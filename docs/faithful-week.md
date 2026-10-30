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

The transcript lands in `~/Documents/Week/week.txt`. Set `WEEK_RAW=<file>` to keep every stream frame for debugging.

## How critics score

Three independent Opus 5.5 critics read the same transcript with the header in `evals/critic-head.txt`: the events as
ground truth, the five multi-day promises (watches, open loops, rules honoured, since-last accuracy, sharper over
time), then the five most important weaknesses with a harness-level fix each, and what an expert engineer and a
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

- One simulated week at a time, one memory server (about 330 MB) per probe profile, started by the rig and killed by it
  with the port checked closed. The rig refuses to start under 6 GB of available memory.
- Lantern sessions and surveys run on bazzite; critics and verifiers are API-side and may fan out freely.
- Never touch Tailscale, the Nyx VM, real services, real backups or the real Lantern memory
  (`~/.claude/plugins/data/lantern-lantern`). The rig's only writes outside its profile are the listed events, all undone
  in `finally`.
- The hooks rewrite `~/.local/share/lantern/data-dir` to the inline data folder while a `--plugin-dir` session runs;
  the rig restores it, but shell `lantern` commands typed during a run would see the sim profile.

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
