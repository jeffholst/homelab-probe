"""Checks on Wi-Fi quality: weak clients and busy or retrying radios."""

from typing import List

from ..settings import DiagnoseSettings
from ..snapshot import Snapshot
from ..util import known_percent, normalize_mac, number_or_zero
from .model import CRITICAL, WARNING, Finding
from .ports import switch_name

BANDS = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}


def _wifi_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """Weak signal, retries and low satisfaction for Wi-Fi clients, and busy or
    retry-heavy AP radios.

    Many connected clients report no signal or satisfaction at all, and some APs report
    radio satisfaction as -1 (unknown), so missing values are never flagged. The
    ``anomalies`` field is deliberately not used: it is present on nearly every client.
    """
    findings: List[Finding] = []
    ap_name = {
        normalize_mac(d.get("macAddress")): d.get("name") or d.get("macAddress") or "?"
        for d in snap.devices
    }
    ap_name.update({normalize_mac(d.get("mac")): switch_name(d) for d in snap.legacy_devices})

    for c in snap.legacy_clients:
        if c.get("is_wired"):
            continue
        name = c.get("name") or c.get("hostname") or c.get("mac") or "?"
        ap = ap_name.get(normalize_mac(c.get("ap_mac")))
        place = ", ".join(x for x in (BANDS.get(str(c.get("radio") or ""), ""), f"on {ap}" if ap else "") if x)
        where = f" ({place})" if place else ""

        signal = number_or_zero(c.get("signal"))
        if signal < 0 and signal <= settings.wifi_weak_signal_dbm:
            findings.append(Finding(WARNING, name, f"weak Wi-Fi signal {signal:.0f} dBm{where}",
                                    code="wifi.weak_signal"))

        attempts = number_or_zero(c.get("wifi_tx_attempts"))
        retries = known_percent(c.get("wifi_tx_retries_percentage"))
        if (attempts >= settings.wifi_min_attempts and retries is not None
                and retries >= settings.wifi_retry_pct):
            findings.append(Finding(
                WARNING, name, f"{retries:.0f}% of Wi-Fi transmissions retried{where}", code="wifi.client_retries"))

        satisfaction = known_percent(c.get("satisfaction"))
        if satisfaction is not None and satisfaction < settings.wifi_satisfaction_warn:
            findings.append(Finding(WARNING, name, f"Wi-Fi satisfaction {satisfaction:.0f}%{where}",
                                    code="wifi.client_satisfaction"))

    for ap in snap.legacy_devices:
        if ap.get("type") != "uap":
            continue
        for radio in ap.get("radio_table_stats") or []:
            band = BANDS.get(radio.get("radio"), str(radio.get("radio") or "?"))
            label = f"{switch_name(ap)} {band} radio"
            channel = radio.get("channel")
            on = f" (channel {channel})" if channel not in (None, "") else ""

            util = known_percent(radio.get("cu_total"))
            if util is not None and util >= settings.radio_util_warn_pct:
                level = CRITICAL if util >= settings.radio_util_critical_pct else WARNING
                findings.append(Finding(
                    level, label, f"channel utilization {util:.0f}%{on}",
                    normalize_mac(ap.get("mac")), code="wifi.radio_utilization"))
            retries = number_or_zero(radio.get("tx_retries_pct"))
            if retries >= settings.wifi_retry_pct:
                findings.append(Finding(
                    WARNING, label, f"{retries:.0f}% of transmissions retried{on}",
                    normalize_mac(ap.get("mac")), code="wifi.radio_retries"))
            satisfaction = known_percent(radio.get("satisfaction"))
            if satisfaction is not None and satisfaction < settings.wifi_satisfaction_warn:
                findings.append(Finding(
                    WARNING, label, f"satisfaction {satisfaction:.0f}%{on}",
                    normalize_mac(ap.get("mac")), code="wifi.radio_satisfaction"))
    return findings
