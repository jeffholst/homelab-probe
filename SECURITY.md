# Security policy

Homelab Probe holds an API key for your UniFi controller and reads real data about your network, so a report about how it handles either is welcome.

## Reporting a vulnerability

**Report it privately.** Open the repository's [Security tab](https://github.com/jeffholst/homelab-probe/security) and choose **Report a vulnerability**, which opens a private advisory that only you and the maintainer can see. Please do not open a public issue or pull request with the details, and do not post them in a discussion.

If that button is not offered, open a public issue titled **Security contact request** with no details in it (not even what the problem is), and the maintainer will arrange a private channel.

This is a one-person project, so replies and fixes are best effort. A confirmed problem is fixed on `main` first and then in a new release, and the [changelog](CHANGELOG.md) says so under **Security**.

### What to include

- The version (`hlp --version`), the command and options, and how you installed it.
- What happens, what you expected, and the smallest steps that show it. A synthetic example (a device named `=HYPERLINK(...)`) is better than a real network.
- **Redact before you send:** remove real MAC addresses, IP addresses, host names, SSIDs, site IDs and your controller's address from anything you paste, `--verbose` output included.
- **Never send your API key or your `.env` file.** If a key has been pasted anywhere public by mistake, revoke it under **Settings > Control Plane > Integrations** and create a new one.

## Supported versions

The latest release and `main`. The project is 0.x: a fix goes into the next release, not into older ones. Only UniFi Network 10.6.106 has been tested (see the [README](README.md#requirements)).

## What is in scope

- **The API key and other secrets:** anything that prints, logs or sends the API key, a notification URL or token, or the mail account, including error messages, `--verbose` output, JSON output and notification messages.
- **The read-only guarantee:** any request to the controller other than a GET (the one approved exception is the read-only event-log query, see [the one POST](docs/network.md#the-one-post-and-why-it-is-safe)), or any way to make the tool change something on the controller.
- **Output safety:** names that come from devices on your network (clients, devices, SSIDs, event text) that can break the terminal output, forge lines, or turn into a spreadsheet formula in a CSV file.
- **Where data goes:** a notification that carries more than the finding identity, severity and text, that follows a redirect, or that is sent without verified TLS; a certificate check that is skipped without `UNIFI_VERIFY_SSL=false`.
- **Web access:** authentication, session cookies, roles, CSRF and origin checks, setup-token authorization, host restrictions and proxy trust; a bypass that exposes data or permits an unauthorized action is in scope.
- **Local files:** `.env` lookup through `--env-file`, `HLP_ENV`, the CLI's current directory or the server's data directory; settings, accounts, notes, triage, snapshots and notification state, which are meant to be readable by the owner only. Static-file serving must not expose these files or escape the browser bundle.
- **Encrypted backups and restore:** secret disclosure, encryption failures, unsafe archive paths, unauthorized export or restore, and failures of restore validation or recovery that compromise application data. Never include a real backup or its passphrase in a report.
- **The supply chain of this repository:** the GitHub workflows (the release workflow is the only one that can write), the dependency lockfile and what gets built and published.

## What is not in scope

- The UniFi controller, its apps and its firmware: report those to Ubiquiti.
- Choices you made on purpose and the documentation warns about: `UNIFI_VERIFY_SSL=false`, `ALLOW_INSECURE_HTTP=true`, an API key with more access than read-only, a `.env` file other users can read (the tool warns about it).
- Anyone who already has your user account or your `.env` file: the key is protected by file permissions only.
- A controller that is itself hostile or broken (slow, enormous or malformed answers), unless the tool leaks something or runs something because of it.
- Wrong or missing findings: those are ordinary bugs, so use a [bug report](https://github.com/jeffholst/homelab-probe/issues/new/choose).
