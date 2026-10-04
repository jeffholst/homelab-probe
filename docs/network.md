# Network views

The commands that show how the network is wired and how it is doing: `topology`, `wifi`, `wan`, `firewall`, `events` and `client`.

## Topology

`topology` draws how the network is wired, so a broken or slow path is visible at a glance:

```text
Gateway (UCG Max)   [CRITICAL x2]
`-- port 2 -> Office Switch (100 Mbps, supports 1000)   1 client   [WARNING x8]
    +-- port 2 -> Office AP   1 client
    `-- port 5 -> Garage AP   [OFFLINE]   [WARNING]

Findings on these devices:
  [CRITICAL] Gateway: reports that it is overheating
  [WARNING ] Gateway Backup: storage 97.5% used
  [WARNING ] Office Switch: CPU utilization 95%
  [WARNING ] Office Switch: PoE budget 41.6 W of 52 W used (80%)
  [WARNING ] Office Switch: uplink to Gateway negotiated at 100 Mbps but both ends support 1000 Mbps
  [WARNING ] Office Switch port 1: link has gone down 5 times since boot, switch up 3h 12m
  [WARNING ] Office Switch port 2: 4 rx/tx errors
  [WARNING ] Office Switch port 2: link is half duplex
  [WARNING ] Office Switch port 2: dropping 0.75% of rx packets (75 of 10000)
  [WARNING ] Office Switch port 2: STP state is blocking, not forwarding
  [WARNING ] Garage AP: device is offline

4 devices, 2 clients, 1 offline, 1 link(s) below capability, 3 with findings
```

Each line is `port N -> device`, where N is the **parent's** port the device plugs into, followed by the negotiated link speed, the number of connected clients (wired by switch port, wireless by AP), and flags.
- **Link speed:** shown when known. `supports 1000` means the link negotiated below what both ends support (the same check `diagnose` makes), so it points at a bad cable, port or device. A gateway's own uplink is its internet connection and is not drawn.
- **Flags:** `[OFFLINE]` for a device the controller reports as offline, and a warning or critical marker (`⚠️ 2`, or `[WARNING x2]` in plain text) when `diagnose` has findings about the device or one of its ports or radios. Those findings are listed under the tree. Info-level findings, such as ports at 100 Mbps, are left to `diagnose` so the flags mean something. It uses the same thresholds and ignore list as `diagnose` (`--config FILE`, or `./unifi-sentinel.toml`).
- **Order:** children are sorted by the parent's port number, then by name.
- **Unattached:** a device that cannot be reached from a gateway is listed separately with the reason: no uplink information, an uplink to an unknown device, or an uplink loop. Nothing silently disappears. An offline device's position is its last known one.
- `--clients` lists the wired clients under each device with their port. `--json` prints the nested tree (and `--clients` adds `wired_clients`). `--no-emoji` forces plain ASCII drawing and text labels, which is also used automatically when output is not a UTF-8 terminal.

The uplink and port data comes from the legacy `stat/device` and `stat/sta` data and the Integration API device detail, which is used for the parent when the legacy data has none (then no port number is shown).

## Wi-Fi

`wifi` describes your wireless side: each AP's radios, and who else is on the air near them.

