# Lantern

Lantern is a personal agent harness for Claude Code. It remembers what you work on, brings the right past context into every prompt, discovers the systems you run and sets up integrations for them, and checks and repairs itself, without you tinkering under the hood.

> Status: early development. Not ready for use yet.

## What it does

- **Remembers** every turn, what tools touched (files, commands, failures) and what agents found, in one SQLite file per user.
- **Brings back** your profile, standing rules and a one-line index of notes at session start, the right notes on each prompt, and where the last session in this folder left off.
- **Learns at once**: a correction ("don't restart X without asking") is a rule from that moment; nightly curation turns the day's turns into rules, facts and profile, replacing facts that changed.
- **Stays honest**: a doctor checks recall rate, memory freshness, hook latency and the shield, and keeps Stage (open findings) in step; uncommitted work left in a project goes on Stage.
- **Stays yours**: `/lantern:profile`, `/lantern:recall`, `/lantern:remember`, `/lantern:forget` show and fix what it believes. Secrets never enter a command or a note.
- **No model call on the prompt path.** One small call a day for curation.

## Install (development)

```sh
claude plugin marketplace add ./lantern
claude plugin install lantern@lantern
```

Lantern needs [uv](https://docs.astral.sh/uv/), which fetches its own Python. It runs on Linux, macOS and Windows.

## Develop

```sh
cd plugins/lantern
uv run --no-project python -m unittest discover -s tests
```

## License

Apache-2.0. See [LICENSE](LICENSE).
