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
