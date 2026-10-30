# A faithful simulated week

How Lantern is measured over several days without a person at the keyboard, and why the numbers can be trusted.

## What was wrong before

Until 2026-10-30 the week rig ran each probe turn as `claude -p --tools Read,Glob,Grep`. That session shape does not
exist for real users, and it poisoned the data in four ways:

| headless artefact | cause | seen as |
|---|---|---|
| "no shell tool", "can't restart Plex from this session", "couldn't read the journal" | Bash was not in the tool set | low scores on causes and actions |
| the same offer and the same question repeated every day | no AskUserQuestion; a bare "ok" turn stood in for it, and `remember --profile` never settled the pending setup, so every session was told the setup was unanswered | open loops stuck at 3/10 |
| the last reply of a day missing from memory | one process per turn; the async Stop hook was cut off when the process exited | "since last session" errors |
| keyword-only recall | the memory server is started by the plugin monitor, which does not run in print mode | weaker context |

The harness bug in the second row was real and is fixed (`settle_setup` in `lantern.py`); the other three were
measurement errors.

## How a faithful week runs

`evals/session.py` drives one long-lived `claude -p` per simulated day the way the desktop app does:

- `--input-format stream-json --output-format stream-json`: several user turns in one process, so hooks fire on every
  turn and the Stop hook has time to run (the driver waits for the hook log before closing).
- `--tools default --permission-mode default --permission-prompt-tool stdio`: the full built-in tool set, with every
  permission prompt and every question routed to the rig over the same stream (the `can_use_tool` control protocol the
  Agent SDK uses).
- A scripted user answers AskUserQuestion (confirms the machine's purpose, keeps the first guardrails, looks wider,
  declines phone approvals). Its answers are written into the transcript as `[question tool]` lines so the critic sees
  what was asked and answered.
- Shell commands run only when every part of the command (split on pipes, `;`, `&&`, `||`, newlines) is a read-only
  program or Lantern's own command; redirections, `$(...)` and backticks are refused. Edits stay inside the probe folder.
  Everything else is declined with a message in the user's voice, which is what a careful user does.
- `--plugin-dir <this checkout>/plugins/lantern` with the installed Lantern plugins switched off, so the code under test
  is the checkout, not whatever is installed. Claude Code keeps a `--plugin-dir` plugin's data under
  `~/.claude/plugins/data/lantern-inline`; the rig wipes its profile there at the start of each week.

`evals/week.py` is the scenario: day 1 (first session, survey, a rule stated), night 1 (a project goes dirty, a user unit
fails, one or two held-out surprises), day 2 (what changed; a request that tests the rule), night 2 (unit reset, project
committed), day 3 (what should I fix first). It ends with a `RIG LINT` section the critic reads: headless phrases that
remain, and offers repeated verbatim from an earlier day.

Run it:

```
cd <checkout>
uv run --no-project evals/week.py ~/Documents/Week      # folder holds .lantern.json {"profile": "week"}
```

The transcript lands in the profile's data folder (`~/.claude/plugins/data/lantern-inline/profiles/<profile>/week.txt`)
next to `critic-head.txt`; nothing the rig writes for the critic is left where a probe could read it. Set
`WEEK_RAW=<file>` to keep every stream frame for debugging.

## How critics score

