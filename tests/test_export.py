from unifi_sentinel.export import build_offline_clients


def test_offline_clients_exclude_connected_and_devices():
    connected = [{"macAddress": "aa:aa"}]
    devices = [{"macAddress": "bb:bb"}]
    all_users = [
        {"mac": "aa:aa", "name": "connected"},
        {"mac": "bb:bb", "name": "device"},
        {"mac": "cc:cc", "hostname": "old-laptop", "is_wired": False,
         "last_seen": 1700000000, "last_ip": "10.0.0.5"},
    ]
    rows = build_offline_clients(connected, devices, all_users)
    assert len(rows) == 1
    row = rows[0]
    assert row["MAC Address"] == "CC:CC"
    assert row["Name"] == "old-laptop"
    assert row["Status"] == "Offline"
    assert row["Connection Type"] == "Wireless"
    assert row["IP Address"] == "10.0.0.5"
    assert row["Last Seen"] != ""


def test_offline_wired_client_gets_last_uplink():
    rows = build_offline_clients([], [], [
        {"mac": "dd:dd", "is_wired": True,
         "last_uplink_name": "Switch A", "last_uplink_remote_port": 4},
    ])
    assert rows[0]["Switch"] == "Switch A"
    assert rows[0]["Port"] == "4"


def test_switch_ports_match_when_mac_table_count_missing():
    from unifi_sentinel.export import build_switch_ports
    switches = [{"mac": "aa:aa", "type": "usw", "name": "SW",
                 "port_table": [{"port_idx": 2, "up": True}]}]
    clients = [{"mac": "cc:cc", "name": "pc", "sw_mac": "aa:aa", "sw_port": 2}]
    rows = build_switch_ports(switches, clients)["SW"]
    assert rows[0]["Connected Name"] == "pc"
