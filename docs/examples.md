# Examples

Every command with its options, and sample output generated from the checked-in synthetic test fixture (not a real network). The same data is what `--demo` serves for supported commands, so you can run those examples without a controller by putting `--demo` before the command (see [Trying it without a controller](configuration.md#trying-it-without-a-controller---demo)). `doctor`, `snapshot` and `diff` are not supported in demo mode, and notification options are refused.

## More examples


```bash
uv run hlp.py web-user list               # the web interface's accounts (see docs/web.md)
uv run hlp.py --demo diagnose             # no controller: the synthetic network, nothing read or sent
uv run hlp.py --demo topology --clients   # supported reports use the same synthetic data
uv run hlp.py info
uv run hlp.py export
uv run hlp.py export -o ./out   # write CSVs to a directory (long form: --output-dir)
uv run hlp.py export --include-offline   # also list previously seen clients
uv run hlp.py export --format json -o ./out   # one file, unifi_inventory.json, instead of the CSVs
uv run hlp.py query devices               # UniFi devices with firmware and uptime
uv run hlp.py query clients -s printer --json   # filter (long form: --search), JSON output
uv run hlp.py query clients --include-offline   # also previously seen clients
uv run hlp.py query clients --csv > clients.csv  # CSV for a spreadsheet (same columns as --json)
uv run hlp.py query reservations          # DHCP fixed IP reservations
uv run hlp.py query reservations --offline   # reserved clients that have been offline for a day or more
uv run hlp.py query ports                 # every switch port
uv run hlp.py query ports --down --switch rack   # down ports on matching switches
uv run hlp.py query ports --errors        # ports with rx/tx errors
uv run hlp.py query wlans                 # Wi-Fi networks (also: query networks; query clients --ssid guest)
uv run hlp.py snapshot                    # save the inventory to ./snapshots/
uv run hlp.py diff                        # what changed since the newest snapshot?
uv run hlp.py topology                    # how the gateway, switches and APs are wired
uv run hlp.py topology --clients          # ...with the wired clients under each device
uv run hlp.py wifi                        # radios and a channel plan from the neighbors
uv run hlp.py wifi --band 2.4 --ap hall   # one band, one AP
uv run hlp.py wan                         # is it my internet or my LAN?
uv run hlp.py wan --days 90               # a longer speedtest history
uv run hlp.py firewall                    # your firewall policies, port forwards and findings
uv run hlp.py firewall --all --zones      # ...plus the built-in policies, the zones and the zone matrix
uv run hlp.py firewall --search plex --json   # filter by text, as JSON
uv run hlp.py events                      # the last 24 hours, newest first
uv run hlp.py events --since 7d --severity high   # recent serious events
uv run hlp.py events --client phone --event disconnected   # one client's drops
uv run hlp.py events --summary --since 7d   # counts and the noisiest clients
uv run hlp.py client desktop              # one client: attachment, link, findings
uv run hlp.py client aa:bb:cc:dd:ee:ff --json   # by MAC (any format) or IP, as JSON
uv run hlp.py new-clients                 # clients in no client group
uv run hlp.py audit                       # settings that are probably not what you want
uv run hlp.py audit --json --fail-on info # as JSON; any finding gives exit code 1
uv run hlp.py diagnose                    # health checks
uv run hlp.py diagnose --json             # the same, as JSON with a stable code per finding
uv run hlp.py diagnose --only ports,wifi  # just those checks, and read only what they need
uv run hlp.py diagnose --skip events      # everything except the event-log checks (no POST)
uv run hlp.py diagnose --watch 60         # every minute, print only what changed (Ctrl-C to stop)
uv run hlp.py --site Lab diagnose --no-events   # another site for this run (beats UNIFI_SITE_ID)
uv run hlp.py completion zsh > _hlp   # a completion script (bash, zsh or fish)
```

**`query --csv`** prints CSV on stdout instead of the table, with the columns and rows of `--json` (`Private MAC` for clients, `Offline For` with `--offline`, every column for `ports`) and no row-count footer; a result with no rows is just the header. It cannot be combined with `--json` (usage error, exit 64). Cells are quoted by Python's `csv` writer, so commas, quotes and line breaks are safe, and every text cell is cleaned first (control characters and invisible characters removed, line breaks turned into spaces) and then checked for a leading `=`, `+`, `-` or `@`: such a name gets a leading `'`, which a spreadsheet shows as plain text instead of running it as a formula. The `'` is part of the value you see in the file (the `export` files follow the same rule); numbers are left as numbers, so a negative error count stays `-5`. `--json` stays raw.

**`--watch`, `--site` and `completion`.** `diagnose --watch SECONDS` prints the full report once and then, every SECONDS (10 to 86400), only what is new, worse or fixed; it cannot be combined with `--json` or `--notify` (see [Diagnose](diagnose.md#diagnose)). `--site` goes before the command and chooses the site for that run ([Configuration](configuration.md#configure)). `completion` prints a script for the shell you name and needs no `.env` ([Shell completion](configuration.md#shell-completion)).

`query` takes an optional kind (`all` by default, `devices`, `clients`, `reservations` or `ports`). Run these from the project root (uv uses `pyproject.toml`). After `pip install .` use `hlp <command>` instead. Run `--help` on the tool or any command for options, and `--version` for the version.

## Example Output

### unifi_clients.csv

```csv
Type,Name,MAC Address,IP Address,Model,Connection Type,Switch,Port,Last Seen,Status
Client,desktop,BB:00:00:00:00:01,10.0.0.10,,Wired,Office Switch,3,2026-01-01 09:00:00,Online
Client,phone,BB:00:00:00:00:02,10.0.0.11,,Wireless,,,2026-01-01 09:30:00,Online
Device - Dream Machine,Gateway,AA:00:00:00:00:01,10.0.0.1,UCG Max,Wired,,,2026-01-01 10:00:00,Online
Device - Switch,Office Switch,AA:00:00:00:00:02,10.0.0.2,USW-Lite-8-PoE,Wired,Gateway,2,2026-01-01 10:00:00,Online
Device - Access Point,Office AP,AA:00:00:00:00:03,10.0.0.3,U7 Pro,Wired,Office Switch,2,2026-01-01 10:00:00,Online
Device - Access Point,Garage AP,AA:00:00:00:00:04,10.0.0.4,U6 Pro,Wired,Office Switch,5,,Offline
Client,old-printer,BB:00:00:00:00:03,10.0.0.50,,Wired,Office Switch,6,2026-10-02 17:28:19,Offline
Client,old-tablet,BB:00:00:00:00:04,10.0.0.51,,Wireless,,,2025-12-06 05:46:40,Offline
```

### diagnose

Sample from synthetic data with `diagnose --no-events` (text labels are used when output is piped; a UTF-8 terminal shows emojis):

```text
[CRITICAL] Gateway: reports that it is overheating
[WARNING ] Garage AP: device is offline
[WARNING ] Gateway Backup: storage 97.5% used
[WARNING ] Office Switch: CPU utilization 95%
[WARNING ] Office Switch: PoE budget 41.6 W of 52 W used (80%)
[WARNING ] Office Switch: uplink to Gateway negotiated at 100 Mbps but both ends support 1000 Mbps
[WARNING ] Office Switch port 1: link has gone down 5 times since boot, switch up 3h 12m
[WARNING ] Office Switch port 2: 4 rx/tx errors
[WARNING ] Office Switch port 2: link is half duplex
[WARNING ] Office Switch port 2: dropping 0.75% of rx packets (75 of 10000)
[WARNING ] Office Switch port 2: STP state is blocking, not forwarding
[WARNING ] old-printer: reserved IP 10.0.0.50 is outside network IoT (10.0.20.1/24)
[INFO    ] Office AP: restarted 5m ago
[INFO    ] Office Switch port 2: negotiated at 100 Mbps
[INFO    ] wlan: wlan subsystem reports warning: 1 device(s) disconnected (see the device findings)

1 critical, 11 warnings, 3 info
```

### new-clients

```text
Name         MAC Address        IP Address  Vendor                Connection Type  Where                        First Seen           Last Seen            Status   Private MAC
-----------  -----------------  ----------  --------------------  ---------------  ---------------------------  -------------------  -------------------  -------  -----------
old-tablet   BB:00:00:00:00:04  10.0.0.51                         Wireless                                      2025-06-15 15:06:40  2025-12-06 05:46:40  Offline
old-printer  BB:00:00:00:00:03  10.0.0.50   Example Printers Inc  Wired            Wired, Office Switch port 6  2023-11-14 22:13:20  2026-10-02 17:27:59  Offline

2 client(s) in no group
```

### switch_Office Switch.csv

```csv
Port,Port Index,Status,Speed,Full Duplex,PoE Enabled,PoE Power (W),PoE Class,Connected Type,Connected Name,Connected MAC,Connected Model,RX Bytes,TX Bytes,RX Packets,TX Packets,RX Errors,TX Errors
Uplink,1,Up,1000 Mbps,Yes,No,,,Device - Dream Machine,Gateway,AA:00:00:00:00:01,UCG Max,100,200,100000,100000,0,0
Port 2,2,Up,100 Mbps,No,No,,,Device - Access Point,Office AP,AA:00:00:00:00:03,U7 Pro,200,400,10000,10000,4,0
Port 3,3,Up,1000 Mbps,Yes,No,,,Client,desktop,BB:00:00:00:00:01,,300,600,2500000,2500000,0,0
Port 4,4,Down,,Yes,No,,,,,,,400,800,,,0,0
```
