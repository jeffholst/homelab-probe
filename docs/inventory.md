# Inventory, queries and exports

The commands that list and export what is on the network: `query` (devices, clients, reservations, switch ports, networks and Wi-Fi networks), `export`, `snapshot` and `diff`, `new-clients`.

## Devices

`query devices` shows each UniFi device with its firmware version, whether a firmware update is available, and its uptime (for example `2d 7h`). Offline devices have no uptime. `--json` adds `Uptime (s)` with the raw seconds. These columns come from the Integration API and appear only for `query devices`; the `export` CSV columns are unchanged.

## Snapshots and diff

When something breaks, the first question is "what changed since it last worked?". `snapshot` saves the inventory, and `diff` compares.

```bash
uv run hlp.py snapshot                         # now: ./snapshots/snapshot-20261001-011530Z.json
# ...later, when something is wrong...
uv run hlp.py diff                             # the newest snapshot against the network right now
uv run hlp.py diff --last-two                  # the two newest snapshots of this site
uv run hlp.py diff snapshot-20260929-080000Z.json   # a named snapshot against now
uv run hlp.py diff OLD.json NEW.json           # two files
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
- **Choosing what to compare:** `diff` with no arguments compares the newest saved snapshot **of this site** with the live network, `diff OLD` compares a snapshot (a path, or a file name inside the site's directory, then inside `snapshots/`) with the live network, and `diff OLD NEW` compares two files. Finding the site's directory (for no argument, a file name or `--last-two`) asks the controller which site is meant, with one request; `diff OLD.json NEW.json` with two paths, and anything with `--dir DIR`, need no controller for the saved snapshots.
- **Snapshots from earlier versions** were all saved straight into `snapshots/`. They are not moved: each is listed for the site its own record names, so nothing is lost and a snapshot of another site is never picked.
- **Files:** `snapshot` writes `snapshot-YYYYMMDD-HHMMSSZ.json` into `./snapshots/<site id>/` (**one directory per site**, named by the site's UUID as the controller reports it; the time in the name is UTC, the `Z`, so the order never depends on time zone or daylight saving; the time inside the file keeps your local time and offset; files named without the `Z` by earlier versions are local time and still listed and sorted correctly, using their offset-bearing `captured_at` when available to disambiguate a repeated hour and falling back to the local filename time if unreadable) (change it with `--dir DIR`, which is then used as it is), never overwriting an existing file. `-o FILE` (long form `--output`) picks the name; it refuses to replace an existing file unless you add `--force`. `--keep N` afterwards deletes the oldest snapshots in the directory beyond the newest N (in the default place only this site's own, including the older ones that earlier versions saved straight into `snapshots/` and whose record names this site); it only touches files named like the ones this tool writes, and never the one just saved.
- **Privacy:** snapshots contain real MACs, IPs and device names. They are created readable only by you, and `snapshots/` is git-ignored. Do not commit or share them.
- A snapshot file has a format version. A file from a newer, incompatible version, a damaged file, or one that is not a snapshot stops with a clear message (exit code 3).
- Both commands only read from the controller; the files are written locally.

## New clients

`new-clients` lists the clients the controller **first saw recently**, connected or not, so a device that joined the network stands out without anyone having to tag it. The controller records a `first_seen` time for every client in its client history (`stat/alluser`); a client is new when that time is within `--since` (default `7d`; units `m`, `h`, `d`, `w`, for example `--since 24h`). Columns: Name, MAC Address, IP Address, Vendor, Connection Type, Where (switch and port, or AP), First Seen, Last Seen, Status, Private MAC (`yes` for a randomized address, see below). The newest first-seen comes first. `-s TEXT` (or `--search TEXT`) filters and `--json` prints JSON.

```bash
uv run hlp.py new-clients                  # first seen in the last 7 days
uv run hlp.py new-clients --since 24h      # in the last day
uv run hlp.py new-clients --ungrouped      # in no client group, whenever first seen
uv run hlp.py new-clients --ungrouped --since 30d   # both: new and still in no group
```

The line under the table says what was counted: `1 client(s) first seen in the last 7d (1 with a private MAC)`. Things to know:

- **A client the controller gives no first-seen time for is unknown, never new.** The footer counts them (`; 2 known client(s) have no first-seen time and are not counted`) so the list is not mistaken for complete. A time in the future (clock skew) counts as now.
- **Randomized (private) MAC addresses.** A phone that rotates its Wi-Fi address can show up as a new client each time; the Private MAC column and the footer count mark them so you can tell a new phone from a known phone with a new address.
- **A client removed from the controller's history** is expected to get a new first-seen time when it reconnects and so to look new again (expected from how the history works; not checked on a live controller).
- **`--ungrouped`** is the earlier behavior: every known client, connected or not, that has not been added to at least one client group (Network > Client Groups), with no age limit unless you also give `--since`. Group membership comes from the legacy `stat/alluser` client records and the legacy v2 `network-members-groups` definitions; the Integration API has no client groups. A group that has been deleted does not count as membership. If the group definitions cannot be read, the tool warns and trusts each client's own group list. It is the way to use groups as an approval list: add a client to a group and it drops off.
- **Reading `stat/alluser` is required** (as for `snapshot` and `diff`): if it cannot be read the command stops with exit code 3 instead of printing an empty list.

**Being told.** `diagnose` reports each client first seen within `new_client_window_hours` (default 24, `0` turns it off, see [thresholds](diagnose.md#configuration-thresholds-and-ignore-list)) as an information finding, code `client.new_device`, with the MAC address as its subject (so renaming the device does not make it a new finding) and a message such as `new device 'guest-phone' first seen 2h ago (wireless, private MAC, 10.0.0.52)`. Information findings never change the exit code and `diagnose --notify` sends only findings at or above `--notify-min` (default `warning`), so to be notified of new devices run it with `--notify-min info` (which also sends the other information findings; see [notifications](notifications.md)).

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

**`--offline`** keeps only the reservations whose client is not connected and was last seen at least `reserved_offline_warn_days` ago (default 1 day, from `./hlp.toml` or `--config FILE`), or has no last-seen time, and adds an **Offline For** column (`6d`, `30h`, or `never seen`). It is the same rule the `diagnose` check uses, without severities and without the ignore list, so it shows the whole set. It only applies to `query reservations`; `--config` is only valid together with `--offline`.

## Networks

`query networks` lists the networks the controller serves, from the legacy `rest/networkconf`:

```text
uv run hlp.py query networks
Name           Purpose          VLAN  Subnet        Gateway    DHCP    DHCP Range                 Clients
-------------  ---------------  ----  ------------  ---------  ------  -------------------------  -------
Main           corporate        1     10.0.0.0/24   10.0.0.1   Server  10.0.0.100 - 10.0.0.200    1
IoT            corporate        20    10.0.20.0/24  10.0.20.1  Server  10.0.20.100 - 10.0.20.200  0
Internet 1     wan                                                                                0
Remote Access  remote-user-vpn        10.0.99.0/24  10.0.99.1                                     0

