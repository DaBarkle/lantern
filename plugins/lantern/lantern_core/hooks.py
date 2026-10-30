"""Claude Code hook entry points. Each reads the event JSON on stdin and may print a JSON reply."""
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, alerts, discovery, doctor, insights, jobs, memory, packs, register, resources, semantic, shield, stage, statusline, survey, transcript
from .paths import ensure_data_dir

SYSTEM_EVENTS = ("<task-notification", "<subagent-notification", "<persisted-output", "<system-reminder")
RECALL_LIMIT = 3
CAPTURE_CHARS = 1200  # per half: a pasted log must not crowd out the reply
SEMANTIC_FLOOR = 0.45  # MiniLM cosine for an item to be a candidate
SEMANTIC_GATE = 0.5  # best cosine below this = memory has nothing on the topic (eval: absent facts top out at 0.41)
KEYWORD_SHARE = 0.5
MIN_CHARS = 40  # log lines like "User requested to proceed." carry nothing
PROFILE_CHARS = 3000
RULES_CHARS = 12000
INDEX_CHARS = 20000  # one short line per curated note, so a session knows what memory holds before it asks
TOP_CHARS = 1500
TITLE_LIMIT = 7  # after the full notes, further matches as one-line titles  # Hermes loaded a ~23k-char memory index every session; one line per rule is leaner
RESUME_HOURS = memory.RESUME_HOURS
AGENT_CHARS = 1500
CARRY_CHARS = 1500  # a last exchange carried into the next session; hedged sentences survive the cut whole
SHORT_PROMPT_TERMS = 8  # below this, the session's own names join the recall query
# A correction holds from the moment it is said, not from tomorrow's curation.
# Only openings that state a standing preference. "Stop the container" or "make sure you restart it" are tasks for now.
CORRECTION = re.compile(r"^\s*(?:please\s+)?(?:do not|don't|dont|never|always|from now on|in future|going forward|"
                        r"stop (?:doing|using|making|trying|asking|adding|suggesting)|i(?:'ve| have)? told you|i said|i asked you|"
                        r"remember that)\b", re.I)
KIND_WEIGHT = {"fact": 1.5, "pattern": 1.5, "note": 1.2}  # curated facts and why-notes outrank raw turn logs
CURATED = {"fact", "rule", "pattern", "doc"}
CURATED_MIN_TERMS = 9


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _is_system(text: str) -> bool:
    return text.lstrip().startswith(SYSTEM_EVENTS)


LOG_BYTES = 1_000_000  # a log is a window on recent behaviour, not an archive: keep the newest part


