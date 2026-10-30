# Lantern

**A personal agent harness for Claude Code that remembers, learns and looks after itself.**

Install it once. From then on Claude Code knows your machines, your projects and how you like things done, learns a rule the moment you state it, picks up where the last session left off, and checks and repairs itself every day. You never tinker with it. One line tells you it is working.

> Status: pre-release. The core works and is tested on Linux, macOS and Windows; the first public release is being prepared.

## Install

```sh
claude plugin marketplace add github.com/DaBarkle/lantern
claude plugin install lantern@lantern
```

Lantern needs [uv](https://docs.astral.sh/uv/), which fetches its own Python. Your first session offers a two-minute setup (`/lantern:setup`). That is all.

## What you get

| | What happens | What it means for you |
| --- | --- | --- |
| **Memory** | Every turn is kept: what you said, what Claude did, which files and commands it touched, what agents found. One SQLite file, yours. | Nothing you worked on is forgotten. |
| **Context on every prompt** | Session start loads your profile, standing rules, how your systems are worked on, a one-line index of everything known, and where the last session in this folder left off. Each prompt brings in the few notes that match, newest state first. | No re-explaining. Claude starts informed. |
| **Learning** | A correction ("don't restart X without asking") is a rule from that moment. Once a day, one small model call turns the day's sessions into facts, patterns and profile, replacing anything that changed. | It gets better with every task. |
| **Discovery** | Within the depth you choose, Lantern finds your repos, tools, containers, services and local servers, remembers them and suggests the packs that add skills for them. | Your environment, known without onboarding. |
| **Stage** | Open findings in one place: the doctor's, uncommitted work left in a project, new things found. Each fades and expires on its own. | You hear about what matters, once. |
| **Doctor** | Daily, and on demand: recall rate, memory freshness, hook latency, secret shield, each accepted capability. It repairs what it safely can. | You do not wonder whether it is working. |
| **Shield** | Raw secrets never enter a command, a file or a note. Values live in the OS keychain and reach commands as environment variables. | Nothing leaks into memory. |
| **Approvals** | Questions that matter go to your phone (ntfy), outbound only, with a confirmation back. | Say yes from anywhere. |
| **Status** | `lantern status`: *3 notes recalled last turn · 2,484 things known · learned 4 today · all well*. | One plain line, no readouts. |

No model call ever sits on the prompt path. A prompt costs Lantern about 50 milliseconds.

## Commands

| Command | What it does |
| --- | --- |
| `/lantern:setup` | Two-minute conversational setup, offered in the first session |
| `/lantern:status` | Is it working? One plain line, and the fix if not |
| `/lantern:profile` | Everything Lantern believes about you and your rules |
| `/lantern:recall <topic>` | What memory holds on a topic |
| `/lantern:remember <text>` | Add something yourself |
| `/lantern:forget <words>` | Drop something |
| `/lantern:discover` | See what was found on this machine |
| `/lantern:depth` | How much of the machine Lantern may look at (1 conversations, 2 workspace, 3 home, 4 life) |
| `/lantern:doctor` | Full self-check |

## Packs

Packs add skills for the systems you run and can enforce your rules structurally. Each is its own plugin in this marketplace: `lantern-docker`, `lantern-media-server` (Plex, Jellyfin, the *arr apps), `lantern-home-assistant`, `lantern-unifi`, `lantern-tailscale` (read-only; it blocks any command that would change your tailnet). Discovery tells you which ones fit.

## How it works

Lantern is four Claude Code hooks and one background process.

- `SessionStart` loads context, starts due jobs (curation, doctor, discovery) detached, and puts the folder's git state on Stage.
- `UserPromptSubmit` runs recall: full-text search and semantic search fused by rank, weighted by kind and recency, gated so a prompt with nothing in memory gets nothing. A correction becomes a rule here.
- `Stop` and `SubagentStop` capture the turn, the tools it used and what agents found, into memory and the session ledger.
- `PreToolUse` is the shield.
- A memory server keeps the embedding model warm on localhost, token-protected, one per data folder.

Data lives in the plugin's data folder (or `$LANTERN_HOME`). A folder can keep its own memory apart from the rest: put `{"profile": "work"}` in a `.lantern.json` there (or in a parent), and sessions in it get their own profile, rules, discovery and Stage. Nothing leaves your machine except the one daily curation call to Claude and, if you enable it, approval questions to ntfy.

## Develop

```sh
cd plugins/lantern
uv run --no-project python -m unittest discover -s tests -t .
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Lantern is Apache-2.0; see [LICENSE](LICENSE).
