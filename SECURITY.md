# Security

Lantern runs on your machine with your user's rights, reads your files to build memory, and stores what it learns in one SQLite file in its data folder. These are the lines it holds:

- **Secrets never enter memory, commands, notes or the model.** The shield stops a pasted secret before the model sees it, stores it in the OS keychain, and hands it to commands only as an environment variable. Manager items (1Password, Bitwarden, pass, KeePassXC) are kept by reference and read through that manager's own CLI at run time.
- **Nothing leaves the machine** except one daily curation call to Claude (the day's turns, with secrets already shielded) and, only if you turn it on, approval questions to your ntfy topic.
- **The memory server listens on 127.0.0.1 only** and answers only requests carrying the token from your data folder (mode 0600).
- **Discovery stays within the depth you chose**, and pack guards fail closed.

## Reporting

If you find a way a secret can reach memory, a command, a note or the model, or a way past a pack guard, please report it privately to the repository owner through GitHub's private vulnerability reporting (Security tab) rather than a public issue. Include the exact input and the Lantern version from `/lantern:status`. You will get a reply within a week.