def _log(name: str, record: dict) -> None:
    path = ensure_data_dir() / f"{name}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), **record}) + "\n")
    try:
        if path.stat().st_size > LOG_BYTES:
            lines = path.read_bytes()[-LOG_BYTES // 2:].split(b"\n", 1)[-1]
            path.write_bytes(lines)
    except OSError:
        pass


FIRST_RUN = """This is Lantern's first session on this machine. Before this reply, Lantern looked at the machine (the summary
above), worked out the findings on its Stage, started a read-only background survey (reports in a few minutes), and took
on three read-only duties by default: it watches for failed services, disk running short, and uncommitted or unpushed
project work. Onboarding happens here, visually; the user never runs a command and never leaves Claude.

Write steps 1 to 4 as text before any tool call: the context above already holds what they need, and a first sight
of shell commands is the wrong first sight. Tools come after the question in step 5 is answered; if this client offers
an Agent tool you may then launch up to three read-only explore agents in parallel (one each: services, timers and
containers; projects and backups; secrets and exposure), each told to only read files and report three findings with
file and line, and fold what they return into the wider look. Never more than three, never with write tools; the
background survey still runs and reports later. Your first reply, in this order, short:
1. Answer their message in a line if it was a request; otherwise one line of hello. One sentence on who you are:
   Claude with Lantern, which remembers this machine and your sessions and brings what matters into every prompt.
2. "What I found", one line: the machine and the counts from the summary above (no table of tool names).
3. "Things you may not know": at most three findings from the Stage, each with its cause (from the finding) and the
   action you propose, ranked by data loss and live credentials first, overlapping automation next, hygiene last; any
   further findings as one line "plus N smaller things, say 'show the rest'". A finding is already checked: never say
   "I'd check" or "unconfirmed"; if something matters and you can read it now (journals, scripts, git), read it in
   this turn and state what you found. For a folder that looks personal (family, finance), propose a local or an
   existing backup, never a remote.
4. "I've started watching" one line: failed services, disk, unpushed work; "say 'stop watching X' to turn one off".
5. One AskUserQuestion with two questions: (a) confirm the guess of what this machine is for, built from the finds
   (e.g. "Gaming, a home media stack and building X. Right?") with options plus their own words; (b) guardrails
   as multi-select, using exactly these texts as options (add one machine-specific option if the finds call for it,
   e.g. "Ask before deleting podman images"): {guardrails}. Phone approvals only if no alert channel to the phone was
   found on this machine. Store each chosen option verbatim with `remember --rule "<exact text>"` and quote
   rules verbatim whenever you apply one; never paraphrase a rule. Store the answers with `remember --profile "<text>"`
   and `remember --rule "<text>"`; for phone approvals run `approve setup` and show the two steps for the ntfy app.
6. Look wider in this same reply, read-only, without asking: run `depth 3` (containers, VMs, services, apps, SSH hosts,
   local servers), show the new finds from the command's output as a table, and say in one line that "look less"
   narrows it. Reading never needs permission; only a change to the machine does. The command is yours to run, never
   shown to the user; they see only what was found.
Lantern's own commands above run in exactly this shape, one per Bash call, nothing before or after it:
`uv run --no-project --quiet "{entry}" remember --rule "<text>"` (same for remember --profile, approve setup, depth 3,
secret ask NAME, forget). Never put the path in a variable, never chain two with && or ;, never wrap it: the user sees
each permission prompt, and this shape is the one they learn to trust.
No table of what Lantern does; it shows through behaviour. Secrets, if they come up: raw secrets never reach the model.
Never show the user syntax to type. To get a key or password, first ask with the question tool, in plain words, what
it unlocks and how they want to give it: "Paste it as my next message (it goes straight to the keychain, never to me)",
"It is in my password manager", "Not now". On the first, run `"{entry}" secret ask NAME` and show the one sentence it
prints; on the second, ask which manager and link it; on "Not now", drop it and never re-ask unprompted.
When the Stage announces the survey, open your next reply with "While you were working I found more:" and show its
service map (table and tree), its "Worth knowing" (at most three numbered, the rest as "plus N smaller"), and the
duties it proposes beyond the monitoring the machine already has, routed through the alert channel it already uses.
If a finding from earlier no longer holds in the light of the survey (for example a folder the backup job already
covers), say so and withdraw it. Report from the commands' own output; never read Lantern's files or database. Never give the user a command
to run, never suggest packs, never suggest a new session. After any task, end by offering the next useful thing."""
EVERY_SESSION = """Lantern notes: the user never runs /lantern commands; whatever Lantern needs, do it yourself with
`uv run --no-project --quiet "{entry}" ...` (remember, forget, depth, discover, approve, secret, ssh grant HOST, pack on NAME, doctor, status),
always in exactly that shape, one per Bash call, never in a variable, never chained with && or ;. Ask
choices with the AskUserQuestion tool, never as prose alternatives at the end of a reply ("A, or say B"), so a "yes"
is never ambiguous; after a declined check, say what is known without it and move on, never "allow it next time".
Explain with tables, short lists and indented text trees (never mermaid).
When the user states a rule or preference ("from now on...", "don't..."), Lantern stores it at once; just acknowledge
it. If they ask whether Lantern is working or what it knows, answer from this context and `"{entry}" status`.
Findings on the Stage were checked within the last hour; when you recommend an order of fixes, rank verified data
loss and verified secret exposure first, services down next, drift and unpinned code after, cosmetic last, and say what
each would free or prevent; at most three numbered, the rest as "plus N smaller". Never say "I'd check", "I didn't check" or
"unconfirmed" about something you can read now: read it in this turn (read-only), before the reply ends, and say what you found; before saying
you cannot verify something, look for file evidence (state files, logs, last-run stamps, artifact dates) and cite it.
Before proposing any fix, check it against the user's rules, the pending defaults above and the other open findings:
a fix that moves personal or finance data to another machine, or that worsens another finding, is ordered after the
fix that removes the risk, and said so. A finding that disappeared is explained by the since-last-session line; never
say it "dropped off". Open a returning session's reply with two sentences: what changed since last time (from the since-last line, the
watches line and where the last session left off) and what Lantern did about it; if nothing changed, say so. If a fact,
priority or remedy differs from what Lantern said before, say "Correction:" with the reason; never drop a finding
silently. When a finding conflicts with a choice the user made on purpose (a file or comment saying so), it is an accepted
risk: never ask them to reopen it; propose the strongest mitigation that honours the choice, and mention the accepted
risk at most as one line. Data with zero copies outranks everything else. Never headline a finding whose location you do not know; ask where it lives.
If you cannot run something because this session has no shell, say what Lantern will do once it can, never a command
for the user to run. Speak in one first-person voice ("I"); name Lantern only in a greeting, never as a
second agent ("Lantern has saved" -> "I've saved"). Known
benign look-alikes: a Plex claim token expires in minutes{lookalikes}. Before
proposing to push anything to a remote, prefer a backup the user already has and say a secret scan comes first.
If the next useful step is read-only or produces a draft (a diff, a list, a check), do it in this reply and show it;
ask only before a write to the system. Something left unfinished in an earlier session is never re-offered in the same words: do
its read-only or text part now (draft the diff, read the file, state the cause), re-ask it once, or retire it with a
reason. When a standing rule touches the request, begin with `Your rule: "<exact text>"` and apply it, including the
reason it was given (check what the reason protects before acting). Read-only watching is on by default: announce it, offer "say stop watching X",
never ask permission for it. Never mention tool names, hooks, "no shell", "the question tool" or Lantern's commands to
the user; at most one plain line about what you could not reach. Absolute words ("only", "never", "all") are allowed
only on claims tagged verified. Before saying A must come before B, name the concrete mechanism by which B fails
without A; if you cannot, there is no dependency. Never suggest pushing, copying off-machine or deleting a folder in
a first reply, and never suggest a remote for a tree that has not had a secret scan. Sort fixes so that anything another fix depends on comes first. Label evidence:
"verified" only for what was observed in the artifact itself; "from config" for what is inferred from paths or
settings. Use the "Now:" line above to compare against scheduled runs and flag a missed one. After any task, end by
offering the next useful thing in one line, drawn from what Lantern knows. Secrets: a raw value never goes in a command
or a note; to get one, first ask with the question tool how they want to give it (paste it as the next message, which
goes straight to the keychain and never to the model; it is in a password manager; not now), then run `"{entry}" secret ask NAME`
and show its sentence (never syntax); a password
manager item is linked with `secret link NAME op://...` (also bw://, pass://, keepass://) and read only when a command
runs; a host is reached with Lantern's own key after one tap on the phone (`ssh grant HOST`); never install a password
manager for the user. Packs (docker, media-server, home-assistant, unifi, tailscale) switch on by themselves when their
system is found and on the user's yes with `pack on NAME`; nothing is installed and no restart is needed, so never
propose a plugin install or a new session for one."""


def session_start(payload: dict) -> dict | None:
    d = ensure_data_dir()
    entry = Path(__file__).resolve().parents[1] / "lantern.py"
    first = not discovery.settings_path().exists()
    if first:
        discovery.auto()  # the first reply should already know this machine: look at the workspace now, within the depth default
        discovery.save_settings({**discovery.settings(), "first_session": payload.get("session_id", "?")})
        survey.start()  # the deeper understanding arrives a few minutes in
        discovery.save_settings({**discovery.settings(), "pending": ["purpose", "guardrails", "wider"]})
        db0 = memory.connect()
        try:
            for marker, rule in SEEDED.items():
                if Path(marker).exists():
                    memory.add(db0, "rule", "seeded", rule)
        finally:
            db0.close()
    started = []
    if not first and payload.get("source") not in ("compact", "resume"):  # learning and health run by themselves, once a day
        started = jobs.start(jobs.due(), entry)
    if resources.can_spawn():
        doctor.start_server()  # idle servers exit; each session brings one back (a no-op while one is up)
    if payload.get("source") not in ("compact", "resume"):
        capture_last_session(payload.get("cwd") or "", payload.get("session_id", "?"))
    packs.sync()  # active packs' skills stay current with the plugin, and the skills folder exists for the session's watcher
    lines = [f"Lantern {__version__} is active. Its data folder is {d}."]
    if "discover" in started:  # background work is visible: the user never wonders what is running
        lines.append(f"Scouting now: Lantern is looking over this machine again in the background (depth {discovery.settings().get('depth', 2)}, "
                     "read-only). Say so in one short line in your reply; new finds appear on Stage at the next session start.")
    if payload.get("source") not in ("compact", "resume"):
        _, msg, changed = statusline.ensure()  # the per-turn signal sets itself up; the user edits nothing
        if changed:
            lines += [f"Lantern's {msg}: its one-line status now shows under each reply, and in a terminal at the bottom of the "
                      "screen. Tell the user so in one line the first time you reply."]
    db = memory.connect()
    try:
        moved = ""
        if payload.get("source") not in ("compact", "resume"):
            insights.run(db)  # findings are re-checked at every session start, so a recommendation is never stale
            alerts.run(db, send=False)  # the user sees them here; the phone only hears what trips while they are away
            alerts.ensure_scheduled(entry)  # the hourly check installs itself once the phone can be reached
            discovery.refresh_frontiers()  # and what is beyond reach closes by itself once its key or sign-in is there
            moved = insights.diff_since_last({e["key"] for e in stage.top(db, k=50) if e["source"] == "insight"}, db)
        profile = memory.profile(db, PROFILE_CHARS)
        rules = memory.rules(db, RULES_CHARS)
        index = memory.rules(db, INDEX_CHARS, kind="fact", dated=True)
        patterns = memory.rules(db, INDEX_CHARS // 2, kind="pattern", dated=True)
        session = payload.get("session_id", "?")
        cwd = payload.get("cwd") or ""
        previous = memory.previous_session(db, session, cwd)
        tail = memory.last_session(db, session, RESUME_HOURS * 3600)
        learned = memory.learned_since(db, previous["updated"] if previous else time.time() - 7 * 86400)
        no_rules = not memory.rules(db, 2000)
        memory_rules_count = len(memory.rules(db, 20000))
        watches = insights.watch_summary(db)
        open_block = insights.open_block(db)
        found_new = insights.discovered_since_last(db)
        if found_new and payload.get("source") not in ("compact", "resume"):
            stage.resolve(db, "discovery/new")  # said as new once; the next session must not call it new again
        # After a compaction or resume the details of this session are no longer in view: bring back its working memory.
        own = memory.session_turns(db, session) if payload.get("source") in ("compact", "resume") else []
        own_ledger = memory.ledger_get(db, session) if own else None
        git_state(db, cwd)
        new_big_files(db, cwd)
        bigfiles = [e["message"] for e in stage.top(db, k=200) if e["key"].startswith("bigfile/")]
        attention = stage.delta(db, payload.get("session_id", "?"))  # marks it seen, so prompt 1 doesn't repeat it
    finally:
        db.close()
    if profile:
        # Who the user is matters on every turn, so it is loaded once rather than searched for.
        lines += ["", "What Lantern knows about the user:", profile]
    if rules:
        # Standing rules apply everywhere, so like the profile they are loaded, not searched for.
        lines += ["", "The user's standing rules and preferences (from past sessions):"] + [f"- {r}" for r in rules]
    if patterns:
        lines += ["", "How the user's systems are worked on (patterns learned from past sessions):"] + [f"- {p}" for p in patterns]
    machine = discovery.summary()
    if machine:
        lines += ["", "This machine, as Lantern found it: " + machine + "."]
        lines += ["Lantern is watching, hourly: failed services, disk running short, uncommitted or unpushed project work, "
                  "and the services it knows (daily). Say 'Lantern is watching ...' only in those words; it is true."]
    if packs.active():
        lines += ["Packs on for this machine (their skills are in the listing as /lantern-<pack>; guards enforce what they promise): "
                  + ", ".join(sorted(packs.active())) + "."]
    for snippet in discovery.readme_snippets():
        lines += [f"In the user's own words (README of a recent project): {snippet}"]
    if discovery.frontier_block():
        lines += ["", discovery.frontier_block()]
    lines += [register.guidance()]
    if not first and not profile and no_rules:
        lines += ["Open loop: the user has not yet said what this machine is for or chosen any guardrail. Assume until told: "
                  "personal and finance folders never leave this machine; ask before restarting a service or changing the OS. "
                  "Re-ask at most one of those questions, with that default stated."]
    progress = survey.status()
    if progress:
        lines += ["", progress]
    understanding = survey.brief()
    if understanding:
        lines += ["", "Lantern's understanding of this machine (from its survey; quote from it when the user asks what Lantern knows):",
                  understanding]
    if discovery.IN_CONTAINER:
        lines += ["This session runs inside a container on the user's machine" + (
            "; Lantern reaches the host through distrobox-host-exec, so host containers, services and tools are included."
            if discovery.HOST_EXEC else ", with no way to reach the host, so host containers and services are out of view: say so once if it matters.")]
    if index:
        lines += ["", "Notes Lantern holds, newest first (one line each, dated; recall brings in detail when a prompt matches; "
                  "an older note may have been overtaken by events):"] + [f"- {i}" for i in index]
    if learned and not first:
        lines += ["", "Learned since the user's last session (mention it briefly if the user asks what Lantern has picked up):"]
        lines += [f"- {l}" for l in learned]
    if own:
        lines += ["", "This session's working memory (from before the compaction):",
                  f"- Started with: {own[0][:400]}",
                  f"- Names in play: {', '.join(memory.identifiers(own))}"]
        lines += memory.ledger_lines(own_ledger)[2:] if own_ledger else []
        lines += [f"- Recent: {t[:500]}" for t in own[-3:]]
    elif previous:
        where = "in this folder" if previous["cwd"] == cwd else f"in {previous['cwd']}"
        lines += ["", f"Where the user's last session {where} left off ({time.strftime('%Y-%m-%d', time.localtime(previous['updated']))}):"]
        lines += memory.ledger_lines(previous)
        if tail:
            lines += [f"- Last exchange: {memory.carry(tail[-1]['text'], CARRY_CHARS)}"]
    elif tail:
        lines += ["", "Where the user's last session left off:"] + [f"- {memory.carry(p['text'], CARRY_CHARS)}" for p in tail]
    if attention:
        lines += ["", "On Lantern's Stage (open findings, most pressing first, checked within the last hour; a finding's text is its "
                  "evidence, cite it rather than saying you have not read the log):", attention]
    tail_block = []
    if moved:
        tail_block += [moved]
    if bigfiles:
        tail_block += ["New large files near the user's work (name each in the reply and say what it is before anyone deletes it): " + "; ".join(bigfiles)]
    if found_new:
        tail_block += [found_new]
    tail_block += [open_block]
    if not first:
        at = discovery.settings().get("insights_at")
        tail_block += [f"You already know the state of this machine: Lantern's watches (failed services, disk, unpushed work, known "
                  f"services, backup jobs, new scheduled jobs) ran at {time.strftime('%H:%M', time.localtime(at)) if at else 'this session start'}"
                  f" and found: {watches}. When the user asks whether things are OK, that is the answer: say it in plain words first "
                  "(what is failing, what is fine), then look deeper only if useful. A declined or failed command never leaves the "
                  "user without an answer; you have this one. Duties beyond these watches: a read-only one is on from the moment it is named; "
                  "only a duty that changes the machine stays a proposal until the user says yes. Say which is which."]
        tail_block += [f"Phone alerts: {alerts.status()}. Change it with `alerts on|off` or `alerts kinds ...` after a structured "
                       "question, never by editing a file."]
        pending = discovery.settings().get("pending") or []
        if discovery.settings().get("answered_at") and "purpose" not in pending:  # "wider" still pending is not a profile gap
            tail_block += ["The setup questions were answered (guardrails and profile above are in force)."]
        elif discovery.settings().get("answered_at"):
            tail_block += ["The guardrails were accepted; what this machine is for was never confirmed: state your reading of it in one "
                           "line and ask once whether it is right (then store it with `remember --profile`)."]
        elif pending:
            tail_block += ["The setup questions from the first session were never answered; defaults apply (see open loop below)."]
    lines += ["", f"Now: {time.strftime('%Y-%m-%d %H:%M %Z')}."]
    lines += ["", (FIRST_RUN if first else EVERY_SESSION).format(entry=entry, guardrails=" | ".join(f'"{g}"' for g in GUARDRAILS),
                                                                lookalikes=lookalikes())]
    if tail_block and not first:
        stored = memory_rules_count
        discovery.update_settings(since_last={**(discovery.settings().get("since_last") or {}), "block": "\n".join(tail_block)[:6000], "block_at": time.time()})
        lines.insert(1, "Since last session, in brief: " + tail_block[0][:300].rstrip() + " (The full SINCE LAST SESSION block closes "
                     "this context; your first reply opens from it.)")  # named at the top too: a long context must not bury it
        lines += ["", "SINCE LAST SESSION (computed by Lantern; open your first reply with one or two sentences built only from "
                  "these lines, using their counts and reasons exactly, each item by its plain name (names of things as the register line above says); never split a count into parts of your own; name every "
                  "item in a 'Found on this machine' line; then answer):"] + tail_block
        lines += [f"Rules already stored by Lantern: {stored} (listed above as the user's standing rules; nothing is pending a save). "
                  "A finding's journal lines above were read by Lantern's watcher: cite them as the cause; never say the log is unread."]
        if previous and previous.get("loops"):
            lines += ["Left unfinished last session (name each one in this reply: done now, asked again once, or dropped with a reason; "
                      "never say 'open loop' to the user): "
                      + "; ".join(previous["loops"])]
    return _context("SessionStart", "\n".join(lines))


def capture_last_session(cwd: str, current: str) -> None:
    """A session that ended without its Stop hook (a crash, print mode) still gets its final exchange captured: at the
    next session start in the same folder, the newest other transcript is read and stored if it was not already."""
    if not cwd:
        return
    # Claude Code names a project folder after its path with every non-alphanumeric character turned into "-" (so a
    # Windows "C:\\Users\\x" and a Linux "/home/x" both work), under CLAUDE_CONFIG_DIR when that is set.
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    try:
        files = sorted(folder.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return
    for f in files[:3]:
        sid = f.stem
        if sid == current:
            continue
        turn = transcript.last_turn(f)
        if not turn["user"] or _is_system(turn["user"]):
            continue
        db = memory.connect()
        try:
            last = db.execute("SELECT text FROM items WHERE kind='turn' AND source=? ORDER BY created DESC LIMIT 1", (sid,)).fetchone()
            if not (last and last[0].startswith(f"User: {turn['user'][:CAPTURE_CHARS]}"[:200])):
                _store_turn(db, sid, cwd, turn)
            elif _empty_reply(last[0]) and turn["reply"].strip():
                row = db.execute("SELECT id FROM items WHERE kind='turn' AND source=? ORDER BY created DESC LIMIT 1", (sid,)).fetchone()
                _repair_turn(db, row[0], sid, cwd, turn)  # the session's last reply was stored empty: its loops matter most
        finally:
            db.close()
        return


def capture_previous(payload: dict) -> None:
    """The Stop hook captures each exchange; where it does not run (print mode, some clients) the next prompt captures the
    previous exchange from the transcript instead, so memory, the ledger and open loops never depend on it."""
    path = payload.get("transcript_path") or ""
    if not path:
        return
    turn = transcript.last_turn(path)
    if not turn["user"] or _is_system(turn["user"]):
        return
    session = payload.get("session_id", "?")
    head = f"User: {turn['user'][:CAPTURE_CHARS]}"
    db = memory.connect()
    try:
        last = db.execute("SELECT id, text FROM items WHERE kind='turn' AND source=? ORDER BY created DESC LIMIT 1", (session,)).fetchone()
        if last and last[1].startswith(head[:200]):
            if _empty_reply(last[1]) and turn["reply"].strip():
                _repair_turn(db, last[0], session, payload.get("cwd") or "", turn)  # Stop ran before the text landed
            return  # the Stop hook already captured it
        _store_turn(db, session, payload.get("cwd") or "", turn)
    finally:
        db.close()


def _empty_reply(text: str) -> bool:
    body = text.split("\nAssistant:", 1)[1] if "\nAssistant:" in text else ""
    return not body.split("\nActions:", 1)[0].strip()


def _repair_turn(db, item_id: int, session: str, cwd: str, turn: dict) -> None:
    """Fill in a turn that was stored before its reply existed, and let the ledger see the reply's unfinished items."""
    text = f"User: {turn['user'][:CAPTURE_CHARS]}\nAssistant: {memory.carry(turn['reply'], CAPTURE_CHARS)}"
    with db:
        db.execute("UPDATE items SET text=? WHERE id=?", (shield.redact(text), item_id))
    memory.ledger_update(db, session, cwd, {**turn, "files": [], "commands": [], "agents": [], "failed": 0})


def _store_turn(db, session: str, cwd: str, turn: dict) -> None:
    text = f"User: {turn['user'][:CAPTURE_CHARS]}\nAssistant: {memory.carry(turn['reply'], CAPTURE_CHARS)}"
    acts = [f"edited {', '.join(turn['files'][-6:])}" if turn["files"] else "",
            f"ran {'; '.join(turn['commands'][-4:])}" if turn["commands"] else "",
            f"{turn['failed']} tool call(s) failed" if turn["failed"] else ""]
    if any(acts):
        text += "\nActions: " + "; ".join(a for a in acts if a)
    memory.add(db, "turn", session, shield.redact(text))
    memory.record_corrections(db, session, shield.redact(turn["reply"]))
    memory.ledger_update(db, session, cwd, turn)


_REASON = re.compile(r"\b(?:because|since|as it|it breaks|breaks|so that|otherwise|my \w+ (?:watch|use|need|rely))\b", re.I)


_WHAT_CHANGED = re.compile(r"what(?:'s| is| has)? (?:changed|new|happened)|since (?:yesterday|last time|last session|I was (?:away|gone))|"
                           r"anything (?:new|I should know|to report)|how (?:is|are) (?:things|everything|the machine|my (?:computer|machine))", re.I)


def user_prompt_submit(payload: dict) -> dict | None:
    prompt = payload.get("prompt", "")
    if not prompt.strip() or _is_system(prompt):
        return None
    capture_previous(payload)
    register.learn(prompt)
    answered = setup_answer(prompt)
    name, stored = shield.paste(prompt)
    if name:  # a secret handed over in chat: this message never reaches the model, stored or not
        if not stored:  # a device-less session has no keychain, and Lantern never keeps a secret anywhere else
            return {"decision": "block", "reason": f"This session has no keychain to hold {name}, so it was not stored anywhere "
                                                    "and never reached the model. Hand it over in a session on your own machine."}
        db = memory.connect()
        try:
            stage.publish(db, f"secret/{name}", "shield", f"The user stored a secret named {name}; commands use it as "
                          f"`lantern secret run {name} -- <command>` ($" + name + " in the environment).", score=4, days=1)
        finally:
            db.close()
        return {"decision": "block", "reason": f"Lantern stored {name} in your keychain. It never reached the model. "
                                                "Carry on; Lantern will use it when a command needs it."}
    session = payload.get("session_id", "?")
    query = prompt
    want = set(memory.terms(query))
    if len(want) < SHORT_PROMPT_TERMS:  # "are they all direct?": the previous message carries the topic
        query += "\n" + transcript.previous_user(payload.get("transcript_path", ""))
        want = set(memory.terms(query))
    noted = None
    # A keyword hit must share most of the query's words; common words alone ("operator", "server") match everything.
    need = max(2, math.ceil(KEYWORD_SHARE * len(want))) if len(want) > 1 else 1
    db = memory.connect()
    try:
        if len(want) < SHORT_PROMPT_TERMS:  # "is it still failing?": the session's own names say what "it" is
            query += "\n" + " ".join(memory.identifiers(memory.session_turns(db, session), limit=10))
            want = set(memory.terms(query))
        noted = note_correction(db, session, prompt)
        touching = memory.rules_touching(db, prompt)
        touching_findings = insights.findings_touching(db, prompt)
        touching_frontiers = discovery.frontiers_touching(prompt)
        kw_hits = [h for h in memory.search(db, query, limit=(RECALL_LIMIT + TITLE_LIMIT) * 3)
                   if len(want & set(memory.terms(h["text"]))) >= need]
        keyword = [h["id"] for h in kw_hits]
        found = semantic.search(ensure_data_dir(), query)
        if found is None and resources.can_spawn():
            doctor.start_server()  # keyword-only this turn; warm again in a few seconds
        meaning = [i for i, cos in found or [] if cos >= SEMANTIC_FLOOR]
        if found is not None:
            # With the server up, its best cosine decides whether memory has anything on this at all, and keyword-only
            # hits must also match on meaning (eval: those were mostly unrelated turn logs). Curated notes and documents
            # that share most of the prompt's words are kept either way: they are written to be precise (eval: an
            # as-built section on quality profiles scored 0.47 and was dropped).
            # Short questions ("the operator's shoe size?") share two words with almost anything, so only longer
            # prompts can vouch for a curated match on words alone.
            curated = [h["id"] for h in kw_hits if h["kind"] in CURATED] if len(want) >= CURATED_MIN_TERMS else []
            if not found or found[0][1] < SEMANTIC_GATE:
                meaning = []
            keyword = [i for i in keyword if i in set(meaning)] + [i for i in curated if i not in set(meaning)]
        # Reciprocal rank fusion: items found by both searches rise to the top.
        fused: dict[int, float] = {}
        for ranked in (keyword, meaning):
            for rank, i in enumerate(ranked):
                fused[i] = fused.get(i, 0.0) + 1 / (60 + rank)
        rows = memory.get(db, list(fused))
        attention = stage.delta(db, payload.get("session_id", "?"))
    finally:
        db.close()
    # This session's turns are already in context. Newer items on the same topic outrank older ones (stale state was
    # the main source of wrong statements about the user's systems), with the same gentle half-life as keyword search.
    now = time.time()

    def rank(i: int) -> float:
        row = rows.get(i, {})
        age = max(0.0, (now - row.get("created", now)) / 86400)
        return fused[i] * KIND_WEIGHT.get(row.get("kind"), 1.0) * (0.5 + 0.5 * math.exp(-age * math.log(2) / memory.HALF_LIFE_DAYS))

    order = sorted(fused, key=rank, reverse=True)
    hits, seen = [], set()
    for i in order:
        h = rows.get(i)
        if not h or h["source"] in (session, f"said:{session}") or len(h["text"]) < MIN_CHARS:
            continue
        if h["text"][:200] in seen:  # the same note stored twice
            continue
        seen.add(h["text"][:200])
        hits.append(h)
        if len(hits) == RECALL_LIMIT + TITLE_LIMIT:
            break
    hits, titles = hits[:RECALL_LIMIT], hits[RECALL_LIMIT:]
    _log("recall", {"fired": bool(hits), "hits": [h["id"] for h in hits],
                    "keyword": len(keyword), "semantic": len(meaning)})
    lines = []
    if hits:
        lines.append("Lantern recalled these past notes as possibly relevant:")
        for n, h in enumerate(hits):
            day = time.strftime("%Y-%m-%d", time.localtime(h["created"]))
            # The best match is shown at length: a note's fix or root cause often comes after its symptom.
            lines.append(f"- [{day}, {h['kind']}] {h['text'][:TOP_CHARS if n == 0 else 400]}")
        if titles:
            lines.append("Also possibly relevant:")
            lines += [f"- [{time.strftime('%Y-%m-%d', time.localtime(h['created']))}] {h['text'].strip().splitlines()[0][:160]}"
                      for h in titles]
    if _WHAT_CHANGED.search(prompt):
        since = discovery.settings().get("since_last") or {}
        block = since.get("block") if time.time() - since.get("block_at", 0) < 12 * 3600 else ""
        lines = (["This asks what changed. Lantern computed the answer at session start; open the reply with it, counts and names as "
                  "given, one line per new, changed, resolved or back-again item, then the unfinished items (done now, asked again once, "
                  "or dropped with a reason). File timestamps in this folder are not the answer; use them only to add detail to an item "
                  "already listed."] + ([block] if block else ["(The SINCE LAST SESSION block is in your session context above.)"])
                 + [""] + lines)
    if touching:
        lines += ["Standing rules that touch this request (quote the one you apply, word for word):"] + [f"- {r}" for r in touching]
        lines += ["A rule reaches exactly what it names: one about X says nothing about Y, so if this request is outside the "
                  "rule's words, say so in one clause and proceed; if inside, honour it."]
        if any(_REASON.search(r) for r in touching):
            lines += ["A rule above states its reason. Before acting on it, run one read-only check of whether that reason holds "
                      "right now (who is watching, what is running, what would be lost) and say what it showed; the rule still "
                      "applies either way."]
    since = discovery.settings().get("since_last") or {}
    first_reply = sum(1 for r, _ in transcript.messages(payload.get("transcript_path", "")) if r == "user") <= 1
    if since.get("names") and time.time() - since.get("at", 0) < 12 * 3600 and (first_reply or _WHAT_CHANGED.search(turn["user"])):
        said = turn["reply"].lower()
        missing = [n for n in since["names"] if n.lower() not in said]
        if missing:
            return {"decision": "block", "reason": "A returning session's first reply, and any answer to what changed, names every item the SINCE LAST "
                    "SESSION block lists as new, resolved, changed or back again. Not named yet: " + ", ".join(missing[:8])
                    + ". Add one line per item with its state."}
    at = discovery.settings().get("insights_at", 0)
    if HEALTH_ASK.search(prompt) and time.time() - at < SNAPSHOT_FRESH_S:
        # The cheap checks run again right now, so the answer is current and the reply never needs an "I haven't looked
        # live" hedge (which the Stop hook would then contradict with live figures).
        db = memory.connect()
        try:
            insights.run(db)
        finally:
            db.close()
        lines += ["You already know the answer to this, and it is current: failed units, disk and scheduled jobs were re-checked "
                  f"moments ago ({time.strftime('%H:%M')}); the rest is from the hourly scan. " + snapshot_line(at) + " Say it first, "
                  "in plain words (what is failing, what is fine), as something you have checked, never as something you have not "
                  "looked at. A deeper read-only look is optional and, if one is declined, this answer still stands. Never hand "
                  "the user commands to run."]
    if touching_findings:
        lines += ["Open findings that touch this request (connect them to it):"] + [f"- {f}" for f in touching_findings]
    if touching_frontiers:
        lines += ["Beyond reach and touching this request (do what you can without it; raise the ask once, in one line):"] + [f"- {f}" for f in touching_frontiers]
    if attention:
        lines += ["Lantern's Stage changed since your last turn:", attention]
        if "finished its survey" in attention:  # deliver the brief now, not at the next session start
            lines += ["", "Lantern's survey of this machine (open your reply with 'While you were working I found more:' and show "
                      "its worth-knowing items, proposed duties and service map):", survey.brief()]
            db = memory.connect()
            try:
                stage.resolve(db, "survey/done")
            finally:
                db.close()
    progress = survey.status()
    if progress:
        lines.append(progress)
    if answered:
        lines += [answered]
    if noted:
        lines += ["Lantern stored this message as a standing rule for future sessions; acknowledge it in a few words."]
    reply = _context("UserPromptSubmit", "\n".join(lines)) if lines else {}
    if noted:
        reply["systemMessage"] = "Lantern noted a standing rule from your message."
    return reply or None


GUARDRAILS = ["Never restart a service or container on this machine without asking first.",
              "Never push or copy folders that look personal (family, finance) off this machine.",
              "Ask before changing the operating system (packages, system config, system-wide services)."]
SEEDED = {  # rules the machine itself implies; stored at the first session so nobody has to say them
    "/run/ostree-booted": "This is an immutable OS (ostree): never layer packages or edit /usr; use Flatpak, Homebrew or containers.",
}
# Facts about this OS that stop the model misreading a finding; each only where its marker exists.
LOOKALIKES = {"/run/ostree-booted": "/home is /var/home on Fedora Atomic (one volume)"}


def lookalikes() -> str:
    return "".join(f"; {t}" for m, t in LOOKALIKES.items() if Path(m).exists())
YES = re.compile(r"^\s*(?:ok(?:ay)?|yes|yep|sure|all|all of them|both|go ahead|do it|fine|sounds good)\b[.!]?\s*$", re.I)


def setup_answer(prompt: str) -> str | None:
    """The first session asks what the machine is for, which guardrails to keep, and whether to look wider. A short yes
    answers all three; the hook records it so the answer holds even if the model has no shell, and so session two knows."""
    s = discovery.settings()
    pending = s.get("pending") or []
    if not pending or not YES.match(prompt):
        return None
    db = memory.connect()
    try:
        for i, rule in enumerate(GUARDRAILS):
            memory.add(db, "rule", f"setup:{i}", rule)  # one source per rule: the index lists one item per source
    finally:
        db.close()
    s["pending"] = ["purpose"]  # a bare yes covers the guardrails and the wider look, not who the user is
    s["depth"] = max(3, s.get("depth", 2))
    s["answered_at"] = time.time()
    discovery.save_settings(s)
    jobs.start(["discover"], Path(__file__).resolve().parents[1] / "lantern.py")
    return ("The user accepted the setup as proposed. Lantern stored these rules verbatim: " + " | ".join(f'"{g}"' for g in GUARDRAILS)
            + ". It confirmed the machine's purpose as the profile and started the wider look in the background (its finds show "
            "at the next session start or on Stage). Acknowledge in one line that names the rules and says 'I took that as yes to "
            "all of them; say if not'. If you offered a machine-specific option too, store it with `remember --rule` now. If the "
            "user chose a subset, store exactly those with `remember --rule` and drop the others with `forget`.")


def note_correction(db, session: str, prompt: str) -> int | None:
    """A correction ("don't X", "always Y") becomes a rule at once; curation merges or rewrites it tonight."""
    text = prompt.strip()
    if not (15 <= len(text) <= 400) or "?" in text or not CORRECTION.match(text) or shield.find(text):
        return None
    words = set(memory.terms(text))
    for _, line in memory.rule_items(db):
        if len(words & set(memory.terms(line))) >= 0.7 * max(1, len(words)):
            return None  # already a rule
    return memory.add(db, "rule", f"said:{session}", text)


def record_answers(db, path: str) -> None:
    """What the user answered through the question tool is Lantern's to keep: the machine's purpose becomes the profile,
    chosen guardrails become rules, and the setup stops being pending, whether or not the model ran a command for it."""
    s = discovery.settings()
    seen = set(s.get("answers_seen") or [])
    pending = s.get("pending") or []
    changed = False
    for q, a in transcript.answers(path):
        mark = hashlib.sha1(f"{q}={a}".encode()).hexdigest()[:12]
        if mark in seen:
            continue
        seen.add(mark)
        changed = True
        ql = q.lower()
        if "guardrail" in ql or "rule" in ql:
            for i, rule in enumerate(re.split(r"(?<=\.)\s*,\s*|\s*;\s*", a)):
                if 12 <= len(rule.strip()) <= 300 and rule.strip() not in memory.rules(db, 20000):
                    memory.add(db, "rule", f"setup:{mark}:{i}", rule.strip())  # one source per rule: the index lists one per source
            pending = [x for x in pending if x != "guardrails"]
        elif re.search(r"machine|computer|this box|is for|used for", ql) and not memory.profile(db, 4000):
            guess = re.split(r"\.?\s*(?:Is (?:that|this) (?:right|correct)|Right\?|Correct\?)", q)[0]
            guess = re.sub(r"^.*?(?:my guess is|looks like it's|looks like|it seems)\s*(?:that\s*)?", "", guess, flags=re.I).strip()
            if re.match(r"(?i)yes|that's (?:it|right)|correct|mostly", a) and len(guess) >= 12:
                memory.add(db, "profile", "setup", guess[0].upper() + guess[1:])
            pending = [x for x in pending if x != "purpose"]
    if changed:
        discovery.save_settings({**discovery.settings(), "answers_seen": sorted(seen)[-200:], "pending": pending,
                                 **({"answered_at": time.time()} if pending != (s.get("pending") or []) else {})})


def stop(payload: dict) -> dict | None:
    turn = transcript.last_turn(payload.get("transcript_path", ""))
    for _ in range(4):
        if turn["reply"].strip() or not turn["user"]:
            break
        time.sleep(0.4)  # the final text block can land in the transcript a moment after Stop fires
        turn = transcript.last_turn(payload.get("transcript_path", ""))
    if not turn["user"] or _is_system(turn["user"]):
        return None
    db = memory.connect()
    try:
        _store_turn(db, payload.get("session_id", "?"), payload.get("cwd") or "", turn)
        record_answers(db, payload.get("transcript_path", ""))
    finally:
        db.close()
    return None


# Admissions in any tense ("I didn't check", "I haven't checked why", "I haven't looked at the logs") and offers to do a
# read-only look later ("I can look at that next", "If you want, I can take a deeper read-only look"): both are looks that
# belong in this turn.
_UNCHECKED = re.compile(r"\bI (?:didn't|did not|couldn't|could not|haven't|have not|was unable to|wasn't able to|have no way to) "
                        r"(?:check(?:ed)?|verif(?:y|ied)|confirm(?:ed)?|look(?:ed)? (?:at|into)|read|inspect(?:ed)?|see|seen|test(?:ed)?)\b[^.\n]{0,160}"
                        r"|\b(?:if you (?:want|like),? )?I can (?:also )?(?:take a (?:deeper |closer |quick )?(?:read-only )?look|look (?:at|into)|check|read|list)"
                        r"\b[^.\n]{0,120}", re.I)
# A reply that sends the user off to do Lantern's looking for it: it delegates outright, or it admits it has no
# state and then lists commands. Each half alone is fine ("I couldn't read the Plex log" with an answer is honest).
_DELEGATES = re.compile(r"\brun [^.\n]{0,80}\byourself\b|\bso you can (?:approve|run) (?:it|them)\b|\bif you approve(?: it| them)?,? I\b|"
                        r"\bhere is what (?:it|I) would have run\b|\bwhat I would have run\b|\byou (?:can|could|'ll need to|will need to) run (?:it|them|these|`)|"
                        r"\b(?:paste|send|show) me (?:the|its|their) output\b|\blet me know what (?:it|that|they) (?:says?|shows?|returns?)\b", re.I)
_NO_STATE = re.compile(r"\bI (?:don't|do not) know (?:the state|whether|if|how)\b|\bI (?:\w+ )?(?:couldn't|could not|can't|cannot|wasn't able to|"
                       r"was unable to) (?:check|run|see|tell|verify|confirm)\b|\b(?:was|were|got) (?:blocked|declined|denied)\b", re.I)
_CMD_LINE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+`[^`]{2,}`", re.M)
# A closing choice typed as prose ("Tell me which account, or say 'copy it locally'", "Which would you like?") makes the
# user's next "yes" ambiguous; a choice is asked with the question tool. Only the last paragraph counts.
_PROSE_CHOICE = re.compile(r"(?:\bor\b[^.?\n]{3,120}\?\s*$)|(?:\b(?:which|either)\b[^.?\n]{3,120}\?\s*$)|(?:,\s*or\s+(?:say|tell me)\b[^.\n]{3,120}\.\s*$)", re.I)
# After a declined check the user is never asked to allow it next time; what is known without it is stated instead.
_ALLOW_NEXT = re.compile(r"\b(?:allow|approve)\b[^.\n]{0,40}\b(?:next time|when it comes up|the next time|if it comes up)\b|\bsince you declined\b[^.\n]{0,60}\b(?:I (?:won't|will not)|I'm not going to)\b", re.I)
_OFFERED_READ = re.compile(r"\b(?:Should I|Shall I|Want me to|Do you want me to|Say the word and I'll) (?:run|check|read|look|inspect|verify|scan|list|pull)\b"
                           r"(?![^?\n]*\b(?:restart|stop|kill|delete|remove|prune|push|install|change|edit|write|enable|disable)\b)[^?\n]{0,140}\?", re.I)
_JARGON = re.compile(r"`[^`\n]*(?:\s-{1,2}[A-Za-z]|\s/[a-z]\b|\$\w)[^`\n]*`|\bcode=exited\b|\bstatus=\d+/|\b\w+\[\d+\]: |\bexit (?:code|status) \d+\b")
SNAPSHOT_FRESH_S = 2 * 3600
HEALTH_ASK = re.compile(r"\b(?:everything|all|things|anything) (?:ok|okay|fine|good|alright|wrong|broken)\b|\bhow(?:'s| is) (?:my|the) "
                        r"(?:machine|computer|system|server|pc|box)\b|\b(?:machine|computer|system|server|pc) (?:ok|okay|fine|healthy|alright)\b"
                        r"|\bhealth check\b|\bstatus of (?:my|the) (?:machine|computer|system|server)\b", re.I)


_WRITE_CMD = re.compile(r"(?:`[^`\n]*|^[^`\n]*)\b(?:systemctl|podman|docker|rpm-ostree|flatpak|brew|apt|dnf)\b[^`\n]*\b(?:restart|reboot|stop|start|enable|disable|"
                        r"install|remove|uninstall|update|upgrade|daemon-reload|kill|rm)\b", re.M | re.I)


def hands_off(reply: str) -> bool:
    """Does the reply send the user off to do Lantern's looking? A change the user asked for (a restart, an install)
    that was declined is theirs to approve, so handing that command over is not hands-off; the snapshot cannot do it."""
    if _WRITE_CMD.search(reply):
        return False
    cmds = len(_CMD_LINE.findall(reply)) + reply.count("```") // 2
    return bool(_DELEGATES.search(reply)) or (bool(_NO_STATE.search(reply)) and cmds >= 2)


def snapshot_line(at: float) -> str:
    """The watch snapshot as one sentence a reply can be built from."""
    db = memory.connect()
    try:
        watches = insights.watch_summary(db)
        open_block = insights.open_block(db)
    finally:
        db.close()
    return (f"Lantern's watches ran at {time.strftime('%H:%M', time.localtime(at))} and found: {watches}. {open_block}")


def stop_nudge(payload: dict) -> dict | None:
    """A reply that admits not checking something is sent back once: if a read-only command on this machine can answer
    it, the model runs it and adds the result. A reply that sends the user off to run things themselves while Lantern's
    own watch snapshot is fresh is sent back to that snapshot. Runs synchronously, so it stays tiny."""
    turn = transcript.last_turn(payload.get("transcript_path", ""))
    if _is_system(turn["user"]):
        return None
    since = discovery.settings().get("since_last") or {}
    first_reply = sum(1 for r, _ in transcript.messages(payload.get("transcript_path", "")) if r == "user") <= 1
    if since.get("names") and time.time() - since.get("at", 0) < 12 * 3600 and (first_reply or _WHAT_CHANGED.search(turn["user"])):
        said = turn["reply"].lower()
        missing = [n for n in since["names"] if n.lower() not in said]
        if missing:
            return {"decision": "block", "reason": "A returning session's first reply, and any answer to what changed, names every item the SINCE LAST "
                    "SESSION block lists as new, resolved, changed or back again. Not named yet: " + ", ".join(missing[:8])
                    + ". Add one line per item with its state."}
    at = discovery.settings().get("insights_at", 0)
    fresh = time.time() - at < SNAPSHOT_FRESH_S
    if payload.get("stop_hook_active"):  # already nudged this turn: never loop, but never leave the user with nothing either
        if fresh and hands_off(turn["last"]):
            line = snapshot_line(at)
            _log("nudge", {"fallback": True, "user": turn["user"][:200]})
            return {"systemMessage": "What Lantern's own watches see: " + line}
        return None
    if fresh and hands_off(turn["reply"]):
        _log("nudge", {"hands_off": True, "user": turn["user"][:200]})
        return {"decision": "block", "reason": f"Lantern's own watches ran at {time.strftime('%H:%M', time.localtime(at))}, within the last "
                "two hours, so you already know the state of this machine: " + snapshot_line(at) + " Add a short follow-up that "
                "answers from this in plain words: what is failing, what is fine, what changed. No commands for the user to run, "
                "no 'I couldn't check', no 'I don't know the state'. Never quote or paraphrase this note; the user sees only your words."}
    if discovery.settings().get("expertise") == "general" and (jargon := _JARGON.search(turn["reply"])):
        return {"decision": "block", "reason": "This reader asked for plain words, and the reply carries jargon: \"" + jargon.group(0).strip()[:80] + "\". "
                "Add two or three plain sentences saying what that means for them and what happens next, with no commands, flags, "
                "exit codes or raw log lines; an exact detail may stay as one short line at the very end, marked 'details'."}
    last = turn["reply"].rstrip().rsplit("\n\n", 1)[-1]
    if "[question tool]" not in last and (choice := _PROSE_CHOICE.search(last)):
        return {"decision": "block", "reason": "Your reply ends with a choice written as prose: \"" + choice.group(0).strip()[:120] + "\". "
                "A choice is asked with the AskUserQuestion tool, with each option as a label, so the user's next \"yes\" is never ambiguous. "
                "Ask it that way now, or close with one plain offer."}
    if (allow := _ALLOW_NEXT.search(turn["reply"])):
        return {"decision": "block", "reason": "Your reply says \"" + allow.group(0).strip() + "\". A declined check is never put back on the user: "
                "say what is known without it and carry on. Rewrite that sentence."}
    offered = _OFFERED_READ.search(turn["reply"])
    if offered:
        return {"decision": "block", "reason": "Your reply offers a read-only check instead of doing it: \"" + offered.group(0).strip() + "\". "
                "Reading never needs permission here: run it now and add one or two sentences, starting \"Checked after all:\", with what it showed."}
    gaps = _UNCHECKED.findall(turn["reply"])
    if not gaps:
        return None
    return {"decision": "block", "reason": "Your reply says \"" + gaps[0].strip() + "\". If a read-only command on this machine "
            "(status, logs, list, cat, git, systemctl show) can answer that, run it now and add a short follow-up of one or two "
            "sentences that simply states what you found; if the earlier sentence said you had not looked, say plainly that you "
            "now have (no 'Correction', no 'I checked'). If nothing here can answer it, add one clause on what would. Never "
            "quote this note."}


def subagent_stop(payload: dict) -> dict | None:
    """What an agent found is worth as much as what the main session said: keep its final text."""
    path = payload.get("agent_transcript_path") or ""
    msgs = transcript.messages(path)
    reply = next((t for r, t in reversed(msgs) if r == "assistant"), "")
    if not reply.strip():
        return None
    db = memory.connect()
    try:
        memory.add(db, "turn", payload.get("session_id", "?"), shield.redact("Agent result: " + reply[:AGENT_CHARS]))
    finally:
        db.close()
    return None


_DISRUPT = re.compile(r"\b(?:systemctl(?:\s+--user)?\s+(?:restart|stop|kill|disable)|(?:podman|docker)\s+(?:restart|stop|kill|rm)|flatpak\s+kill)\s+(?:-\S+\s+)*([^\s;&|]+)")


def rule_guard(payload: dict) -> dict | None:
    """A restart, stop or kill of something a stored rule names asks the user first, in code, unless this turn already
    quoted the rule to them: "restart it now" is a go-ahead only once the rule and its reason have been said."""
    if payload.get("tool_name") != "Bash":
        return None
    m = _DISRUPT.search((payload.get("tool_input") or {}).get("command", ""))
    if not m:
        return None
    stem = re.sub(r"\.(?:service|timer|container|socket)$", "", Path(m.group(1).strip("'\"")).name).lower()
    words = {w for w in re.split(r"[^a-z0-9]+", stem) if len(w) >= 3} - {"container", "service", "user", "app"}
    if not words:
        return None
    db = memory.connect()
    try:
        rules = memory.rules(db, 20000)
    finally:
        db.close()
    hit = next((r for r in rules if any(w in r.lower() for w in words)), None)
    if not hit:
        return None
    said = transcript.last_turn(payload.get("transcript_path", ""))["reply"].lower()
    if "your rule" in said or hit[:40].lower() in said:
        return None  # quoted in this turn; the user's go-ahead stands
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": f'Your rule: "{hit}". Quote it to the user with the reason it protects and what you checked, '
                                        "and run this only on their go-ahead."}}