```text
Access points
AP         Band     Channel  Width   Power   Clients  Utilization  Retries  Satisfaction
---------  -------  -------  ------  ------  -------  -----------  -------  ------------
Office AP  2.4 GHz  6        20 MHz  22 dBm  4        30%          5%       99%
Office AP  5 GHz    36       80 MHz  26 dBm  6        10%          3%
  Garage AP (offline): no radio data

Neighboring networks: 9 seen by your APs (8 stronger than -80 dBm, 1 open, 1 with a hidden name)

2.4 GHz
Channel  Neighbors  Strong  Your radios
-------  ---------  ------  -----------
1        1          0
4        1          1
6        3          3       Office AP
11       2          2

  Channel 4: strongest of 1 stronger than -80 dBm
    Adjacent Net  (-60 dBm, WPA2-Personal (AES/CCMP))

  Channel 6: strongest of 3 stronger than -80 dBm
    Neighbor One  (-45 dBm, WPA2-Personal (AES/CCMP), heard by 2 APs)
    (hidden, Acme Corp)  (-66 dBm, WPA2-Personal (AES/CCMP))
    Café Guest ☕  (-72 dBm, Open)  [OPEN]

  Channel 11: strongest of 2 stronger than -80 dBm
    Eleven Net  (-50 dBm, WPA2-Personal (AES/CCMP))
    Line Break xxxxxxxxxxxxxxxxxxxxxxxxxxxx…  (-70 dBm, WPA2-Personal (AES/CCMP))

5 GHz
Channel  Neighbors  Strong  Your radios
-------  ---------  ------  -----------
36       0          0       Office AP
44       1          1
149      1          1

  Channel 44: strongest of 1 stronger than -80 dBm
    Five GHz Neighbor  (-55 dBm, WPA2-Personal (AES/CCMP))

  Channel 149: strongest of 1 stronger than -80 dBm
    Far Block  (-70 dBm, WPA2-Personal (AES/CCMP))

Observations
  - Office AP 2.4 GHz (channel 6): 3 neighbors stronger than -80 dBm on the same channel, 1 overlapping it
  - Office AP 5 GHz (channel 36): 0 neighbors stronger than -80 dBm on the same channel, 1 overlapping it
  - Of the usual 2.4 GHz channels, channel 1 overlaps the fewest neighbors (channel 1: 1, channel 6: 4, channel 11: 2), stronger than -80 dBm
```

- **Access points:** one row per radio with the channel it is actually using (even when set to auto), width, transmit power, connected clients, channel utilization, retry rate and the controller's satisfaction score (blank when the controller reports it as unknown). An AP with no radio data, such as an offline one, is listed so it does not vanish.
- **Neighbors are counted once per network.** The controller's scan returns one row for every AP that hears a network, so counting rows would count one neighbor several times; `wifi` merges them by BSSID, keeps the strongest reading and says how many of your APs heard it. Your own networks never count as neighbors (they are recognized by their BSSIDs, which the AP data lists).
- **Strong:** neighbors at or above `--min-signal` (default -80 dBm). Weaker ones are still counted in the totals but are not named or compared, which is what keeps a list of dozens readable. At most 5 strong neighbors are named per channel; `--all` names every one. Hidden networks show as `(hidden)` with the equipment vendor when known, and open networks are marked `[OPEN]`.
- **Overlap, not just the channel number.** 2.4 GHz channels are 5 MHz apart but about 22 MHz wide, so a neighbor on channel 4 disturbs both channel 1 and channel 6; a 5 GHz radio with an 80 MHz width occupies a block of channels. 2.4 GHz channel 14 (Japan) is centered on 2484 MHz, 12 MHz above channel 13 rather than 5, and the 5 GHz U-NII-4 channels 165 to 177 form their own 40, 80 and 160 MHz blocks (149 to 177 is one 160 MHz block). For 40 MHz 2.4 GHz radios, the reported center or extension channel is used; without it, the frequency span is unknown. The observations count neighbors on your radio's channel and neighbors that merely overlap it. The observations also point out your own radios that compete with each other and which of the usual 2.4 GHz channels (1, 6, 11) overlaps the fewest strong neighbors. They only describe; they never tell you what to change.
- If the neighbor scan fails, text marks neighbor counts `n/a` and skips neighbor-based observations. JSON sets `neighbors.available` to `false` and the unavailable totals and per-channel counts to `null`.
- `--band 2.4|5|6` and `--ap NAME` filter (`--ap` also limits the neighbors to those that AP hears); `--json` prints everything.
- **6 GHz:** the controller's neighbor scan reports no 6 GHz networks, so those cells say `n/a` and no claim is made about that band.
- **Neighbor names are shown, as the controller reports them.** They identify other households' networks, so check the output before pasting it into an issue or sharing it. The names above are synthetic.

## WAN

