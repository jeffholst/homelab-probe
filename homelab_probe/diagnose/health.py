"""Checks on the controller's own health subsystems and the internet connection (``stat/health``)."""

import time
from typing import List

from ..events import describe_duration
from ..settings import DiagnoseSettings
from ..snapshot import Snapshot
from ..util import describe_age, number_or_zero
from ..wan import SPEEDTEST_BASELINE_DAYS, median_download, monitoring, nat_status, speedtests_for_baseline
from .model import CRITICAL, DEVICE_SUBSYSTEMS, INFO, WARNING, Finding


def _health_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """Findings from the controller's own subsystem health (``stat/health``).

    lan/wlan go "error"/"warning" merely because devices are disconnected, which the
    device checks already report, so that case is a single info line; an unexplained
    status keeps the controller's severity. Other subsystems map error to critical.
    """
    findings: List[Finding] = []
    severity_of = {"error": CRITICAL, "warning": WARNING}
    pending = 0

    for h in snap.health:
        name = h.get("subsystem") or "?"
        status = h.get("status")
        pending += int(number_or_zero(h.get("num_pending")))

        if status in severity_of:
            disconnected = int(number_or_zero(h.get("num_disconnected")))
            if name in DEVICE_SUBSYSTEMS and disconnected:
                findings.append(Finding(
                    INFO, name, f"{name} subsystem reports {status}: {disconnected} "
                                "device(s) disconnected (see the device findings)",
                    code="health.device_subsystem"))
            else:
                gateway = f" (gateway {h['gw_name']})" if h.get("gw_name") else ""
                findings.append(Finding(
                    severity_of[status], name, f"{name} subsystem is in {status} state{gateway}",
                    code="health.subsystem"))

        if name == "www":
            latency, drops = number_or_zero(h.get("latency")), number_or_zero(h.get("drops"))
            if latency and latency >= settings.wan_latency_warn_ms:
                findings.append(Finding(WARNING, name, f"internet latency {latency:.0f} ms", code="internet.latency"))
            if drops and drops >= settings.wan_drops_warn:
                findings.append(Finding(WARNING, name, f"internet reports {drops:.0f} drops", code="internet.drops"))
            speedtest = str(h.get("speedtest_status") or "")
            if any(word in speedtest.lower() for word in ("fail", "error")):
                findings.append(Finding(INFO, name, f"last speedtest: {speedtest}", code="internet.speedtest_failed"))

    if pending:
        findings.append(Finding(INFO, "controller", f"{pending} device(s) waiting to be adopted",
                                code="controller.pending_adoption"))
    return findings


def _wan_findings(snap: Snapshot, settings: DiagnoseSettings) -> List[Finding]:
    """The controller's own 24-hour internet monitoring, and the last speedtest.

    ``alerting_monitors`` is the list of monitors configured to raise alerts, not the ones
    currently alerting (on a healthy network every one of them reads 100%), so each
    monitor is judged by its own availability.
    """
    findings: List[Finding] = []
    nat = nat_status(snap)
    if nat["message"]:
        findings.append(Finding(
            WARNING, "wan", nat["message"],
            code=("wan.double_nat" if nat["kind"] == "private"
                  else "wan.cgnat" if nat["kind"] == "cgnat" else "wan.link_local_address")))
    all_wans = monitoring(snap)
    for m in all_wans:
        which = f" ({m['name']})" if len(all_wans) > 1 else ""
        window = describe_duration(m["period_s"]) if m["period_s"] else "24h"
        if m["availability"] is not None and m["availability"] < settings.wan_availability_warn_pct:
            findings.append(Finding(
                WARNING, "wan", f"internet availability{which} {m['availability']:.1f}% over the last {window}",
                code="wan.availability"))
        for t in m["targets"]:
            if t["availability"] is not None and t["availability"] < settings.wan_availability_warn_pct:
                latency = f", latency {t['latency_ms']:.0f} ms" if t["latency_ms"] is not None else ""
                findings.append(Finding(
                    WARNING, "wan",
                    f"monitor {t['target']} ({t['type']}) availability {t['availability']:.1f}% "
                    f"over the last {window}{latency}",
                    code="wan.monitor_availability"))

    now_ms = int(time.time() * 1000)
    recent = speedtests_for_baseline(snap.speedtests, SPEEDTEST_BASELINE_DAYS, now_ms)
    median = median_download(recent)
    last = recent[-1] if recent else {}
    download = last.get("download_mbps")
    if (median and isinstance(download, (int, float)) and not isinstance(download, bool)
            and download < median * settings.wan_speed_drop_pct / 100):
        age = describe_age(max(0, int((now_ms - last["time"]) / 1000)))
        findings.append(Finding(
            WARNING, "wan",
            f"last speedtest download {download:.0f} Mbps ({age} ago) is {download / median * 100:.0f}% "
            f"of the {SPEEDTEST_BASELINE_DAYS}-day median ({median:.0f} Mbps)",
            code="wan.speedtest_slow"))
    return findings