def new_big_files(db, cwd: str, now: float | None = None) -> None:
    """A large file that appeared in this folder in the last two days goes on Stage, so a 600 MB scratch file is noticed
    without the user pointing at it; it comes off again once it is gone."""
    if not cwd or not Path(cwd).is_dir():
        return
    now = now or time.time()
    out = discovery._run(["find", cwd, "-xdev", "-maxdepth", "2", "-type", "f", "-size", "+100M", "-mtime", "-2", "-not", "-path", "*/.git/*", "-printf", "%s %p\n"])
    seen = set()
    for line in out.splitlines()[:5]:
        size, _, path = line.partition(" ")
        if not size.isdigit():
            continue
        key = f"bigfile/{path}"
        seen.add(key)
        stage.publish(db, key, "watch", f"a new large file appeared: {path.replace(str(Path.home()), '~')} ({int(size) / 1e9:.1f} GB, written in the last two days); "
                      "say what it is before anyone deletes it", score=3, days=3, now=now)
    for e in stage.top(db, k=200, now=now):
        if e["key"].startswith("bigfile/") and e["key"] not in seen and e["key"][8:].startswith(cwd):
            stage.resolve(db, e["key"])


def git_state(db, cwd: str) -> None:
    """Uncommitted or unpushed work left in this folder goes on Stage; a clean tree takes it off again."""
    key = f"git/{cwd}"
    if not cwd:
        return
    try:
        status = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "--branch"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return
    if status.returncode:
        return  # not a repository
    lines = status.stdout.splitlines()
    dirty = len(lines) - 1
    ahead = re.search(r"ahead (\d+)", lines[0]) if lines else None
    parts = [f"{dirty} uncommitted change(s)" if dirty else "", f"{ahead.group(1)} unpushed commit(s)" if ahead else ""]
    if any(parts):
        stage.publish(db, key, "git", f"{cwd}: " + ", ".join(p for p in parts if p), score=4, days=3)
    else:
        stage.resolve(db, key)


