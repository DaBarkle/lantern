# Contributing to Lantern

Thanks for helping. Lantern is small on purpose; please keep it that way.

## Principles

- **No model call on the prompt path.** Hooks must finish in well under 100 ms. Anything that needs a model runs in a daily job.
- **Deterministic where possible.** Capture, ledger, recall, discovery and the doctor are plain code with tests. One small model call a day does the curation.
- **The user never tinkers.** A feature that needs a setting, a command or a manual step as its normal path is not finished. Prefer self-repair to a report.
- **Nothing hard-wired.** No paths, hosts or service names from one machine in core. Machine-specific knowledge goes in memory or in a pack.
- **Consent boundaries hold.** Discovery stays within the depth the user chose. Guards (like the Tailscale pack's) fail closed.
- **Secrets never enter memory, commands or notes.** The shield is built in, not configured.

## Layout

```
plugins/lantern/            the core plugin
  lantern.py                command line and hook entry (uv script, no dependencies)
  serve.py                  memory server (fastembed, numpy)
  lantern_core/             memory, recall hooks, ledger, curation, stage, shield (keychain and manager bridges), doctor, discovery (and frontiers), jobs, approvals, sshkey
  commands/                 /lantern:* commands
  tests/                    unittest, run with uv
plugins/lantern-*/          packs: skills, optional guards
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

A pack is a plugin with a `skills/` folder and, optionally, a `hooks/` guard. Keep skills read-first: diagnose before changing, and stage any change for the user's approval. A guard is a `PreToolUse` hook with a tested deny or ask list; see `plugins/lantern-tailscale/guard.py` and `plugins/lantern-docker/guard.py`. `tests/test_packs.py` checks every pack's skill (read-first order, no terminal step for the user, secrets by name) and that each guard enforces what its skill text promises.

## Reporting a problem

Run `/lantern:doctor` and include its output. Say what you expected Lantern to know or do, and what it did instead.