4 row(s)
```

- **Name** and **Purpose** (as the controller names it: `corporate`, `wan`, `remote-user-vpn`, ...).
- **VLAN**: the tag when VLANs are on, `1` (the default untagged VLAN) when they are off, and blank for WAN and VPN networks, which do not say.
- **Subnet** and **Gateway**: the network address with its prefix, and the gateway's own address on it (the controller stores them as one `ip_subnet` value). Blank for a WAN network; text that is not an address is shown as it came.
- **DHCP** and **DHCP Range**: `Server` with the dynamic range (the range is blank if the controller serves DHCP but has no valid range), `Relay`, `Off`, or blank when the record says nothing (WAN and VPN networks).
- **Clients**: the connected clients whose `network_id` is this network's id, of every kind. A client with no or an unknown network is counted nowhere. The cell is blank, not `0`, when the client list could not be read.

Networks keep the controller's order. `--json` and `--csv` give the same columns (a number is a number, blank is `""`), and `-s TEXT` matches any cell. The network list is the whole answer, so if it cannot be read the command stops with exit code 3 and the warning names the endpoint, instead of printing an empty table. It reads only `rest/networkconf` and the connected clients (`stat/sta`), no device details.

## Wi-Fi networks

`query wlans` lists the Wi-Fi networks (SSIDs), from the legacy `rest/wlanconf`, with the VLAN of the network each one is on:

```text
uv run hlp.py query wlans
Name      Enabled  Security   Bands           Network  VLAN  Guest  Client Isolation  Hidden  Clients
--------  -------  ---------  --------------  -------  ----  -----  ----------------  ------  -------
HomeNet   Yes      WPA2/WPA3  2.4 GHz, 5 GHz  Main     1     No     No                No      1
GuestNet  Yes      WPA2       5 GHz           IoT      20    Yes    No                No      0
Lobby     Yes      Open                       Main     1     No     No                No      0
OldCam    Yes      WEP                        Main     1     No     No                No      0
Retired   No       Open                       Main     1     No     No                No      0
Sensors   Yes      WPA3       5 GHz, 6 GHz    IoT      20    No     Yes               Yes     0