`wan` answers "is it my internet or my LAN?" from data the controller already keeps (all read with GET):

```text
Internet: ok (Example ISP)
  WAN IP: 192.0.2.10
  Gateway: Gateway
  NAT: none seen (public WAN address; a modem doing NAT in front of the gateway cannot be seen)
  Now: latency 20 ms, 0 drops, status ok
  Link wan1: eth4 up, 1000 Mbps full duplex (port supports 2500 Mbps)
    live: 1.0 Mbps up, 2.0 Mbps down

Last 24h (controller monitoring, WAN): availability 100.0%, average latency 23 ms
Target       Type  Availability  Latency  Alerts
-----------  ----  ------------  -------  ------
192.0.2.53   dns   100.0%        22 ms    yes
example.com  icmp  100.0%        20 ms
example.org  icmp  100.0%        24 ms

Speedtests, last 30 days (11 runs), 12 stored
  Last: 2026-10-01 16:11 (6h ago): download 880 Mbps, upload 40 Mbps, latency 25 ms
  Download: min 500 Mbps, median 925 Mbps, max 940 Mbps
  Upload: min 31 Mbps, median 40 Mbps, max 41 Mbps
  Latency: min 23 ms, median 24 ms, max 41 ms

  Download below 70% of the median (1):
    2026-09-23 22:11  download 500 Mbps, upload 31 Mbps, latency 41 ms
```