HANDLERS = {"session-start": session_start, "user-prompt-submit": user_prompt_submit, "stop": stop, "stop-nudge": stop_nudge,
            "subagent-stop": subagent_stop, "pre-tool-use": lambda p: shield.pre_tool_use(p) or rule_guard(p) or packs.pre_tool_use(p),
            "signal": statusline.signal}


def run(name: str, stdin=sys.stdin, stdout=sys.stdout) -> int:
    handler = HANDLERS.get(name)
    if handler is None:
        print(f"lantern: unknown hook {name!r}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    t0 = time.monotonic()
    try:
        reply = handler(payload)
    except Exception as e:  # a platform Lantern has not met must never block a prompt: log it, say so once, carry on
        if name == "pre-tool-use":
            raise  # the shield fails closed on its own terms; a crash there must stay visible
        reply = _crashed(name, e)
    if name != "pre-tool-use":  # every prompt-path hook reports how long it took; the doctor watches the trend
        _log("hooks", {"hook": name, "ms": round((time.monotonic() - t0) * 1000)})
    if reply:
        stdout.write(json.dumps(reply))
    return 0


ERROR_NOTE_EVERY = 3600


def _crashed(name: str, e: Exception) -> dict | None:
    import traceback
    try:
        _log("errors", {"hook": name, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]})
        marker = ensure_data_dir() / "errors.noted"
        fresh = time.time() - marker.stat().st_mtime < ERROR_NOTE_EVERY if marker.exists() else False
        marker.touch()
    except OSError:
        return None
    if fresh:
        return None
    return {"systemMessage": f"Lantern's {name} step hit a problem on this machine ({type(e).__name__}: {str(e)[:120]}). "
                             f"The session carries on without it; the doctor has the details."}
