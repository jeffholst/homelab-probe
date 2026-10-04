# Diagnose and audit

`diagnose` runs read-only health checks and `audit` reports settings that are probably not what you want. They share the finding format, the ignore list, `--json` and the exit-code rules. See also [Notifications](notifications.md) and the [exit codes](../README.md#exit-codes).

## Diagnose

`diagnose` prints findings sorted by severity:

| Level | Examples |
| ----- | -------- |
| 🛑 critical | an AP radio's channel utilization at or above `radio_util_critical_pct` (default 90%); a switch's PoE budget at or above `poe_critical_pct` (default 95%); the controller reports a WAN, internet or VPN subsystem in `error`; a LAN/WLAN `error` with no disconnected device to explain it; gateway offline; an offline switch or device that other devices uplink through; CPU or memory at or above `resource_critical_pct` (default 98%); a device that reports it is overheating; a client with a DHCP reservation that has been offline for `reserved_offline_critical_days` (default 7) or more; a reserved IP inside its network's DHCP pool that another client is using right now |
| ⚠️ warning | 24-hour internet availability below `wan_availability_warn_pct` (default 99%), overall or for one monitoring target; the last speedtest well below the 30-day median (`wan_speed_drop_pct`, default 70%); an IP conflict reported in the last 24 hours; a client that disconnected `event_flap_count` or more times in that window, or a device that was unreachable that often; a Wi-Fi client with signal at or below `wifi_weak_signal_dbm` (default -75 dBm), `wifi_retry_pct` (default 30%) or more of its transmissions retried, or satisfaction below `wifi_satisfaction_warn` (default 50%); an AP radio with channel utilization at or above `radio_util_warn_pct` (default 70%), or retries or satisfaction past the same limits; a port whose link has gone down `link_flap_count` or more times since boot (default 5); a port dropping `port_drop_pct` or more of its packets (default 0.1%); an up port whose STP state is not forwarding; a switch's PoE budget at or above `poe_warn_pct` (default 80%); an uplink negotiated below what both ends support; a subsystem in `warning` the same way; internet latency at or above `wan_latency_warn_ms` (default 100 ms) or drops at or above `wan_drops_warn` (default 10); other offline devices; port rx/tx errors; half-duplex links; CPU or memory at or above `resource_warn_pct` (default 90%) but below the critical level; connected clients with no IP address or a link-local (169.254.x.x) address, shown with where they attach; DHCP reservation problems: an online client whose current IP differs from its reservation, the same IP reserved for several clients, or a reserved IP outside its network's subnet; the same IP in use by several clients or UniFi devices on any VLAN, or a reserved IP currently used by a different client or UniFi device; a client with a DHCP reservation that has been offline for `reserved_offline_warn_days` (default 1 day) or more; a reserved IP inside its network's DHCP pool; a private, carrier-grade NAT or link-local WAN address (double NAT or no address from the ISP) |
| ℹ️ info | a client that roamed `event_flap_count` or more times; a device that was unreachable earlier but is online now; high-latency events from the ISP monitor; the controller's LAN/WLAN status when it is only caused by disconnected devices (they are reported individually); devices waiting to be adopted; a failed speedtest; ports negotiated at or below `slow_link_mbps` (default 100 Mbps); legacy data unavailable (port and overheating checks skipped); a reservation whose client has no last-seen time; a reservation tied to a randomized (private) MAC address, and a count of the connected clients that use one; a network whose DHCP range is missing or invalid, so its reservations cannot be checked against it |

**Overheating.** `device.overheating` is critical for an **online** device whose legacy `stat/device` record says `overheating` is `true`: a device that cannot cool itself can shut down, so it is worth knowing before it does. The flag is the device's own judgement, so there is no threshold to set. It exists on the gateway and on most switches (on the controller checked, 5 of 6 such devices; none was overheating, so the `true` case has not been seen live), not on access points, and a missing or non-boolean value means "unknown", never a finding. An offline device is skipped, because its record keeps what it last reported (it is reported as offline). The temperature the gateway also lists, and `has_temperature`, are not used: the limits depend on the model, and `has_temperature` was `false` even on the gateway that lists its CPU temperature. It reads the same `stat/device` data as the port checks, so it needs no extra request, and when that data cannot be read the one `controller.legacy_unavailable` finding says both were skipped. Select it with `--only devices`.

The reservation checks read the legacy `stat/alluser` and `rest/networkconf` endpoints (the same data as `query reservations`); offline clients are checked for duplicate and out-of-subnet reservations (a reservation whose network cannot be resolved is skipped for the subnet check) and for **being offline for too long**: a reserved client that is not connected and was last seen `reserved_offline_warn_days` (default 1) days ago or more is a warning, and `reserved_offline_critical_days` (default 7) or more is critical, so a server or appliance that went quiet does not stay invisible. A reservation with no last-seen time is reported once as info. UniFi devices are left to the device checks. A client that is meant to be off (a laptop, a seasonal device) belongs in the ignore list: `subject = "travel-laptop"`, `message = "is offline"`. `query reservations --offline` lists exactly the reservations this check reports, so you can inspect them before relying on the alerts.

**Choosing checks: `--only` and `--skip`.** The checks are grouped into **areas**, named after their finding codes. `--only AREA[,AREA...]` runs just those areas and `--skip AREA[,AREA...]` runs all but those (both can be repeated and take comma-separated names, in any case; giving both is a usage error, exit 64, as is a name that is not an area, which lists the valid ones). Only the data the chosen checks need is read.

| Area | Codes | Reads beyond devices and clients |
| ---- | ----- | -------------------------------- |
| `devices` | `device.*`, `controller.*` | health (for devices waiting to be adopted) |
| `health` | `health.*`, `internet.*` | health |
| `wan` | `wan.*` | health, speedtests |
| `clients` | `client.*`, `ip.*` | nothing |
| `reservations` | `reservation.*` | client history and network configuration |
| `ports` | `port.*`, `link.*` | nothing |
| `wifi` | `wifi.*` | nothing |
| `events` | `event.*` | the event log (the one POST) and the reservations, to name who holds a conflicting address |

A few checks report in two areas (the IP-conflict and duplicate-address checks name reservations, the randomized-MAC checks cover both `reservations` and `clients`), so a finding appears with the area its code belongs to and only when that area is selected. `--skip events` sends no event-log request, and `--no-events` is the same as `--skip events`. Exit codes, `--fail-on`, the ignore list and `--show-ignored` apply to the findings that were produced. The text output ends with a `Checked: ... (not checked: ...)` line when you chose areas, so "No issues found." is not mistaken for a clean bill of health; `--json` always has `areas`, the list of the areas that ran (all eight for a full run). With `--notify`, a finding of an area that did not run is neither new nor recovered and its remembered state is left as it was, so `diagnose --only ports --notify` never announces that problems in other areas were fixed; `--notify-baseline` after a partial run keeps what was recorded for the other areas.

**Watching: `--watch SECONDS`.** `diagnose --watch 60` prints the findings once, exactly as without the option, and then runs the checks again every 60 seconds (10 to 86,400), printing only what changed, one line each with the time, until you press Ctrl-C:

```text
14:03:11  [CRITICAL] NEW  Office Switch: device is offline
14:09:11  [OK] RECOVERED  Office Switch (device.offline)
```

What counts as a change is what a notification would announce: a finding that is **new**, one that got **worse** (`WORSE`), a critical one still unresolved after `notify_repeat_hours` (`STILL`, so a long outage does not scroll away unnoticed), and one that is **gone** (`RECOVERED`). A finding is the same finding when its check and subject are the same, so a changing count or wording prints nothing, and findings of every severity are followed (information too). The ignore list and `--only`/`--skip` apply, and with `--only` or `--skip` the areas that were not checked are never reported as gone. A required read failure is reported on stderr (`could not read the controller (...); trying again in 60 s`) and retried; only a failure on the first pass ends the command with exit code 3. If requested optional data is unavailable, that pass is not compared and the last complete watch state is kept; if the first pass is incomplete, the first complete pass establishes the baseline without reporting changes. The exit code, when you stop it, is the one the last complete pass would have given (`--fail-on` is honored). Nothing is written, sent or remembered: `--watch` is a live terminal view and cannot be combined with `--json` or `--notify`; for alerts without a long-running process run `diagnose --notify` from cron instead (see [Notifications](notifications.md)). Warnings about unavailable data are printed on every pass, and every pass is one full read of the controller (about ten requests), which is why the shortest interval is 10 seconds.

**Reservations inside the DHCP pool.** For each network where the controller itself serves DHCP (`dhcpd_enabled` true and no DHCP relay), a reserved IP between the pool's first and last address (both ends included) is a warning: `reserved IP 10.0.20.150 is inside the DHCP pool 10.0.20.100-10.0.20.200 of IoT`. The gateway honours the reservation, but an address in the dynamic range can also be offered to other clients, and a device with a static address in the range collides with them, so the safe layout keeps reservations outside the range. It is critical when another client or UniFi device is using that address right now (the message names it). Networks where DHCP is off or relayed (and WAN or VPN networks, which can carry a range without serving client DHCP) are skipped; if DHCP is on but the range is missing or invalid, that network is one info finding (only if it has reservations) instead of a guess. A reservation placed in the pool on purpose belongs in the ignore list (`subject = "media-box"`, `message = "DHCP pool"`). The range comes from the same `rest/networkconf` data as the subnet check; `query reservations` has no in-pool column, so the table stays readable.

Emoji labels are used on a UTF-8 terminal. When output is piped or redirected, or with `--no-emoji`, it prints text labels (`[CRITICAL]`, `[WARNING ]`, `[INFO    ]`) instead.

### Controller health

`diagnose` also reads the controller's own subsystem health (`stat/health`: `wlan`, `lan`, `wan`, `www`, `vpn`) so it agrees with the UniFi dashboard. The controller sets `lan`/`wlan` to `error` or `warning` whenever any device is disconnected, which the per-device findings already report, so that case is a single info line and does not raise severity or the exit code. A `lan`/`wlan` status with no disconnected device to explain it, and any `wan`, `www` or `vpn` status, use the controller's severity (`error` is critical, `warning` is warning). The `www` subsystem also gives internet latency and drops; the drops default is a heuristic because the controller does not document whether the counter is cumulative, so tune `wan_drops_warn`. If `stat/health` cannot be read, the tool warns and skips these checks.

### Port health

For each switch port `diagnose` also checks the controller's port counters (the legacy `stat/device` port tables; skipped with a warning if unavailable):
- **Flapping links:** `link_down_count` is cumulative since the switch booted, so the finding says how long the switch has been up. Several ports sharing one count usually mean a single switch-wide event, such as a reboot or power loss, not a bad cable on each.
- **Dropped packets:** judged as a percentage of the port's packets in that direction (and only with at least `min_packets_for_drop_pct` packets, default 1,000), because a raw count means little on a busy port. Only ports that are up are checked.
- **STP:** an up port whose state is not `forwarding` (for example `blocking`).
- **PoE budget:** used power as a percentage of the switch's total PoE budget; switches without PoE are skipped. The per-port `poe_good` flag is deliberately not used: it is false on every PoE-capable port that simply has no PoE device attached.
- **Uplink speed:** an uplink negotiated below what both ends support (the device's own maximum and the parent's port maximum). A gigabit device on a 2.5G port is at its own maximum and is not flagged.

### Recent events

`diagnose` also reads the controller's event log (the last 24 hours by default) so it notices things that **happened and went away**, which a snapshot of the network right now cannot see (an IP conflict is usually over by the time `diagnose` runs):
- **IP conflicts:** a warning per IP that names the devices involved, the network, how many times it was reported and when last. When one of the devices has a DHCP reservation for that address it says so, and when a device is reserved a *different* address it says that too, which points at a stale lease or a static IP on the device. Over a longer window it also says on how many different days the conflict happened, so a recurring one stands out. For example:

  ```text
  [WARNING ] 10.0.0.50: IP conflict reported 1 time in the last 24h between Guest Laptop and old-printer on Main (most recent 2026-09-30 20:57:30); old-printer holds the reservation for 10.0.0.50
  ```

  The devices come from the event itself (merged across events and de-duplicated by MAC address); an event that does not list any gets the shorter message with just the address.
- **Flapping:** a client that disconnected `event_flap_count` (default 10) or more times in the window, wired and wireless together, is a warning. A device that was unreachable that often is a warning too.
- **Roaming** is normal for phones (one phone here roams about 30 times a day), so a client that roamed `event_flap_count` or more times is only info.
- **Unreachable earlier, online now:** info. A device that is offline right now is left to the existing offline finding.
- **ISP high latency** events: info with the count.

`--since DURATION` changes the window (for example `12h` or `7d`), and `--no-events` (or `--skip events`) skips these checks and the request they need. If the log cannot be read, `diagnose` warns and carries on without them. An event-based warning stays in the output until its event leaves the window, so it keeps `diagnose` at exit code 1 for that long; use a shorter `--since` or `--no-events` for a cron job that should only react to what is wrong right now.

### Wi-Fi quality

`diagnose` checks every connected Wi-Fi client and every AP radio (from the legacy `stat/sta` and `stat/device` data; skipped with a warning if unavailable). Each client finding names the band and the AP it is on.
- **Weak signal:** the client's signal at or below `wifi_weak_signal_dbm`.
- **Retries:** the share of the client's transmissions that were retried, only once it has made at least `wifi_min_attempts` transmissions, because a percentage over a few packets is noise. The 30% default is deliberately high: on a busy 2.4 GHz band many clients retry 20 to 30% of the time because of neighboring networks, which is an environmental condition more than a per-client fault. Lower `wifi_retry_pct` to see more.
- **Satisfaction:** the controller's own 0-100 score, flagged below `wifi_satisfaction_warn`.
- **AP radios:** channel utilization (warning at `radio_util_warn_pct`, critical at `radio_util_critical_pct`), and the same retry and satisfaction limits per radio.

Clients without signal or satisfaction data (the controller omits it for some) and radios that report satisfaction as unknown (`-1`) are never flagged. The controller's `anomalies` field is not used, because it is present on nearly every client.

### Configuration: thresholds and ignore list

Thresholds and an ignore list live in an optional TOML file, read from `./unifi-sentinel.toml` or given with `diagnose --config FILE` (copy [unifi-sentinel.example.toml](../unifi-sentinel.example.toml); the real file is git-ignored because it may name your devices).

```toml
[thresholds]                 # all optional; these are the defaults
resource_warn_pct = 90       # CPU or memory: warning
resource_critical_pct = 98   # CPU or memory: critical
slow_link_mbps = 100         # ports negotiated at or below this: info
wan_latency_warn_ms = 100    # internet latency at or above this: warning
wan_drops_warn = 10          # internet drops at or above this: warning (heuristic)
link_flap_count = 5          # port link-down count since boot at or above this: warning
port_drop_pct = 0.1          # dropped packets, % of a port's packets, at or above this: warning
min_packets_for_drop_pct = 1000 # minimum packets before evaluating drop percentage
poe_warn_pct = 80            # switch PoE budget used at or above this: warning
poe_critical_pct = 95        # switch PoE budget used at or above this: critical
wan_availability_warn_pct = 99   # 24h internet availability below this (%): warning
wan_speed_drop_pct = 70      # last speedtest download below this % of the 30-day median: warning
event_flap_count = 10        # disconnects (or unreachable events) in the window at or above this: warning
wifi_weak_signal_dbm = -75   # Wi-Fi client signal at or below this (dBm): warning
wifi_retry_pct = 30          # client or radio TX retries at or above this (%): warning
wifi_min_attempts = 1000     # client TX attempts needed before its retries are judged
wifi_satisfaction_warn = 50  # client or radio satisfaction below this (%): warning
radio_util_warn_pct = 70     # AP radio channel utilization at or above this: warning
radio_util_critical_pct = 90 # AP radio channel utilization at or above this: critical
reserved_offline_warn_days = 1      # a reserved client offline this many days: warning
reserved_offline_critical_days = 7  # a reserved client offline this many days: critical

[[ignore]]
subject = "Garage AP"        # case-insensitive name; * and ? wildcards
message = "offline"          # case-insensitive substring; every field given must match
reason = "spare AP, kept unplugged on purpose"   # required

[[ignore]]
code = "port.slow_link"      # a finding code, matched exactly (see the code table below)
subject = "* port 2"         # optional with a code: only this port of any switch
reason = "printer only supports 100 Mbps"

[[ignore]]
code = "reservation.offline"
subject = "Test server"
until = 2026-12-31           # optional: the last day the rule applies; after it the finding comes back
reason = "temporary: shut down while I travel"
```

Ignored findings are left out of the output, counted in the summary (`3 warnings (2 ignored)`), and excluded from exit codes, so a known-okay finding cannot fail a cron job. `diagnose --show-ignored` lists them with each rule's reason, so ignores do not hide problems forever. A rule needs a `reason` and at least one of `code`, `subject` and `message`; every field it gives must match.

**Ignoring by code.** `code = "port.slow_link"` silences that check, with or without a `subject` and/or `message` to narrow it. A code survives a change of wording (a rule on the message text does not) and says what you mean (a rule on a device's name silences everything about it). Codes are matched **exactly**: no wildcards, no case folding, no spaces. A code that does not exist is a configuration error that names the closest valid code (a typo must not silently match nothing) and lists the valid ones, which are the `diagnose` codes in the table below and the `audit.*` codes of [Audit](#audit) (one file serves both commands). To find the code of a finding, run `diagnose --json` (every finding has `code`), or `diagnose --show-ignored`, which prints `(code: ...; ignored: reason)` for each suppressed finding. Rules apply the same way in `diagnose` (text, `--json`, `--show-ignored`, exit codes and `--notify`: an ignored finding is never sent), `client` and `topology`. Existing rules without a code behave exactly as before.

**Temporary ignores: `until`.** `until = 2026-12-31` (a TOML date, or the string `"2026-12-31"`) is the last day the rule applies, by the local date and inclusive. From the next day the rule is **expired**: it no longer matches, so its findings are back in the output, in the exit code and in `--notify` (where they count as new), and `diagnose` and `audit` print one warning per expired rule on stderr, for example `Warning: the ignore rule for code "reservation.offline", subject "Test server" expired on 2026-12-31 and no longer applies; delete it or give it a later until date (temporary: shut down while I travel)`. The warning never changes stdout or an exit code (so `--json` stays valid JSON), and with `--watch` it is printed once at the start; a rule that runs out while the watch is running simply stops applying and its findings show up as new. `client` and `topology` apply the date too but print no warning. `--show-ignored` prints `ignored until 2026-12-31: reason` and `--json` adds `until` to those entries (only for rules that have one). Only a date in the form `2026-12-31` is accepted: a date with a time, another spelling (`2026-1-1`, `20261231`), an impossible date or any other value is a configuration error that names the rule, and `until` alone is not a rule (it needs a `code`, `subject` or `message`, and a `reason`).

A missing, unreadable or invalid file (unknown keys, bad values, rules without a reason, an unknown code, a bad `until`) stops `diagnose` with exit code 3 before it contacts the controller. `client`, `topology`, `wan` and `audit` read the same file (`--config FILE`, or `./unifi-sentinel.toml`) for the thresholds and ignore rules they use; the other commands do not. On Python 3.10 the `tomli` package (installed automatically) reads it; 3.11 and later use the standard library.

### JSON output and finding codes

`diagnose --json` prints one JSON document on stdout (warnings still go to stderr) for scripts, dashboards and notifiers. The exit code is the same as without `--json` (`--fail-on` is honored), and `--no-emoji` has no effect. Findings suppressed by the ignore list are counted in `summary.ignored`; they are listed under a separate `ignored` key (each with its rule's `reason`, and its `until` date if the rule has one) only with `--show-ignored`. A configuration error prints nothing on stdout and exits 3.

```json
{
  "version": 1,
  "areas": ["devices", "health", "wan", "clients", "reservations", "ports", "wifi"],
  "summary": {
    "critical": 0,
    "warning": 10,
    "info": 2,
    "ignored": 0
  },
  "findings": [
    {
      "severity": "warning",
      "code": "device.offline",
      "subject": "Garage AP",
      "message": "device is offline",
      "mac": "AA:00:00:00:00:04"
    },
    {
      "severity": "warning",
      "code": "reservation.outside_subnet",
      "subject": "old-printer",
      "message": "reserved IP 10.0.0.50 is outside network IoT (10.0.20.1/24)",
      "mac": ""
    },
    {
      "severity": "info",
      "code": "port.slow_link",
      "subject": "Office Switch port 2",
      "message": "negotiated at 100 Mbps",
      "mac": "AA:00:00:00:00:02"
    }
  ]
}
```

(The example is from the synthetic fixture with `--no-events`; the findings are shortened, so they do not add up to the summary.)

- **`version`** is the document format (currently `1`); it changes only when a field is removed or renamed. New fields may be added without a new version.
- **`areas`** lists the areas of checks that ran, in a fixed order (`devices`, `health`, `wan`, `clients`, `reservations`, `ports`, `wifi`, `events`; with `--no-events` or `--skip` the skipped ones are missing). An empty `findings` list only means "nothing found" for these areas.
- **`code`** names the check that produced the finding, so a script does not have to match wording that can change. Codes are an interface: they are never renamed or reused. The same finding can have a different `severity` between runs (a device offline is critical for a gateway, a warning otherwise), so key on `code` and `subject`.
- **`subject`** and **`message`** are the same text as the plain output, with names exactly as the controller reports them (JSON escapes control characters; treat them as untrusted data if you pass them on).
- **`mac`** is the upper-case MAC address of the device the finding is about, or an empty string when it is not about one device.
- `client --json` and `topology --json` carry the same `code` in each of their findings.

| Code | What it reports |
| ---- | --------------- |
| `client.link_local_ip` | a connected client has a link-local (169.254.x.x) address |
| `client.no_ip` | a connected client has no IP address |
| `client.private_mac_summary` | how many connected clients use randomized (private) MAC addresses |
| `controller.legacy_unavailable` | legacy device data could not be read, so the port and overheating checks were skipped |
| `controller.pending_adoption` | devices waiting to be adopted |
| `device.cpu_high` | device CPU utilization at or above the warning threshold |
| `device.memory_high` | device memory utilization at or above the warning threshold |
| `device.offline` | a UniFi device is not online (critical for a gateway or a device that others uplink through) |
| `device.overheating` | an online UniFi device reports that it is overheating |
| `event.client_disconnects` | a client disconnected repeatedly in the event window |
| `event.client_roams` | a client roamed repeatedly in the event window |
| `event.device_unreachable` | a device was reported unreachable in the event window |
| `event.internet_latency` | the controller reported high internet latency in the event window |
| `event.ip_conflict` | the controller reported an IP conflict in the event window |
| `event.log_truncated` | the event log read hit its cap, so event counts may be low |
| `health.device_subsystem` | lan/wlan subsystem status that only reflects disconnected devices |
| `health.subsystem` | a controller health subsystem is in a warning or error state |
| `internet.drops` | internet drops at or above the threshold |
| `internet.latency` | internet latency at or above the threshold |
| `internet.speedtest_failed` | the last speedtest failed |
| `ip.duplicate` | the same IP is in use by several clients or devices |
| `link.below_capability` | an uplink negotiated below what both ends support |
| `port.drops` | a switch port is dropping packets above the threshold |
| `port.errors` | a port has rx/tx errors |
| `port.half_duplex` | a port link is half duplex |
| `port.link_flaps` | a switch port's link has gone down repeatedly since boot |
| `port.poe_budget` | a switch's PoE budget use is at or above the threshold |
| `port.slow_link` | a port negotiated at or below the slow-link speed |
| `port.stp` | an up port is not in the STP forwarding state |
| `reservation.duplicate` | the same IP is reserved for several clients |
| `reservation.in_dhcp_pool` | a reserved IP lies inside its network's dynamic DHCP range |
| `reservation.ip_in_use` | a reserved IP is in use by a different client or device |
| `reservation.ip_mismatch` | an online client's IP differs from its reservation |
| `reservation.never_seen` | a reservation whose client has no last-seen time |
| `reservation.offline` | a client with a reservation has been offline longer than the threshold |
| `reservation.outside_subnet` | a reserved IP is outside its network's subnet |
| `reservation.pool_unknown` | a network's DHCP range is missing or invalid, so its reservations cannot be checked |
| `reservation.private_mac` | a reservation is tied to a randomized (private) MAC address |
| `wan.availability` | 24-hour internet availability below the threshold |
| `wan.cgnat` | the WAN address is in the carrier-grade NAT range (100.64.0.0/10) |
| `wan.double_nat` | the WAN address is private: the gateway is behind another router doing NAT |
| `wan.link_local_address` | the WAN address is link-local: the gateway got no address from the ISP |
| `wan.monitor_availability` | one monitored internet target below the availability threshold |
| `wan.speedtest_slow` | the last speedtest download is well below the 30-day median |
| `wifi.client_retries` | a Wi-Fi client retries too many transmissions |
| `wifi.client_satisfaction` | a Wi-Fi client's satisfaction is below the threshold |
| `wifi.radio_retries` | an AP radio retries too many transmissions |
| `wifi.radio_satisfaction` | an AP radio's satisfaction is below the threshold |
| `wifi.radio_utilization` | an AP radio's channel utilization is at or above the threshold |
| `wifi.weak_signal` | a Wi-Fi client's signal is at or below the threshold |

## Audit

`audit` answers "what is configured in a way I probably did not intend?", where `diagnose` answers "what is wrong now?". It reads settings (all with GET) and prints findings in the same format. Shown with `--no-emoji`:

```text
[WARNING ] GuestNet: is a guest network whose clients can reach each other (client isolation is off)
[WARNING ] Lobby: is an open network: anyone in range can join and read unencrypted traffic
[WARNING ] OldCam: uses WEP, which can be broken in minutes; use WPA2 or WPA3
[INFO    ] GuestNet: offers WPA2 only; WPA3 is not enabled
[INFO    ] Office Switch: firmware update available

3 warnings, 2 info
```

| Code | Severity | Meaning |
| ---- | -------- | ------- |
| `audit.wifi_open` | warning (info for a guest network) | An enabled Wi-Fi network has no password |
| `audit.wifi_weak_encryption` | warning | An enabled Wi-Fi network uses WEP |
| `audit.wifi_no_wpa3` | info | An enabled WPA2 network does not offer WPA3 |
| `audit.wifi_guest_no_isolation` | warning | A guest network whose clients can reach each other (client isolation is off) |
| `audit.wifi_unavailable` | info | The Wi-Fi settings could not be read, so the Wi-Fi checks did not run |
| `audit.default_device_name` | info | A UniFi device has no name, is named after its model, or is named after its MAC address (or the last three bytes of it) |
| `audit.firmware_update` | info | A UniFi device has a firmware update available |
| `audit.unnamed_clients` | info | One summary for the known clients (including offline ones) with neither a name nor a hostname, with the first five by MAC and vendor |

- **Options:** `--fail-on {info,warning}` (default `warning`), `--config FILE` for the ignore list (`subject`, `message` and `until` rules work as in `diagnose`; a deliberately open lobby network is `subject = "Lobby"`, `message = "open network"`), `--show-ignored`, `--no-emoji` and `--json`. The JSON document is the one of `diagnose --json` (`version`, `areas`, `summary`, `findings` with `severity`, `code`, `subject`, `message` and `mac`); its `areas` are `wifi`, `devices` and `clients`.
- **Exit codes:** the same rules as `diagnose`: `1` when there is a finding at or above `--fail-on` (the audit never produces a critical one, so never `2`), `0` otherwise, `3` when the controller cannot be read.
- **Data:** the Wi-Fi networks come from the legacy `rest/wlanconf` (one record per network with `security`, `wpa3_support`, `is_guest`, `l2_isolation` and `enabled`; the Integration API's list has no guest flag). Disabled networks are skipped. The device checks use the device list (`name`, `model`, `macAddress`, `firmwareUpdatable`) and the client check the client history (`stat/alluser`), so no per-device or legacy device requests are made.
- **Verified and not verified:** checked against one controller on Network 10.6.106 with three WPA2/WPA3 networks, so none of the Wi-Fi findings fire there. `security` is `wpapsk` on those networks; `open` and `wep` are the legacy API's other values and are matched as such but were not seen live. No device there had a firmware update or a default name, so those findings come from the field names and the fixture. A default name is a heuristic: no name, the model name, the MAC address, or a name that ends with the last three bytes of the device's own MAC.
- **Dropped, and why:** *a port's native VLAN against the network of the client on it.* On the controller checked, 7 of the 21 wired clients sat on a different network than their port's native one, and all 7 were on trunk ports (`forward` of `all` or `customize` with tagged VLANs allowed), where that is normal; the port profile list was empty, and the only reliable subset (access ports) had no example, so the check would be wrong more often than right. *Hidden SSIDs:* hiding a network name is not a configuration risk that isolation or encryption fixes, so it is not flagged.
