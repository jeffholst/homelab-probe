# Inventory, queries and exports

The commands that list and export what is on the network: `query`, `export`, `snapshot` and `diff`, `new-clients`.

## Devices

`query devices` shows each UniFi device with its firmware version, whether a firmware update is available, and its uptime (for example `2d 7h`). Offline devices have no uptime. `--json` adds `Uptime (s)` with the raw seconds. These columns come from the Integration API and appear only for `query devices`; the `export` CSV columns are unchanged.

## Snapshots and diff

When something breaks, the first question is "what changed since it last worked?". `snapshot` saves the inventory, and `diff` compares.

```bash
uv run unifi-sentinel.py snapshot                         # now: ./snapshots/snapshot-20261001-011530Z.json
# ...later, when something is wrong...
uv run unifi-sentinel.py diff                             # the newest snapshot against the network right now
uv run unifi-sentinel.py diff --last-two                  # the two newest snapshots (no controller needed)
uv run unifi-sentinel.py diff snapshot-20260929-080000Z.json   # a named snapshot against now
uv run unifi-sentinel.py diff OLD.json NEW.json           # two files
```

```text
Comparing snapshot-20261001-011530Z.json (captured 2026-09-30 20:15) -> the network right now

Devices
  Firmware changed (1):
    Office Switch: 7.0.0 -> 7.1.0
  State changed (1):
    Garage AP: Online -> Offline

Clients
  New clients (1):
    newcomer (10.0.0.19, Wireless)
  IP changed (1):
    printer: 10.0.0.50 -> 10.0.0.77

3 change(s)
```

- **What is saved:** devices (name, IP, model, type, firmware, state, uplink and port), every client the controller knows (name, IP, wired or Wi-Fi, online status, network and VLAN, the device and port it is on, client groups by name) and DHCP reservations, plus the site and controller version. It is built from the same rows the other commands print, not raw API data, and leaves out values that change constantly (uptime, last-seen times, traffic), so a diff shows real changes.
- **What diff reports** (matching by MAC address): new and missing devices, clients and reservations; renamed items; IP, firmware, state, model and network or VLAN changes; devices and clients that **moved** (a different switch, port or AP; an unknown location, such as an offline Wi-Fi client, is never a move); group changes; reservation changes; and a controller version change. Clients that connected or disconnected are listed too, but only the first 15 of each (and of client IP changes); `--all` lists every one. `--json` prints everything.
- **Choosing what to compare:** `diff` with no arguments compares the newest saved snapshot with the live network, `diff OLD` compares a snapshot (a path, or a file name inside the snapshot directory) with the live network, and `diff OLD NEW` or `--last-two` compare two files without contacting the controller.
- **Files:** `snapshot` writes `snapshot-YYYYMMDD-HHMMSSZ.json` into `./snapshots/` (the time in the name is UTC, the `Z`, so the order never depends on time zone or daylight saving; the time inside the file keeps your local time and offset; files named without the `Z` by earlier versions are local time and still listed and sorted correctly, using their offset-bearing `captured_at` when available to disambiguate a repeated hour and falling back to the local filename time if unreadable) (change it with `--dir DIR`), never overwriting an existing file. `-o FILE` (long form `--output`) picks the name; it refuses to replace an existing file unless you add `--force`. `--keep N` afterwards deletes the oldest snapshots in the directory beyond the newest N; it only touches files named like the ones this tool writes, and never the one just saved.
- **Privacy:** snapshots contain real MACs, IPs and device names. They are created readable only by you, and `snapshots/` is git-ignored. Do not commit or share them.
- A snapshot file has a format version. A file from a newer, incompatible version, a damaged file, or one that is not a snapshot stops with a clear message (exit code 3).
- Both commands only read from the controller; the files are written locally.

## New clients

`new-clients` lists every known client, connected or not, that has not been added to at least one client group (Network > Client Groups), so newly seen devices stand out. Add a client to a group in the controller and it drops off the report. Columns: Name, MAC Address, IP Address, Vendor, Connection Type, Where (switch and port, or AP), First Seen, Last Seen, Status, Private MAC (`yes` for a randomized address, see below). Newest first-seen comes first, with no age cutoff. `-s TEXT` (or `--search TEXT`) filters and `--json` prints JSON.

Group membership comes from the legacy `stat/alluser` client records and the legacy v2 `network-members-groups` definitions; the Integration API has no client groups. A group that has been deleted does not count as membership. If the group definitions cannot be read, the tool warns and trusts each client's own group list.

## Randomized MAC addresses

