"""Synthetic large sites for the scale tests: every address and name is generated, none is real."""

from homelab_probe.snapshot import Snapshot


def mac(prefix, i):
    return f"{prefix}:{(i >> 16) & 255:02x}:{(i >> 8) & 255:02x}:{i & 255:02x}"


def big_site(n_clients, n_devices=20):
    """A site with ``n_clients`` connected clients (a third wired), some offline ones, reservations, groups and
    ``n_devices`` switches and access points under one gateway."""
    devices, legacy_devices = [], []
    gw_mac = "AA:00:00:00:00:01"
    devices.append({"id": "gw", "name": "Gateway", "macAddress": gw_mac, "ipAddress": "10.0.0.1", "state": "ONLINE",
                    "model": "UCG", "features": ["gateway"]})
    legacy_devices.append({"mac": gw_mac.lower(), "name": "Gateway", "type": "udm", "uplink": None, "port_table": []})
    switches = []
    for i in range(n_devices):
        kind = "usw" if i % 2 == 0 else "uap"
        m = mac("AA:01", i + 2).upper()
        devices.append({"id": f"d{i}", "name": f"Dev {i}", "macAddress": m, "ipAddress": f"10.0.1.{i + 2}",
                        "state": "ONLINE", "model": "X"})
        ports = [{"port_idx": p, "up": True, "speed": 1000, "full_duplex": True, "rx_packets": 5000, "rx_dropped": 0,
                  "tx_packets": 5000, "tx_dropped": 0, "link_down_count": 0, "stp_state": "forwarding"} for p in range(1, 25)]
        legacy_devices.append({"mac": m.lower(), "name": f"Dev {i}", "type": kind, "port_table": ports if kind == "usw" else [],
                               "uplink": {"uplink_mac": gw_mac.lower(), "uplink_remote_port": i + 1, "speed": 1000, "max_speed": 1000},
                               "radio_table_stats": [] if kind == "usw" else [{"radio": "ng", "channel": 6, "cu_total": 20}]})
        if kind == "usw":
            switches.append(m.lower())
    aps = [d["mac"] for d in legacy_devices if d["type"] == "uap"]
    clients, legacy_clients, all_users = [], [], []
    for i in range(n_clients):
        m = mac("BB:02", i + 1)
        wired = i % 3 == 0
        ip = f"10.{1 + (i >> 16) % 200}.{(i >> 8) & 255}.{i & 255}"
        clients.append({"id": f"c{i}", "name": f"client-{i}", "type": "WIRED" if wired else "WIRELESS", "macAddress": m,
                        "ipAddress": ip, "connectedAt": "2026-01-01T00:00:00Z", "uplinkDeviceId": "d0"})
        sta = {"mac": m, "name": f"client-{i}", "ip": ip, "is_wired": wired}
        if wired:
            sta.update(sw_mac=switches[i % len(switches)], sw_port=1 + i % 24)
        else:
            sta.update(ap_mac=aps[i % len(aps)], radio="ng", channel=6, signal=-60, satisfaction=90)
        legacy_clients.append(sta)
        all_users.append({"mac": m, "name": f"client-{i}", "last_ip": ip, "is_wired": wired, "first_seen": 1, "last_seen": 2,
                          "use_fixedip": i % 10 == 0, "fixed_ip": ip, "last_connection_network_id": "net-1",
                          "network_members_group_ids": ["g1"] if i % 2 else []})
    for i in range(n_clients // 5):   # offline known clients
        m = mac("CC:03", i + 1)
        all_users.append({"mac": m, "name": f"old-{i}", "last_ip": f"10.9.{(i >> 8) & 255}.{i & 255}", "is_wired": False,
                          "first_seen": 1, "last_seen": 2, "use_fixedip": i % 20 == 0, "fixed_ip": f"10.9.{(i >> 8) & 255}.{i & 255}",
                          "last_connection_network_id": "net-1"})
    return Snapshot(site={"id": "s", "name": "Big"}, devices=devices, clients=clients, legacy_devices=legacy_devices,
                    legacy_clients=legacy_clients, all_users=all_users,
                    networks=[{"_id": "net-1", "name": "Main", "ip_subnet": "10.0.0.1/8", "vlan_enabled": False,
                               "dhcpd_enabled": True, "dhcpd_start": "10.0.0.100", "dhcpd_stop": "10.0.0.200"}],
                    client_groups=[{"id": "g1", "name": "G"}])