6 row(s)
```

- **Name** is the SSID. **Enabled**, **Guest**, **Client Isolation** and **Hidden** are `Yes` or `No` (a flag the controller does not send reads as `No`, and a missing `enabled` as `Yes`, so a network is never hidden by a missing field).
- **Security** is `Open`, `WEP`, `WPA`, `WPA2`, `WPA2/WPA3` (the mixed mode) or `WPA3`. A value this tool has not seen is shown as the controller wrote it, never guessed.
- **Bands** is for example `2.4 GHz, 5 GHz`; blank when the network does not say.
- **Network** and **VLAN** come from the network the SSID is attached to, joined by id (blank if the network list could not be read, with a warning).
- **Clients** counts the connected clients that are on the SSID: those with an SSID whose `wlanconf_id` is this network's id, or, when a client has no id, whose SSID is the network's name. A client with no SSID is not counted, whatever else its record says: on the controller checked, a few clients that are not wired carry no SSID, access point or signal at all, so by their own record they are not on Wi-Fi. Blank when the client list could not be read.

**The passphrase is never read.** The controller keeps it in the same record, and the code only looks at the fields above, so it cannot reach a table, `--json`, `--csv`, `--verbose` output, a search or an error message (a test makes the record raise if anything else is touched). `--json`, `--csv` and `-s` work as for networks. A site with no Wi-Fi network prints `0 row(s)`; if the settings cannot be read the command stops with exit code 3, not an empty table.

## Clients on a network, SSID or access point

`query clients` takes three filters for where a **connected** client is attached. Each is a case-insensitive substring and they combine with AND, and with `-s`:

```bash
uv run hlp.py query clients --ssid guest              # on a Wi-Fi network whose name contains "guest"
uv run hlp.py query clients --network iot --ap garage   # on the IoT network and on the Garage AP
```

- `--network NAME`: the client's network. The name comes from the client's `network_id` (so a renamed network matches its new name), or from the client's own `network` text when the network list could not be read.
- `--ssid NAME`: the SSID the client is on. Wired clients have none.
- `--ap NAME`: the name of the access point the client is on (found by the access point's MAC address). Wired clients have none.

A row with no connected-client record, an offline client from `--include-offline`, has no attachment and is dropped when any filter is used. A filter with no match prints `0 row(s)`; if the connected-client details (`stat/sta`) cannot be read the command stops with exit code 3 instead of filtering everything away. The filters are only valid with `query clients`, and an empty value is a usage error. `--network` also reads the network configuration (`rest/networkconf`); the others read nothing extra.

## Output files

1. **`unifi_clients.csv`**: master inventory of connected clients and UniFi devices. Columns: Type, Name, MAC Address, IP Address, Model, Connection Type, Switch, Port, Last Seen, Status. By default only currently connected clients are listed; pass `--include-offline` to add previously seen clients with Status `Offline` (from the legacy `stat/alluser` endpoint).
2. **`switch_<name>.csv`**: one file per switch with port status, speed, duplex, PoE, connected client or device, and traffic counters.

`--format json` writes the same data as one file instead (below).

CSV files are ignored by git.

### `--format json`: the same data in one file

`export --format json` writes one file, **`unifi_inventory.json`**, into the output directory (`-o`, as for the CSV files) instead of the CSV files; it does not touch CSV files that are already there. It holds the same data, with the same `--include-offline`, as an object with a `version` (1, as every JSON object of the tool, with a [schema](schemas.md)) and three lists:

- **`devices`** and **`clients`**: the rows of `unifi_clients.csv`, with the same columns (Type, Name, MAC Address, IP Address, Model, Connection Type, Switch, Port, Last Seen, Status), split by their Type. Offline clients come last in `clients`.
- **`switches`**: one entry per switch that has a port table, with its `name`, its `mac` and its `ports`, the rows of that switch's `switch_<name>.csv` (numbers stay numbers, as in `query ports --json`). Two switches with the same name are two entries, told apart by `mac`.

```bash
uv run hlp.py export --format json -o ./out --include-offline
```

Unlike the CSV files, the values are **raw**: a name that starts with `=`, `+`, `-` or `@` is not changed, because JSON is not opened as a spreadsheet, and control characters are escaped by the JSON itself (`\u001b`). The default stays `--format csv`, and its files are byte for byte what they were. `unifi_inventory.json` is git-ignored like the CSV files, because it holds real MAC and IP addresses.

### Names in exports and output

A device or client chooses its own hostname, and so does anyone who joins your network, so names are treated as untrusted:

- **CSV cells**: a text cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return would run as a formula when the file is opened in Excel, Sheets or LibreOffice. Such cells are written with a leading apostrophe (`'=1+1`), so the spreadsheet treats it as text. Excel and LibreOffice show the apostrophe when they open a CSV; that is the price of safety, and it is the only change. Numbers and every other cell are written as they are (a number stored as text, such as `-67`, counts as text). If you read the CSV with a script, strip a leading `'` from text columns.
- **Text output** (tables, `diagnose`, `topology`, `client`, `wifi`, `wan`, `events`, `diff`, and messages): tabs and line breaks in a name become a space (so a name cannot add a fake line such as a forged `[CRITICAL]` finding), and other control characters, including terminal escape sequences, are removed, as are the text-direction override and isolate characters that can disguise a name, and invisible characters (zero-width space, word joiner, byte order mark) that make two different names look the same. Accented letters, emoji (including their joiners) and right-to-left scripts such as Hebrew and Arabic, with their direction marks, are kept.
- **`--json` output** is not changed: it carries the names as the controller reports them, with control characters escaped by JSON itself (`\u001b`). Treat them as untrusted data if you pass them on.
