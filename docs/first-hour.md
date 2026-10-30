# The first hour

Lantern's first hour is designed backwards from "AGI in a harness": the user should feel, within minutes, that something
already understands their machine and is quietly working for them.

**Before the first reply (seconds).** Discovery looks at the workspace: the machine (OS, CPU, memory, GPU), projects with
remotes and last commits, dev tools, password managers. Insights turn that into findings the user may not know: projects
with uncommitted or unpushed work, active projects with no remote, long-untouched projects, disks running short,
failed units, known services that are down. A read-only survey agent starts in the background (one process, capped
turns and time, stands down under memory pressure).

**Fast path (clients with an Agent tool).** Up to three read-only explore agents run in parallel at first contact (services, timers and containers; projects and backups; secrets and exposure) and their findings join the first reply; the background survey still runs for the full picture.

**The first reply.** Who Lantern is in two sentences; what it found as a table; the findings as "things you may not know";
one structured question: look wider (read-only), take on the responsibilities the findings suggest, approvals on the
phone. Secrets are a line, not a step: `secret NAME: value` in chat goes to the keychain and never reaches the model.
Then two questions for the profile (what the machine is for; what to be careful about), and a closing table of what
Lantern now does, with one concrete thing it can do right now.

**A few minutes in.** The survey finishes and announces itself on Stage: Lantern's understanding of the machine, what is
worth knowing (with the files that show it), what it proposes to take on, and a service map as a table and an indented
tree (mermaid is not rendered in Claude Code's desktop tab, so Lantern never relies on it).

**Session two.** Opens with what was learned since, the survey's understanding, where the last session left off, and
the findings that still stand. Nothing to run, nothing to configure.

Everything above is deterministic except the survey; nothing sits on the prompt path; the user never runs a command.
