# Contributing to Lantern

Thanks for helping. Lantern is small on purpose; please keep it that way.

## Principles

- **No model call on the prompt path.** The prompt hook finishes in about 150 ms today and the doctor warns past its budget; anything that needs a model runs in a daily job.
- **Deterministic where possible.** Capture, ledger, recall, discovery and the doctor are plain code with tests. One small model call a day does the curation.
- **The user never tinkers.** A feature that needs a setting, a command or a manual step as its normal path is not finished. Prefer self-repair to a report.
- **Nothing hard-wired.** No paths, hosts or service names from one machine in core. Machine-specific knowledge goes in memory or in a pack.
- **Consent boundaries hold.** Discovery is read-only and the user can narrow it ("look less"); nothing that changes the machine runs without the user. Guards (like the Tailscale pack's) fail closed.
- **Secrets never enter memory, commands or notes.** The shield is built in, not configured.

## Layout

```
plugins/lantern/            the core plugin
  lantern.py                command line and hook entry (uv script, no dependencies)
  serve.py                  memory server (fastembed, numpy)
  lantern_core/             memory, recall hooks, ledger, curation, stage, shield (keychain and manager bridges), doctor, discovery (and frontiers), jobs, approvals, sshkey
  commands/                 /lantern:* commands
  tests/                    unittest, run with uv
plugins/lantern/packs/      packs: one folder per system, SKILL.md plus an optional guard.py
evals/                      replay.py scores recall on your own transcripts; week.py simulates a week (both need Claude Code)
.github/workflows/ci.yml    tests on Linux, macOS and Windows
```

## Making a change

1. Read the module you are changing and its test. Every behaviour has one.
2. Make the smallest change that fully works. Delete before you add.
3. Add or extend one test that fails without your change.
4. Run the tests: `cd plugins/lantern && uv run --no-project python -m unittest discover -s tests -t .`.
5. Write a commit message that says what changed for the user and why.

A shortcut with a known limit is fine when marked in the code as `shortcut: <the limit>, <when to upgrade>`.

## Packs

A pack is a folder under `plugins/lantern/packs/` with a `SKILL.md` (frontmatter `name: lantern-<folder>`) and, optionally, a `guard.py` with `check(command) -> reason | None` and `DECISION = "ask" | "deny"`. Add the finds that switch it on to `TRIGGERS` in `lantern_core/packs.py`. Switching on copies the skill into the user's skills folder (Claude Code watches it, so it appears in the running session) and the core `PreToolUse` hook runs the guard; nothing is installed and no restart is needed. Keep skills read-first: diagnose before changing, and stage any change for the user's approval; see `packs/tailscale/guard.py` and `packs/docker/guard.py`. `tests/test_packs.py` checks every pack's skill (read-first order, no terminal step for the user, secrets by name), that each guard enforces what its skill text promises, and that switching on and off works without a restart.

## Reporting a problem

Run `/lantern:doctor` and include its output. Say what you expected Lantern to know or do, and what it did instead.
