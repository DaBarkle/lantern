# Lantern

Lantern is a personal agent harness for Claude Code. It remembers what you work on, brings the right past context into every prompt, discovers the systems you run and sets up integrations for them, and checks and repairs itself, without you tinkering under the hood.

> Status: early development. Not ready for use yet.

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