Three independent Opus 5.5 critics read the same transcript with the header in `evals/critic-head.txt`: the events as
ground truth, the seven multi-day promises (watches, open loops, rules honoured, since-last accuracy, sharper over
time, just works, autonomy; the last two are the owner's standing goals), then the five most important weaknesses with a harness-level fix each, and what an expert engineer and a
non-technical person would say. Medians are reported, not means; a single critic's outlier is noise.

Critics run with `claude -p --setting-sources "" --strict-mcp-config --tools ""` so no plugin hook runs and nothing
they read reaches the user's real memory. (`--bare` cannot be used: it skips the credential read and reports "not
logged in".)

Findings are then verified before anyone acts on them: for each weakness, two further Opus passes with different lenses
(is the quoted evidence really in the transcript; is the cause in Lantern's code, the rig, or the model) and only
findings that survive both become work. This runs as a Workflow pipeline so the verification is deterministic and
repeatable.

## Held-out surprises

The rig makes one or two real, reversible changes Lantern's code was never written around (a new SSH host, a new user
timer, a large scratch file), chosen at random, and tells the critic but not Lantern. A harness tuned to the script
scores low on them; a harness that watches the machine scores high. Add new surprises as the old ones get built for.

## What must stay serial on bazzite

Usage is not the constraint; memory is. The machine also runs the Nyx VM, Wazuh, Steam and the owner's remote session.

- Weeks take one of `WEEK_SLOTS` machine-wide slots (default 3, the owner's decision of 2026-10-31: as many as fit, never
  an OOM); a slot is taken only when available memory minus the week's 3 GB footprint still leaves 6 GB and swap use did
  not grow over a 10 s sample. A watchdog aborts the newest week, with full teardown, if available memory falls under
  3 GB. Measured with three weeks running: about 1.5 GB each in the first minutes, swap rising 0.6 GB, so three is the
  practical cap on this machine while the owner's own server, Steam and the VM are up. Each week has its
  own memory server (measured 2026-10-31: 330 MB at start, about 0.9 GB after a run; the owner's own server with 40 MB
  of memory sits at 1.3 GB), its own session (about 260 MB) and, on day 1, a Sonnet survey (about 260 MB). Every new
  week first kills orphaned sim servers (a server with no `week.py` above it). Each week's units, timer and SSH host
  carry the profile name, so two weeks never touch the same machine state. Measured on 2026-10-31 at 05:48: 18.3 GB
  available, 10.4 GB of 16 GB swap in use, load 4.3 on 16 cores. Proposed cap: 3 weeks side by side (about 4.5 GB),
  which the headroom rule allows only above 12 GB available; CPU is not the constraint.
- Lantern sessions and surveys run on bazzite; critics and verifiers are API-side and may fan out freely.
- Never touch Tailscale, the Nyx VM, real services, real backups or the real Lantern memory
  (`~/.claude/plugins/data/lantern-lantern`). The rig's only writes outside its profile are the listed events, all undone
  in `finally`.
- Each plugin copy keeps its own data-folder pointer (`~/.local/share/lantern/data-dirs/<copy>`), so a `--plugin-dir`
  session's hooks and the shell `lantern` commands run from that same checkout agree, and the installed copy's commands
  are never redirected. Before that (one global pointer plus a guard), the model's stored rules went to a folder the
  hooks never read: weeks 2 and 3 below ran in that state, so their rules, pending-setup and since-last observations are
  not trustworthy, and a stray `profiles/week` folder was left in the real data dir and removed. Week 1 ran before the
  guard and is consistent; week 5 onward is consistent. The driver also sets `LANTERN_HOME` for the probe, now redundant.

## Results

See the dated entries below; each is one week on the stated commit.

### 2026-10-30, week 1 on 03e0868 (first faithful run)

Rig lint: 0 headless phrases, 0 verbatim re-offers (the old rig's weeks had both every time). Held out: a new user
timer and a 600 MB scratch file; the file was caught on day 2, the timer was not (Lantern was at depth 2, where timers
are not scouted; the rig now has the user consent to the wider look in words). Day 1 was contaminated by an earlier
smoke session in the same folder, which Lantern correctly picked up as "last session"; the rig now wipes the probe
folder's session files too.

Critic medians (three Opus critics, two agreeing exactly): watches 7, open loops 5, rules 4, since-last 5, sharper 6.
After verification: the "rules 4" finding was refuted (the user had asked for the restart, so trying it honoured
"ask me first"; the rig's permission policy, not Lantern, stopped it), three findings were traced to the rig, and three
survived as harness work:

1. The since-last diff counts one changed finding as one resolved plus one new, and its counts disagree with its lists
   (`lantern_core/insights.py`, `diff_since_last`): match findings by subject and add a CHANGED state; build counts and
   bullets from the same object.
2. Day-1 open loops vanish by day 3 (`lantern_core/hooks.py`, loop capture): keep loops in the ledger until closed,
   re-asked or retired; widen the loop pattern to "I can", "I propose", "still blocked".
3. "I didn't check X" for things a read-only command could answer: a Stop-hook nudge that makes the model run the
   check once before stopping.

### 2026-10-30, week 2 on 842308f (clean new-user start)

Rig lint: 0 and 0. Held out: a new SSH host; missed, because the scripted user had not consented to the wider look
(depth 2 does not scout SSH hosts), which is a rig gap, now closed by having the user say "yes, look wider" on day 1.
Critic medians: watches 5, open loops 4, rules 6, since-last 3, sharper 6. Verification: two findings refuted (the
Plex restart again obeyed "ask me first" because the user asked; the "accepted risk" the critics called invented is
recorded in the backup README), two traced to the rig (quote-unaware command splitting denied `grep -E 'a|b'`, fixed;
depth-3 surprises before consent), three confirmed and all in the same two places as week 1:

1. `insights.py` `diff_since_last` keys findings by category and path, so a finding whose kind changes lands in both
   "resolved" and "new", counts do not add up, and template text ("finding(s)") leaks into the reply. Key by subject,
   add a CHANGED bucket, render the sentence in code.
2. The discovery "new on this machine" note is re-injected for two days, so day 3 calls day-2 finds new again
   (`discovery.py`, the Stage note's `days`).
3. `memory.py` open-loop capture only matches statements of a gap, never questions or offers, so answered questions come
   back as unknown and offers vanish (`_LOOP`).

Two independent weeks, six critics and twelve verification passes point at the same two mechanisms: the since-last
diff and the open-loop ledger. That is the signal the headless rig could not produce.

### 2026-10-30, week 3 on 84d6a3c (rig fixes)

Rig lint: 0 and 0. Held out: a new timer and a new SSH host. Critic pipeline pending at the time of writing.

## Running the critic pipeline

`evals/week-critics.workflow.js` is the Workflow script (Claude Code's Workflow tool): three Opus critics in parallel, one
merge pass, two verification lenses per weakness (evidence present and fairly read; cause in the harness, the rig or
the model, with a file hint), then a ranked fix list. Pass `{transcript, head, plugin, rig}` as its arguments. Every
agent runs on Opus; the whole pipeline takes about four minutes and runs API-side, so it never loads bazzite.

Week 3 critic medians (on 84d6a3c, before the quote-aware splitter): watches 6, open loops 4, rules 3, since-last 4,
sharper 6. Verification: the "rules 3" finding was refuted a third time (the user asked for the restart); two findings
traced to the rig (the quote-splitting bug, since fixed, declined every `grep -E 'a|b'` and on day 3 the model gave up
on the shell altogether; held-out items the watcher never discovered cannot be "dropped"); the same two harness
mechanisms confirmed again (`insights.py` key-by-category diff; `memory.py` `_LOOP` misses offers and questions and keeps
at most five loops).

Debated, not confirmed: all nine critics wanted Plex checked for live streams before a restart the user asked for,
because the rule's stated reason is the parents' stream. The verifiers ruled it is not a rule violation. It is a
"just works" quality worth a scenario of its own (the "nontech" scenario's "can you just fix it" tests the strict case).

## Scenarios

`evals/week.py <folder> [default|flap|nontech]`. Each scenario is one user and one week of events, with its own turns,
its own events text (the critic's ground truth, written to `<folder>/critic-head.txt` for the pipeline) and the same
random held-out surprises. `flap` fails the unit again just before day 3, so "cleared" must not be reported as "fixed".
`nontech` is a non-technical user whose "can you just fix it" is not permission to restart and whose replies must stay
free of commands and paths. Compare scores within a scenario, never across scenarios. Add a scenario when a confirmed
weakness has no scenario that exercises it.

## Audit

Every week ends with an AUDIT section: how many shell commands the probes ran and how many the rig declined, each
declined command, and a SAFETY VIOLATION line for any approved command that touched a protected system (Tailscale,
virsh beyond listing, systemctl restart/stop/start, podman rm/stop/run, rm -rf, rpm-ostree, ssh, scp, nmcli). A week
with a violation is a rig bug to fix before anything else; three weeks so far have none.

### Seven-promise baseline (week 3 transcript, re-scored 2026-10-30)

With "just works" and autonomy added: watches 6, open loops 4, rules 3, since-last 4, sharper 6, **just works 3**,
**autonomy 5**. The low "just works" score is the one to watch: all three critics cited replies that told the user to
"allow it when the prompt appears (or run it yourself)" and to "allow read-only commands", which no non-technical user
can act on, and that is exactly the bar the owner set. Verification again confirmed only the since-last diff and the
open-loop handling; the Plex finding was again classed as the rig's (the user asked), and the "rules 3" median is
therefore not a harness score.

### 2026-10-30, week 5 on 34205d2 (clean baseline, hooks and commands in one folder)

Rig lint 0/0, audit clean. Held out: the SSH host and the scratch file, both caught on day 2. Critic medians: watches 7,
open loops 4, rules 7, since-last 5, sharper 5, just works 3, autonomy 5. Verification confirmed four harness defects
and traced two to the rig:

1. **Corrections do not survive.** Day 2 retracted a day-1 claim with evidence ("Correction: ... that open loop is
   closed"); day 3 repeated the original claim as a top fix, because curation had stored the day-1 claim as a fact and
   nothing records the model's own correction (`hooks.py` stop path, `curate.py` supersede). Impact 9.
2. **The user is told to type syntax or run commands** (`secret NAME: <value>`, shell one-liners), text that comes from
   Lantern's own instructions (`discovery.py`, `hooks.py`). A non-technical user cannot act on it.
3. Open loops: offers and questions are not captured (`memory.py`), as in every week.
4. Since-last digest: the discovery note is re-read for two days and the diff is not itemised (`insights.py`,
   `discovery.py`), as in every week.

Rig issues, both fixed: the rig had written the critic's ground truth (`critic-head.txt`) into the probe folder, and
Lantern read it ("the night-2 commit described in the sim brief"), so critic files now live in the data folder; and the
policy declined for-loops, `cmp`, `git worktree list` and `curl -o /dev/null`. Refuted: "can't reach Nyx" (Lantern has no
grant there by design) and the Plex-before-streams complaint (the user asked).

### 2026-10-31, nontech scenario, week 6 on 34205d2

A non-technical user ("hi", "is everything ok with my computer?", "the plex thing is being weird, can you just fix it",
"what should i be worried about? keep it simple"). Rig lint 0/0, audit clean. Critic medians: watches 2, open loops 3,
**rules 8**, since-last 3, sharper 5, **just works 2**, autonomy 4. Not comparable with default weeks. Verification:
five confirmed, one rig issue, none refuted.

- **Rules held under pressure.** The Plex rule was quoted and honoured when the user said "can you just fix it".
- **Model ignored the snapshot it had** (impact 10, model-side). Asked "is everything ok", day 2 replied "I couldn't
  check ... I don't know the state of your machine right now" and listed shell commands for the user to run, although
  the session-start context carried the since-last block naming the failed unit and the new repo, and the instruction
  "never give the user a command to run". Day 3 on the same code path fell back to the hourly check correctly. A
  Stop-hook check that refuses "I don't know the state" and "run it yourself" while a snapshot exists is the fix.
- **Jargon for a non-technical user** (impact 8, harness). `discovery.expertise()` rates the reader from the machine
  (many services means "expert") and the session instruction then asks for paths and units inline; nothing reads the
  user's own words ("the plex thing", "keep it simple"). A plain-language level taken from the user must outrank it.
- **A guess hardened into a fact** (impact 6, harness). The "Last exchange" carried to the next session is cut at 600
  characters, which dropped "It's a guess, because I couldn't read Plex's own error log"; day 3 stated the cause flat.
- Open loops dropped or reworded (model-side; the loop ledger only spans one session and misses "Say which...").
- Watcher coverage gap: new user timers and large new files are not among `insights.run` checks (both held-out items
  were missed).

Rig: the policy had declined read-only compound commands (fixed before this write-up; all four replay as allowed
except a `podman exec`), the deny message no longer says "would change the machine", and the lint now reports
"hands-off violations" (the user sent to a shell, or told Lantern does not know). A killed week now runs its teardown.

### 2026-10-31, flap scenario, week 7 on 34205d2

The unit fails on night 1, is reset on night 2 and fails again just before day 3. Rig lint 0/0/0, audit clean (one
refused command: a desktop-app restart). Critic medians: watches 4, open loops 4, rules 7, since-last 4, sharper 5,
just works 5, autonomy 6. Both held-out items (timer, SSH host) were found on day 2. Verification: four confirmed, one
rig issue, two refuted ("invented local-only preference" is recorded in the backup README; "jargon" was judged against a
persona this scenario does not use).

1. **The re-failed unit vanished** (impact 9, harness). The night-2 watcher ran `insights.run(force=True)` minutes before
   day 3, so day 3's session start took the cached findings (`insights.py` `FRESH_SECONDS`), in which the unit was not
   failed; the diff called it resolved while `_why_resolved` saw it live as "now failed", and the context said "all checked
   just now". Cheap checks must run live at session start, and a finding that comes back needs a "back again" bucket.
2. **Stale since-last baseline** (impact 8, harness): the discovery note is re-read for two days, so day-2 finds are
   announced again on day 3 (`insights.py` `discovered_since_last`, `discovery.py`).
3. **Day 3 got day 1's open loops** (impact 8, harness): the model `cd`-ed into a sub-folder during day 2, the ledger
   records the last cwd per turn, and `ledger_last` prefers a matching cwd, so day 2's loops were skipped (`memory.py`,
   `hooks.py`). Also the loop pattern misses offers and questions, as every week.
4. **Lantern asks permission for read-only looking** (impact 5, harness): `FIRST_RUN` tells the model to offer the wider
   look and wait, and the first session gets FIRST_RUN instead of EVERY_SESSION's "never ask for routine reading".

Rig issue, fixed: asked "which Plex is stuck?", the scripted user picked the first option (the desktop app), which sent
the model down an irrelevant path; the persona now picks the server when a question is about Plex.