- **Now:** the WAN and internet subsystems of the controller's health (status, ISP, WAN IP, latency, drops) and the gateway's WAN link: its negotiated speed against what the port supports (a 1 Gbps plan on a 2.5 Gbps port is normal, so that is only shown, never flagged) and the live traffic rate.
- **NAT:** whether the gateway is behind NAT, judged from the address of its WAN port (the `wan_ip` of the controller's health, nothing is looked up outside your network). A **private** address (10.x, 172.16 to 172.31, 192.168.x, or an IPv6 `fc00::/7`) means another router that does NAT sits in front of the gateway (**double NAT**); an address in **100.64.0.0/10** is carrier-grade NAT, where the ISP shares one public address between customers; a **link-local** address (169.254.x.x or IPv6 `fe80::/10`) means the gateway got no address from the ISP. Double NAT and carrier-grade NAT break inbound port forwards and some VPNs, game and camera features. `--json` has it under `nat` (`wan_ip`, `kind` of `public`, `private`, `cgnat`, `link_local`, `none` or `unknown`, and `message`). **Limits:** a public-looking address does not prove there is no NAT, because a modem or router in front of the gateway that translates addresses while handing the gateway a public one cannot be seen without an outside lookup, which this tool does not make; and only the primary WAN is checked (the `wan_ip` of the health entry), not a second WAN.
- **Last 24 hours:** the controller's own monitoring of the connection: overall availability and average latency, and each monitoring target (`icmp` ping or `dns`) with its availability and latency. `Alerts: yes` marks a target the controller is configured to alert on; it does not mean the target is failing.
- **Speedtests:** every stored result (the controller runs them on a schedule) over `--days N` (default 30), using the latest run's `wan_networkgroup` or `interface_name` so dual-WAN links are not mixed: the last one and how old it is, the minimum, median and maximum of download, upload and latency, and the runs whose download fell below `wan_speed_drop_pct` (default 70%) of the median, with their dates. That is where a degradation window shows up; try `--days 90`. At least 5 runs are needed before the median means anything.
- `--json` prints the same data, and `--config FILE` (or `./unifi-sentinel.toml`) sets the threshold. A missing piece (no speedtests stored, no monitoring data, a gateway with no WAN link data) is simply left out.
- **`diagnose` uses the same data:** a warning (`wan.double_nat`, `wan.cgnat` or `wan.link_local_address`) when the NAT check above finds a private, shared or link-local WAN address (silence a deliberate double NAT with an ignore rule: `subject = "wan"`, `message = "double NAT"`), a warning when 24-hour availability, overall or for any single monitoring target, is below `wan_availability_warn_pct` (default 99%), and a warning when the last speedtest (within 30 days) is below `wan_speed_drop_pct` of the 30-day median, with its age.
- **Not included:** an hourly traffic and latency history. The controller only returns that from a POST to its report endpoint, which is outside the one approved POST (the event log); a plain GET returns empty rows.

## Firewall

`firewall` answers "what does my firewall allow, and what is reachable from the internet?" (all read with GET). Shown with `--no-emoji`; the findings use the same severity marks as `diagnose` (the command never changes the exit code):

```text
Firewall: zone-based (7 zones, 6 policies shown)

Port forwards
Name                On   Protocol  External port  Forwards to      Interface  Only from
------------------  ---  --------  -------------  ---------------  ---------  ------------
Web Server          yes  TCP       443            10.0.0.10:443    WAN
Phone Test          yes  TCP       8080           10.0.0.11:8080   WAN
Game Server         yes  UDP       27015          10.0.0.77:27015  WAN
Game Server Backup  yes  UDP       27015          10.0.0.10:27016  WAN
Old FTP             no   TCP       21             10.0.0.50:21     WAN        198.51.100.7

Policies of your own (6 built-in policies not shown, use --all)
Name               Action  On   From      To        Source                      Destination                      Protocol  Hits
-----------------  ------  ---  --------  --------  --------------------------  -------------------------------  --------  ----
Open Inbound       allow   yes  External  Internal  any                         any                              any
Admin SSH          allow   yes  Internal  Gateway   10.0.0.10 port 49152-65535  any port 22                      TCP       9
Guest Printer      allow   yes  Internal  IoT Zone  any                         a network that no longer exists  TCP/UDP
Allow IoT DNS      allow   yes  IoT Zone  Internal  IoT                         10.0.0.53 port 53                TCP/UDP   42
Old Camera Access  allow   no   IoT Zone  Internal  no network left             any                              any
Legacy VPN Allow   allow   no   Vpn       Internal  any                         any                              any

Findings
[WARNING ] Open Inbound: allows all traffic from the External zone to Internal
[INFO    ] Old Camera Access: source matches specific networks but lists none (the network was probably deleted); the rule is switched off
[WARNING ] Guest Printer: destination matches a network that no longer exists
[INFO    ] policies: 2 rules of your own are switched off
[INFO    ] Phone Test: TCP port 8080 to 10.0.0.11:8080; that client has no DHCP reservation, so the forward breaks if its address changes
[WARNING ] Game Server: UDP port 27015 to 10.0.0.77:27015, but nothing is using that address now
[WARNING ] Game Server Backup: uses UDP external port 27015 like 'Game Server'

4 warnings, 3 info
```

- **Port forwards** (legacy `rest/portforward`): name, whether it is on, protocol, external port, the internal address and port, the WAN interface, and the only source address it accepts (blank for any).
- **Policies:** only the ones you defined by default, because a zone-based controller also holds a long list of built-in ones (`--all` shows them too, marked by the count of what is hidden). Columns: the rule's action, whether it is on, the zone the traffic comes **From** and goes **To**, what the **Source** and **Destination** match (`any`, the networks by name, addresses, or the kind of target, each with its port when it matches one; `not` in front when the match is inverted), the protocol and how many times the rule has matched (`Hits`, blank when it never did). Ordered by zone pair, then the controller's rule order.
- **`--zones`** adds each zone with its networks and the **zone matrix**: for traffic from the row's zone into the column's zone, `A` allows all, `B` blocks all, `R` allows return traffic only, `C` means custom rules decide and `-` that nothing is defined.
- **`--search TEXT`** keeps the policies and port forwards with that text in any column; `--json` prints the same data (`version`, `style`, `policies`, `port_forwards`, `zones`, `matrix`, `findings` and `notes`) with the names untouched.
- **Findings:**

  | Code | Severity | Meaning |
  | ---- | -------- | ------- |
  | `firewall.forward_target_offline` | warning | An enabled port forward points at an address that no connected client or UniFi device is using |
  | `firewall.forward_no_reservation` | info | An enabled port forward points at a client that has no DHCP reservation, so it breaks when the address changes |
  | `firewall.forward_duplicate` | warning | Two enabled port forwards use the same protocol, external port and interface |
  | `firewall.allow_any_from_external` | warning | An enabled rule of your own allows all protocols and ports from anywhere in the External zone to a zone |
  | `firewall.rule_missing_network` | warning (info when the rule is off) | A rule matches specific networks but lists none, or lists one that is gone, which usually means the network was deleted |
  | `firewall.disabled_rules` | info | How many rules of your own are switched off |

  Built-in policies are never judged. The codes are fixed (listed in `firewall.FIREWALL_CODES`) like the `diagnose` ones; they are not part of `diagnose --json`.
- **Data and limits:** checked against one controller on Network 10.6.106 that uses the **zone-based** firewall: the policies come from the v2 `firewall-policies`, `firewall/zone` and `firewall/zone-matrix` endpoints (the Integration API lists fewer policies and has no ports or hit counts). That controller had **no port forwards**, so the port forward fields are the legacy ones and are not verified against live data. The **classic firewall** (rules and groups) is not shown: on a controller without zone-based policies the command says so, with a warning for each endpoint that did not answer, and still lists port forwards. A policy can also match a client, a region or a group; those are shown as the kind of target only.

## Event history

`events` reads the controller's event log, so it can answer "why did the Wi-Fi drop at 3 pm?", which none of the other commands can because they show the network as it is now. The controller keeps about three months.

```text
uv run unifi-sentinel.py events --client phone --since 6h
Time                 Severity  Category        Event                         Message
-------------------  --------  --------------  ----------------------------  --------------------------------------------------
2026-10-02 03:32:28  Low       CLIENT_DEVICES  CLIENT_DISCONNECTED_WIRELESS  phone disconnected from Home. Time Connected: 25s.
2026-10-02 03:17:28  Low       CLIENT_DEVICES  CLIENT_CONNECTED_WIRELESS     phone connected to Home on Office AP.
2026-10-02 03:02:28  Low       CLIENT_DEVICES  CLIENT_DISCONNECTED_WIRELESS  phone disconnected from Home. Time Connected: 2m.
2026-10-02 02:52:28  Low       CLIENT_DEVICES  CLIENT_ROAMED                 phone roamed from Garage AP to Office AP.
2026-10-02 02:42:28  Low       CLIENT_DEVICES  CLIENT_DISCONNECTED_WIRELESS  phone disconnected from Home. Time Connected: 1h.

5 event(s)
```

Options (the filters combine with AND; the first group is done by the controller, the second by this tool):
- `--since DURATION`: how far back, such as `90m`, `24h`, `7d` or `2w` (default `24h`)
- `--category NAME` (repeatable): for example `CLIENT_DEVICES`, `UNIFI_DEVICES`, `INTERNET_AND_WAN` or `AUDIT`
- `--severity low|medium|high` (repeatable) and `-s TEXT` or `--search TEXT` (text search)
- `--event TEXT`: event types containing TEXT, such as `roam`, `disconnected` or `ip_conflict`
- `--client NAME|MAC|IP` and `--device NAME|IP`: events about that client or UniFi device (MAC fragments of six or more hex digits work)
- `--limit N`: the newest N events (default 100; `0` for all). At most 20,000 events are read from the controller per run.
- `--summary`: instead of a list, counts by severity and event type over the whole window (it ignores `--limit`) and the noisiest client or device per event type, which is where a flapping device or client shows up
- `--json`: the events as JSON

Some audit events have no value for part of their message; those parts show as `<setting name>` and similar.

### The one POST, and why it is safe

The event log has no GET endpoint. The controller only answers a POST that carries the time range and filters, and the request only *reads*: it returns events and changes nothing (reading does not mark events as read, and two identical queries return identical data). To keep the read-only promise checkable:
- the POST is sent only by `UniFiClient.system_log` (used by `events`, and by `diagnose` and `client` unless `--no-events`), to the one fixed `system-log/all` path, and the request body may only contain the documented query keys (anything else is rejected before anything is sent);
- `UniFiClient` has no general-purpose POST, PUT, PATCH or DELETE method;
- the test suite fails if any other code sends a POST, PUT, PATCH or DELETE, or if a second POST appears in `client.py`.

## Client view

`client <name|mac|ip>` answers "why is this device slow or offline?" in one place:

```text
desktop
  MAC:        BB:00:00:00:00:01
  Status:     Online, connected since 2026-01-01 09:00:00
  Connection: Wired
  IP:         10.0.0.10  (reserved 10.0.0.10, matches)
  Network:    Main (VLAN 1)
  Groups:     Desktops
  First seen: 2020-09-13 12:26:40
  Last seen:  connected now

Attached: desktop -> Office Switch port 3 (1000 Mbps) -> Gateway port 2 (100 Mbps)
Link:     1000 Mbps, full duplex, 0 errors, 60 dropped packets on its port

Recent events (last 24h, newest first):
  2026-10-02 02:12:28  CLIENT_CONNECTED_WIRED: desktop connected to Main on Office Switch Port 3.

Related findings:
[CRITICAL] Gateway: reports that it is overheating
[WARNING ] Office Switch: CPU utilization 95%
[WARNING ] Office Switch: PoE budget 41.6 W of 52 W used (80%)
[WARNING ] Office Switch: uplink to Gateway negotiated at 100 Mbps but both ends support 1000 Mbps

1 critical, 3 warnings
```

- **Finding the client:** an exact MAC (any separator or case), an exact IP, a single exact name, then a case-insensitive part of a name or hostname (or a MAC fragment of six or more hex digits). It looks across every client the controller knows, connected or not, but never UniFi devices. If several clients match it lists up to 20 of them and exits with code 4 instead of guessing; no match also exits 4.
- **Attached:** the switch port (or AP, with band, channel and SSID) and each parent up to the gateway, with the parent's port and the negotiated link speed. Offline devices on the path are marked `OFFLINE`. An offline client shows the last uplink the controller recorded.
- **Link:** for a wired client, its port's speed, duplex, errors and dropped packets; for Wi-Fi, signal, noise, rates, retries and satisfaction. Offline clients have none.
- **Addressing:** the DHCP reservation and whether it matches the current IP, the network and VLAN, and the client groups by name (or that it is in none).
- **Related findings:** the `diagnose` findings about this client, its IP, or the devices and ports on its path (not unrelated ports on the same switch). It uses the same thresholds and ignore list as `diagnose` (`--config FILE`, or `./unifi-sentinel.toml`).
- **Recent events:** the client's events from the controller's event log (the last 24 hours by default; `--since DURATION` changes it, for example `12h` or `7d`), newest first, up to 10, then how to see the rest with `events --client MAC`. The client is matched by its MAC address, so a similarly named device never mixes in. A second list shows events about the devices on its path, matched by device ID (name only when an ID is unavailable), but only device-state events (a switch or AP going unreachable or reconnecting), up to 5: not other clients connecting to the same AP, and not internet-latency events, which also name the gateway but do not explain why one client dropped. That is how a client's disconnect lines up with the switch outage that caused it.
- `--no-events` skips this section and the request it needs (the one approved read-only event-log query, see [Event history](#event-history)); with it, `client` sends no POST at all. If the log cannot be read, the rest of the view is shown with "Recent events: unavailable".
- `--json` prints the same data as JSON, with `events`, `device_events`, `events_window`, `events_omitted` (how many were left out), `events_truncated` (`true` if the 20,000-event read cap was reached; omission counts may then be incomplete, or `null` when events are unavailable/not requested) and `events_available` (`true`, `false` when the log could not be read, or `null` with `--no-events`). Text output says "at least" for omission counts and notes when counts are incomplete; it avoids claiming there were no matches when the log was truncated. `--no-emoji` forces text severity labels.
