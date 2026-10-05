# Web accounts

The web interface is built in stages (see the roadmap in issue #160). This page covers the first stage: the **accounts** that will be allowed to log in, and `web-user`, the command that manages them. It needs nothing beyond the base install, and nothing here contacts the controller or reads your `.env`.

## Roles

| Role | May |
| ---- | --- |
| `viewer` | See every read-only report, change their own password |
| `admin` | Also edit settings, take snapshots and manage users |

The **last enabled administrator cannot be deleted, demoted or disabled**, by `web-user` or by anything else that uses the accounts module, so the interface cannot lock everybody out. A disabled administrator does not count.

## Managing accounts: `web-user`

```bash
uv run hlp.py web-user list
uv run hlp.py web-user add alice --role admin             # asks for the password twice
echo 'a long password here' | uv run hlp.py web-user add bob --password-stdin
uv run hlp.py web-user set-role bob --role admin
uv run hlp.py web-user disable bob
uv run hlp.py web-user enable bob
uv run hlp.py web-user reset-password bob          # asks for the new password; --password-stdin reads it from a pipe
uv run hlp.py web-user delete bob --data-dir /srv/hlp
```

| Action | What it does |
| ------ | ------------ |
| `list` | The users with their role, status, creation time and last login |
| `add NAME` | Creates a user (`--role viewer` by default) |
| `set-role NAME --role ROLE` | Changes the role |
| `disable NAME` / `enable NAME` | Stops or allows logins without deleting the account |
| `reset-password NAME` | Sets a new password: this is also the **recovery path** when an administrator forgets theirs (run it on the host, or with `docker exec` in a container) |
| `delete NAME` | Removes the account |

Options:

- **`--role viewer|admin`**: for `add` (default `viewer`) and `set-role`.
- **`--password-stdin`**: for `add` and `reset-password`: read the password from one line of standard input. Without it the command asks for the password twice, with nothing echoed. **A password is never an argument**, so it cannot end up in the process list or a shell history.
- **`--data-dir DIR`**: where `users.json` and `audit.log` are kept (default: the current directory, like `.env`, `hlp.toml` and `snapshots/`).

A username has 3 to 64 characters (letters, digits and `. _ @ -`, starting with a letter or digit) and capitals do not matter: `Alice` and `alice` are one user. A password has **at least 12** and at most 1024 characters; there are no other rules (a long phrase is better than a short mixed one).

Exit codes are the usual ones: `0` done, `3` refused (a user that does not exist or already exists, a password that is too short, the last administrator, an accounts file that cannot be read), `64` a command-line usage error. `--demo` refuses the command, since it works on your own files.

## The files

Both live in the data directory, readable by the owner only (`0600`, and `0700` for a directory the command creates), and are git-ignored.

- **`users.json`**: the accounts. Each has `username`, `role`, `password`, `created_at`, `last_login` and `disabled`. A password is stored only as `scrypt$N$r$p$salt$hash` (a per-user random salt; the cost parameters travel with the hash, so they can be raised later and a user's hash is **upgraded at their next login**). Every write is atomic (a temporary file, then a rename) under a lock file `users.json.lock`, so a crash never leaves half a file and two writers never lose each other's change. A program that has the file open notices when it changed and reads it again. Existing files are tightened to owner-only permissions when read. A damaged file is refused with a reason and never overwritten.
- **`audit.log`**: one JSON line per change: `time`, `event`, `actor` (`cli:` and the operating-system user), the `user` and, where it applies, the `role`.

  | Event | When |
  | ----- | ---- |
  | `user.added` | `add` |
  | `user.role_changed` | `set-role` |
  | `user.disabled`, `user.enabled` | `disable`, `enable` |
  | `user.password_reset` | `reset-password` |
  | `user.password_upgraded` | a login replaced an old hash with one made with the current parameters |
  | `user.deleted` | `delete` |

  A password never reaches the file (a field named like a secret is hidden whatever it holds), and a refused change is not recorded because nothing changed. A failed audit write is reported and the corresponding account change is rolled back. Login and settings events arrive with the features that make them. The same records are also logged at `INFO` as `audit.event` on `homelab_probe.audit` ([logging](logging.md)).

  The log **rotates by size, never by age**: `AUDIT_LOG_MAX_MB` (default 5, 1 to 1024) is the size of one file and `AUDIT_LOG_FILES` (default 10, 2 to 1000) how many are kept in all, the current `audit.log` and `audit.log.1`, `audit.log.2`... `web-user` reads both from the environment (not from `.env`, which it does not read); they are also settings of `.env` for the server to come.

## What is not here yet

The login page, sessions and the server itself come with later stages of the roadmap. The accounts module already has the interface they will use: an `Authenticator` that turns a username and password into a `Principal(username, role, source)`, with `LocalAccounts` (this page's accounts) as the first implementation. A wrong password, an unknown user and a disabled one all take the same work and give the same answer, so the answer does not reveal which usernames exist. Authentication checks the current account record and records the login under the same file lock, so a concurrent disable, role change or deletion cannot return a stale principal.
