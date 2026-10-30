# Lantern

**A personal agent harness for Claude Code that remembers, learns and looks after itself.**

Install it once. From then on Claude Code knows your machines, your projects and how you like things done, learns a rule the moment you state it, picks up where the last session left off, and checks and repairs itself every day. You never tinker with it. One line tells you it is working.

> Status: pre-release. The core works and is tested on Linux, macOS and Windows; the first public release is being prepared.

## Install

```sh
claude plugin marketplace add github.com/DaBarkle/lantern
claude plugin install lantern@lantern
```

Lantern needs [uv](https://docs.astral.sh/uv/), which fetches its own Python. Say hello: the first reply already knows the machine, tells you what you may not know, and asks two questions. That is all. See [docs/first-hour.md](docs/first-hour.md).

## What you get

| | What happens | What it means for you |
| --- | --- | --- |
| **Memory** | Every turn is kept: what you said, what Claude did, which files and commands it touched, what agents found. One SQLite file, yours. | Nothing you worked on is forgotten. |
| **Context on every prompt** | Session start loads your profile, standing rules, how your systems are worked on, a one-line index of everything known, and where the last session in this folder left off. Each prompt brings in the few notes that match, newest state first. | No re-explaining. Claude starts informed. |
| **Learning** | A correction ("don't restart X without asking") is a rule from that moment. Once a day, one small model call turns the day's sessions into facts, patterns and profile, replacing anything that changed. | It gets better with every task. |
| **Discovery and insight** | Before your first reply Lantern has looked at the machine (OS, hardware, projects with remotes and last commits, tools, password managers; wider on request: containers, VMs, services, apps, SSH hosts, local servers) and worked out what you may not know: projects with uncommitted or unbacked work, disks running short with what could be freed, failed units with the telling log line, services down, secrets sitting in unit files (by name). Findings are re-checked at every session start. Where it hits a wall (a host it has no key for, a service with no API key, a manager not signed in, a refused command) it records a frontier: what is there, why it cannot pass, what passing it would unlock, and a one-line ask, shown greyed in the service map and raised only when a task needs it. | It tells you things about your own machine, and knows the edge of what it can see. |
| **Survey** | Once, a read-only background agent reads the machine's own files and writes a brief: what the machine is for, how it is set up (service map), what is worth knowing, what Lantern should take on, who you are. Progress shows on every turn; the brief arrives in the conversation the moment it is done. | Real understanding, a few minutes in. |
| **Stage** | Open findings in one place: the doctor's, uncommitted work left in a project, new things found. Each fades and expires on its own. | You hear about what matters, once. |
| **Doctor** | Daily, and on demand: recall rate, memory freshness, hook latency, secret shield, each accepted capability. It repairs what it safely can. | You do not wonder whether it is working. |
| **Shield** | Raw secrets never enter a command, a file, a note or the model. When a key is needed Claude asks for it in one plain sentence; you paste it as your next message, on its own, and Lantern puts it in the OS keychain and stops that message before the model sees it (typing `secret NAME: value` works too). Commands get it as an environment variable. Items in 1Password, Bitwarden, pass or KeePassXC are linked by reference (`op://`, `bw://`, `pass://`, `keepass://`) and read through that manager's own CLI only when a command runs; Lantern never installs a manager for you. A session with no device keychain stops a pasted secret all the same and keeps it nowhere. | Nothing leaks, and you never leave the conversation. |
| **Approvals** | Questions that matter go to your phone (ntfy), outbound only, with a confirmation back. To reach another host Lantern makes its own SSH key and asks once on the phone before installing it; nothing is typed and your keys stay where they are. The same topic carries one line when a watch trips while no session is open (a failed service, a service down, a disk running short, a backup missed or failed) and one line when it clears; never a repeat. | Say yes from anywhere, and hear about trouble while away. |
| **Status** | `lantern status`: *3 notes recalled last turn · 2,484 things known · learned 4 today · all well*. | One plain line, no readouts. |

No model call ever sits on the prompt path. A prompt costs Lantern about 50 milliseconds.

## Commands

| Command | What it does |
| --- | --- |
| `/lantern:setup` | Revisit the setup choices (it happens by itself in the first session) |
| `/lantern:status` | Is it working? One plain line, and the fix if not |
| `/lantern:profile` | Everything Lantern believes about you and your rules |
| `/lantern:recall <topic>` | What memory holds on a topic |
| `/lantern:remember <text>` | Add something yourself |
| `/lantern:forget <words>` | Drop something |
| `/lantern:discover` | See what was found on this machine |
| `/lantern:depth` | How much of the machine Lantern may look at (1 conversations, 2 workspace, 3 home, 4 life) |
| `/lantern:doctor` | Full self-check |

## Packs

Packs add skills for the systems you run and can enforce your rules structurally. Each is its own plugin in this marketplace: `lantern-docker` (asks before anything that deletes containers, images or volumes), `lantern-media-server` (Plex, Jellyfin, the *arr apps), `lantern-home-assistant`, `lantern-unifi`, `lantern-tailscale` (read-only; it blocks any command that would change your tailnet). Discovery tells you which ones fit.

## How it works

Lantern is four Claude Code hooks and one background process.

- `SessionStart` loads context, starts due jobs (curation, doctor, discovery) detached, and puts the folder's git state on Stage.
- `UserPromptSubmit` runs recall: full-text search and semantic search fused by rank, weighted by kind and recency, gated so a prompt with nothing in memory gets nothing. A correction becomes a rule here.
- `Stop` and `SubagentStop` capture the turn, the tools it used and what agents found, into memory and the session ledger.
- `PreToolUse` is the shield.
- A memory server keeps the embedding model warm on localhost, token-protected, one per data folder. It exits after half an hour without a search and comes back on the next session or prompt.

Data lives in the plugin's data folder (or `$LANTERN_HOME`). A folder can keep its own memory apart from the rest: put `{"profile": "work"}` in a `.lantern.json` there (or in a parent), and sessions in it get their own profile, rules, discovery and Stage. Nothing leaves your machine except the one daily curation call to Claude and, if you enable it, approval questions to ntfy.

## Develop

```sh
cd plugins/lantern
uv run --no-project python -m unittest discover -s tests -t .
```

Does it help *you*? Score Lantern on your own transcripts:

```sh
uv run --no-project evals/replay.py        # 30 of your recent prompts; about 90 short Opus calls
```

It builds a scratch memory from what you said before those prompts, has the same model answer each one with and
without Lantern's context, and a blind judge (claude-opus-5-5) grades both against what you actually said next: how
often you would have had to re-teach, how often the reply was wrong about you, and a 1-5 score. Your real Lantern data
is never touched. `evals/week.py` simulates a week of sessions against a scratch profile.

See [CONTRIBUTING.md](CONTRIBUTING.md). Lantern is Apache-2.0; see [LICENSE](LICENSE).