Phones, tablets and laptops often use a **private (randomized) Wi-Fi MAC address**, frequently a different one for each network, and some rotate it. That makes one device look like several, defeats "new client" detection, makes `snapshot`/`diff` noisy, and breaks DHCP reservations, which are tied to one MAC. A MAC address is **randomized** here when it is a locally administered unicast address: the second hex digit is `2`, `6`, `A` or `E` (the second-lowest bit of the first byte is set and the lowest is clear). This is read from the address itself, so it needs no extra request.

- `query clients` (table and `--json`) and `new-clients` have a **Private MAC** column: `yes` for a randomized address, empty otherwise. The CSV export and `query devices` are unchanged.
- `client` adds `[randomized MAC: reservations and history may not hold]` after the MAC, and `client --json` has `identity.private_mac` (`true` or `false`).
- `diagnose` adds two **info** findings, never a warning, because it is normal for phones: `reservation.private_mac` for each reservation whose MAC is randomized (it stops applying if the device changes its address), and one `client.private_mac_summary` finding with the count of connected clients that use randomized addresses. To silence both, use an ignore with `message = "randomized"` (and a required `reason`); add `subject = "clients"` to silence only the summary, or use the reservation's client name to silence only that reservation.

It is a hint, not proof: virtual machines, containers, bridges, VPNs and some IoT devices also use locally administered addresses, and for them the vendor (OUI) lookup is empty. A device that turned the feature off keeps its old random MAC until it reconnects.

## Switch ports

`query ports` lists every port on every switch: status, speed, duplex, PoE power, the connected client or device, and rx/tx errors (`--json` includes every column, such as traffic counters). Filters, which combine with AND:

- `--switch NAME`: switches whose name contains NAME (case-insensitive)
- `--down`: only ports that are down
- `--errors`: only ports with rx/tx errors
- `-s TEXT`: text match on any field, for example a connected client's name

`--switch`, `--down` and `--errors` are only valid with `query ports`. Port data comes from the legacy `stat/device` and `stat/sta` endpoints, so it is empty (with a warning) if those are unavailable.

## DHCP reservations

`query reservations` lists every enabled fixed IP reservation, including clients that are currently offline. Columns: Name, MAC Address, Reserved IP, Network, VLAN, Current IP, Status, Last Seen. It reads the legacy `stat/alluser` and `rest/networkconf` endpoints, since the Integration API does not expose reservations. Only clients with the reservation enabled are listed; disabled reservations keep a stale IP on the controller and are ignored.

**`--offline`** keeps only the reservations whose client is not connected and was last seen at least `reserved_offline_warn_days` ago (default 1 day, from `./unifi-sentinel.toml` or `--config FILE`), or has no last-seen time, and adds an **Offline For** column (`6d`, `30h`, or `never seen`). It is the same rule the `diagnose` check uses, without severities and without the ignore list, so it shows the whole set. It only applies to `query reservations`; `--config` is only valid together with `--offline`.

## Output files

1. **`unifi_clients.csv`**: master inventory of connected clients and UniFi devices. Columns: Type, Name, MAC Address, IP Address, Model, Connection Type, Switch, Port, Last Seen, Status. By default only currently connected clients are listed; pass `--include-offline` to add previously seen clients with Status `Offline` (from the legacy `stat/alluser` endpoint).
2. **`switch_<name>.csv`**: one file per switch with port status, speed, duplex, PoE, connected client or device, and traffic counters.

CSV files are ignored by git.

### Names in exports and output

A device or client chooses its own hostname, and so does anyone who joins your network, so names are treated as untrusted:

- **CSV cells**: a text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return would run as a formula when the file is opened in Excel, Sheets or LibreOffice. Such cells are written with a leading apostrophe (`'=1+1`), so the spreadsheet treats it as text. Excel and LibreOffice show the apostrophe when they open a CSV; that is the price of safety, and it is the only change. Numbers and every other cell are written as they are (a number stored as text, such as `-67`, counts as text). If you read the CSV with a script, strip a leading `'` from text columns.
- **Text output** (tables, `diagnose`, `topology`, `client`, `wifi`, `wan`, `events`, `diff`, and messages): tabs and line breaks in a name become a space (so a name cannot add a fake line such as a forged `[CRITICAL]` finding), and other control characters, including terminal escape sequences, are removed, as are the text-direction override and isolate characters that can disguise a name, and invisible characters (zero-width space, word joiner, byte order mark) that make two different names look the same. Accented letters, emoji (including their joiners) and right-to-left scripts such as Hebrew and Arabic, with their direction marks, are kept.
- **`--json` output** is not changed: it carries the names as the controller reports them, with control characters escaped by JSON itself (`\u001b`). Treat them as untrusted data if you pass them on.
