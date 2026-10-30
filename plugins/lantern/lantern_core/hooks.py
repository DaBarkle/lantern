"""Claude Code hook entry points. Each reads the event JSON on stdin and may print a JSON reply."""
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, discovery, insights, jobs, memory, semantic, shield, stage, survey, transcript
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
RESUME_HOURS = 48
PROJECT_RESUME_DAYS = 14  # a session in the same folder is worth showing for longer than one anywhere
AGENT_CHARS = 1500
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

Your first reply, in this order, short:
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
   found on this machine. Store each chosen option verbatim with `"{entry}" remember --rule "<exact text>"` and quote
   rules verbatim whenever you apply one; never paraphrase a rule. Store the
   answers with `"{entry}" remember --profile "<text>"` and `"{entry}" remember --rule "<text>"`; for phone approvals
   run `"{entry}" approve setup` and show the two steps for the ntfy app.
6. Offer to go wider in one line (containers, VMs, services, apps, SSH hosts, local servers; read-only):
   `uv run --no-project --quiet "{entry}" depth 3`, then show the new finds from the command's output as a table.
No table of what Lantern does; it shows through behaviour. Secrets, if they come up: raw secrets never reach the model;
`secret NAME: <value>` typed in chat goes to the keychain and the message is stopped before the model.
When the Stage announces the survey, open your next reply with "While you were working I found more:" and show its
service map (table and tree), its "Worth knowing" (at most three numbered, the rest as "plus N smaller"), and the
duties it proposes beyond the monitoring the machine already has, routed through the alert channel it already uses.
If a finding from earlier no longer holds in the light of the survey (for example a folder the backup job already
covers), say so and withdraw it. Report from the commands' own output; never read Lantern's files or database. Never give the user a command
to run, never suggest packs, never suggest a new session. After any task, end by offering the next useful thing."""
EVERY_SESSION = """Lantern notes: the user never runs /lantern commands; whatever Lantern needs, do it yourself with
`uv run --no-project --quiet "{entry}" ...` (remember, forget, depth, discover, approve, secret, ssh grant HOST, doctor, status). Ask
choices with the AskUserQuestion tool; explain with tables, short lists and indented text trees (never mermaid).
When the user states a rule or preference ("from now on...", "don't..."), Lantern stores it at once; just acknowledge
it. If they ask whether Lantern is working or what it knows, answer from this context and `"{entry}" status`.
Findings on the Stage were checked within the last hour; when you recommend an order of fixes, rank verified data
loss and verified secret exposure first, services down next, drift and unpinned code after, cosmetic last, and say what
each would free or prevent; at most three numbered, the rest as "plus N smaller". Never say "I'd check" or
"unconfirmed" about something you can read now: read it in this turn (read-only) and say what you found; before saying
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
benign look-alikes: a Plex claim token expires in minutes; /home is /var/home on Fedora Atomic (one volume). Before
proposing to push anything to a remote, prefer a backup the user already has and say a secret scan comes first.
If the next useful step is read-only or produces a draft (a diff, a list, a check), do it in this reply and show it;
ask only before a write to the system. An open loop from an earlier session is never re-offered in the same words: do
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
or a note; `secret NAME: <value>` typed in chat goes to the keychain and the message stops before the model; a password
manager item is linked with `secret link NAME op://...` (also bw://, pass://, keepass://) and read only when a command
runs; a host is reached with Lantern's own key after one tap on the phone (`ssh grant HOST`); never install a password
manager for the user."""


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
    elif payload.get("source") not in ("compact", "resume"):  # learning and health run by themselves, once a day
        jobs.start(jobs.due(), entry)
    if payload.get("source") not in ("compact", "resume"):
        capture_last_session(payload.get("cwd") or "", payload.get("session_id", "?"))
    lines = [f"Lantern {__version__} is active. Its data folder is {d}."]
    db = memory.connect()
    try:
        moved = ""
        if payload.get("source") not in ("compact", "resume"):
            insights.run(db)  # findings are re-checked at every session start, so a recommendation is never stale
            discovery.refresh_frontiers()  # and what is beyond reach closes by itself once its key or sign-in is there
            moved = insights.diff_since_last({e["key"] for e in stage.top(db, k=50) if e["source"] == "insight"}, db)
        profile = memory.profile(db, PROFILE_CHARS)
        rules = memory.rules(db, RULES_CHARS)
        index = memory.rules(db, INDEX_CHARS, kind="fact", dated=True)
        patterns = memory.rules(db, INDEX_CHARS // 2, kind="pattern", dated=True)
        session = payload.get("session_id", "?")
        cwd = payload.get("cwd") or ""
        previous = memory.ledger_last(db, session, PROJECT_RESUME_DAYS * 86400, cwd=cwd) if cwd else None
        if previous and previous["cwd"] != cwd and time.time() - previous["updated"] > RESUME_HOURS * 3600:
            previous = None  # another folder's session only counts while it is fresh
        if not previous:
            previous = memory.ledger_last(db, session, RESUME_HOURS * 3600)
        tail = memory.last_session(db, session, RESUME_HOURS * 3600)
        learned = memory.learned_since(db, previous["updated"] if previous else time.time() - 7 * 86400)
        no_rules = not memory.rules(db, 2000)
        memory_rules_count = len(memory.rules(db, 20000))
        watches = insights.watch_summary(db)
        open_block = insights.open_block(db)
        found_new = insights.discovered_since_last(db)
        # After a compaction or resume the details of this session are no longer in view: bring back its working memory.
        own = memory.session_turns(db, session) if payload.get("source") in ("compact", "resume") else []
        own_ledger = memory.ledger_get(db, session) if own else None
        git_state(db, cwd)
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
    for snippet in discovery.readme_snippets():
        lines += [f"In the user's own words (README of a recent project): {snippet}"]
    if discovery.frontier_block():
        lines += ["", discovery.frontier_block()]
    level = discovery.expertise()
    lines += ["Write for an expert: paths, units and line numbers inline, mechanism stated plainly." if level == "expert" else
              "Write for a non-technical reader: consequence first in plain words, mechanism in one clause, paths in a trailing details line."]
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
            lines += [f"- Last exchange: {tail[-1]['text'][:600]}"]
    elif tail:
        lines += ["", "Where the user's last session left off:"] + [f"- {p['text'][:600]}" for p in tail]
    if attention:
        lines += ["", "On Lantern's Stage (open findings, most pressing first, checked within the last hour; a finding's text is its "
                  "evidence, cite it rather than saying you have not read the log):", attention]
    tail_block = []
    if moved:
        tail_block += [moved]
    if found_new:
        tail_block += [found_new]
    tail_block += [open_block]
    if not first:
        at = discovery.settings().get("insights_at")
        tail_block += [f"Lantern's watches (failed services, disk, unpushed work, known services, backup jobs) last ran at "
                  f"{time.strftime('%H:%M', time.localtime(at)) if at else 'this session start'}: {watches}. Duties beyond these "
                  "watches are proposals until the user says yes; say which are active and which are only proposed."]
        pending = discovery.settings().get("pending") or []
        if discovery.settings().get("answered_at") and not pending:
            tail_block += ["The setup questions were answered (guardrails and profile above are in force)."]
        elif discovery.settings().get("answered_at"):
            tail_block += ["The guardrails were accepted; what this machine is for was never confirmed: state your reading of it in one "
                           "line and ask once whether it is right (then store it with `remember --profile`)."]
        elif pending:
            tail_block += ["The setup questions from the first session were never answered; defaults apply (see open loop below)."]
    lines += ["", f"Now: {time.strftime('%Y-%m-%d %H:%M %Z')}."]
    lines += ["", (FIRST_RUN if first else EVERY_SESSION).format(entry=entry, guardrails=" | ".join(f'"{g}"' for g in GUARDRAILS))]
    if tail_block and not first:
        stored = memory_rules_count
        lines += ["", "SINCE LAST SESSION (computed by Lantern; open your first reply with one or two sentences built only from "
                  "these lines, using their counts and reasons word for word, then answer):"] + tail_block
        lines += [f"Rules already stored by Lantern: {stored} (listed above as the user's standing rules; nothing is pending a save). "
                  "A finding's journal lines above were read by Lantern's watcher: cite them as the cause; never say the log is unread."]
        if previous and previous.get("loops"):
            lines += ["Open loops from the last session, each to be done now, re-asked once, or retired with a reason: "
                      + "; ".join(previous["loops"])]
    return _context("SessionStart", "\n".join(lines))


def capture_last_session(cwd: str, current: str) -> None:
    """A session that ended without its Stop hook (a crash, print mode) still gets its final exchange captured: at the
    next session start in the same folder, the newest other transcript is read and stored if it was not already."""
    if not cwd:
        return
    folder = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
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
        last = db.execute("SELECT text FROM items WHERE kind='turn' AND source=? ORDER BY created DESC LIMIT 1", (session,)).fetchone()
        if last and last[0].startswith(head[:200]):
            return  # the Stop hook already captured it
        _store_turn(db, session, payload.get("cwd") or "", turn)
    finally:
        db.close()


def _store_turn(db, session: str, cwd: str, turn: dict) -> None:
    text = f"User: {turn['user'][:CAPTURE_CHARS]}\nAssistant: {turn['reply'][:CAPTURE_CHARS]}"
    acts = [f"edited {', '.join(turn['files'][-6:])}" if turn["files"] else "",
            f"ran {'; '.join(turn['commands'][-4:])}" if turn["commands"] else "",
            f"{turn['failed']} tool call(s) failed" if turn["failed"] else ""]
    if any(acts):
        text += "\nActions: " + "; ".join(a for a in acts if a)
    memory.add(db, "turn", session, shield.redact(text))
    memory.ledger_update(db, session, cwd, turn)


def user_prompt_submit(payload: dict) -> dict | None:
    prompt = payload.get("prompt", "")
    if not prompt.strip() or _is_system(prompt):
        return None
    capture_previous(payload)
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
    if touching:
        lines += ["Standing rules that touch this request (quote the one you apply, word for word):"] + [f"- {r}" for r in touching]
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
              "Ask before changing the operating system (rpm-ostree, system config, system-wide services)."]
SEEDED = {  # rules the machine itself implies; stored at the first session so nobody has to say them
    "/run/ostree-booted": "This is an immutable OS (ostree): never layer packages or edit /usr; use Flatpak, Homebrew or containers.",
}
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
        for rule in GUARDRAILS:
            memory.add(db, "rule", "setup", rule)
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


def stop(payload: dict) -> dict | None:
    turn = transcript.last_turn(payload.get("transcript_path", ""))
    if not turn["user"] or _is_system(turn["user"]):
        return None
    db = memory.connect()
    try:
        _store_turn(db, payload.get("session_id", "?"), payload.get("cwd") or "", turn)
    finally:
        db.close()
    return None


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


HANDLERS = {"session-start": session_start, "user-prompt-submit": user_prompt_submit, "stop": stop,
            "subagent-stop": subagent_stop, "pre-tool-use": shield.pre_tool_use}


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
    reply = handler(payload)
    if name != "pre-tool-use":  # every prompt-path hook reports how long it took; the doctor watches the trend
        _log("hooks", {"hook": name, "ms": round((time.monotonic() - t0) * 1000)})
    if reply:
        stdout.write(json.dumps(reply))
    return 0
