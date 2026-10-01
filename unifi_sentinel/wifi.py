"""Wireless report: `unifi-sentinel wifi`.

Shows each access point's radios and a channel plan built from the neighboring networks
the APs hear, with plain observations. It only describes; it never suggests changing a
setting. Neighboring networks are other people's: their names are shown as the
controller reports them, so be careful where you paste the output.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .client_view import DeviceIndex
from .query import format_table
from .snapshot import Snapshot
from .util import clean_data, number, plural, printable

BANDS = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}
BAND_ALIASES = {"2.4": "ng", "2": "ng", "24": "ng", "5": "na", "6": "6e"}
DEFAULT_MIN_SIGNAL = -80          # dBm: weaker neighbors are counted but not named or compared
NAMES_PER_CHANNEL = 5             # strongest neighbors named per channel unless --all
USUAL_2G_CHANNELS = (1, 6, 11)
Radio = Dict[str, Any]
Neighbor = Dict[str, Any]

# 5 GHz channel blocks for a given width (first and last 20 MHz channel in each block)
_BLOCKS_5G = {
    160: [(36, 64), (100, 128)],
    80: [(36, 48), (52, 64), (100, 112), (116, 128), (132, 144), (149, 161)],
    40: [(36, 40), (44, 48), (52, 56), (60, 64), (100, 104), (108, 112), (116, 120),
         (124, 128), (132, 136), (140, 144), (149, 153), (157, 161)],
}


def parse_band(text: str) -> str:
    """'2.4', '5' or '6' (optionally with 'GHz') to the controller's band code."""
    key = re.sub(r"\s*ghz\s*$", "", str(text).strip().lower())
    if key in BAND_ALIASES:
        return BAND_ALIASES[key]
    if key in BANDS:
        return key
    raise ValueError(f"invalid band {text!r}: use 2.4, 5 or 6")


# -- spectrum --------------------------------------------------------------------

def span_mhz(
    band: str, channel: Any, width: Any, center_freq: Any = None,
    center_channel: Any = None, extension_channel: Any = None,
) -> Optional[Tuple[float, float]]:
    """The frequency range (MHz) a transmitter on ``channel`` with ``width`` occupies, or None
    when the channel is unknown. 2.4 GHz channels are 5 MHz apart but about 22 MHz wide,
    which is why a neighbor on channel 4 disturbs channels 1 and 6."""
    ch, bw = number(channel), int(number(width) or 20)
    if ch is None:
        return None
    ch = int(ch)
    if band == "ng":
        centre = number(center_freq)
        if centre is not None and centre <= 0:
            centre = None
        if centre is None and bw > 20:
            centre_ch = number(center_channel)
            if centre_ch is not None and centre_ch > 0:
                centre = 2407 + 5 * centre_ch
            else:
                secondary = number(extension_channel)
                if secondary is not None and secondary > 0:
                    centre = ((2407 + 5 * ch) + (2407 + 5 * secondary)) / 2
                elif isinstance(extension_channel, str):
                    extension = extension_channel.strip().lower()
                    offset = 4 if "+" in extension or "above" in extension else (
                        -4 if "-" in extension or "below" in extension else 0
                    )
                    if offset:
                        centre = ((2407 + 5 * ch) + (2407 + 5 * (ch + offset))) / 2
            if centre is None:
                return None
        if centre is None:
            centre = 2407 + 5 * ch
        half = 11 if bw <= 20 else bw / 2
        return centre - half, centre + half
    centre = number(center_freq)
    if centre is not None and centre > 0:
        half = 11 if band == "ng" and bw <= 20 else max(bw, 20) / 2
        return centre - half, centre + half
    if band == "na":
        for lo, hi in _BLOCKS_5G.get(bw, []):
            if lo <= ch <= hi:
                return 5000 + 5 * lo - 10, 5000 + 5 * hi + 10
        centre, half = 5000 + 5 * ch, max(bw, 20) / 2
        return centre - half, centre + half
    if band == "6e":
        n = max(1, bw // 20)
        if n > 1:
            size = 4 * n                                     # 6 GHz blocks start at channel 1, 1+size...
            lo = (ch - 1) // size * size + 1
            return 5950 + 5 * lo - 10, 5950 + 5 * (lo + size - 4) + 10
        return 5950 + 5 * ch - 10, 5950 + 5 * ch + 10
    return None


def overlaps(a: Optional[Tuple[float, float]], b: Optional[Tuple[float, float]]) -> bool:
    return bool(a and b and a[1] > b[0] and b[1] > a[0])


# -- gathering ---------------------------------------------------------------------

def own_bssids(snap: Snapshot) -> set:
    """BSSIDs of our own networks, which must never be counted as neighbors."""
    found = {(d.get("mac") or "").lower() for d in snap.legacy_devices}
    for d in snap.legacy_devices:
        for vap in d.get("vap_table") or []:
            if isinstance(vap, dict) and vap.get("bssid"):
                found.add(str(vap["bssid"]).lower())
    found.discard("")
    return found


def unique_neighbors(snap: Snapshot, ap_macs: Optional[set] = None) -> List[Neighbor]:
    """One entry per neighboring network (BSSID), from rows that are per (BSSID, observing AP).

    Counting rows would count a network once for every AP that hears it. The strongest
    reading is kept and ``seen_by`` lists the APs that heard it.
    """
    ours = own_bssids(snap)
    best: Dict[str, Neighbor] = {}
    for row in snap.neighbors:
        bssid = str(row.get("bssid") or "").lower()
        signal = number(row.get("signal"))
        ap_mac = str(row.get("ap_mac") or "").lower()
        if (not bssid or bssid in ours or signal is None
                or (ap_macs is not None and ap_mac not in ap_macs)):
            continue
        seen = best.setdefault(bssid, {"seen_by": set(), "signal": signal, "row": row})
        seen["seen_by"].add(ap_mac)
        if signal > seen["signal"]:
            seen["signal"], seen["row"] = signal, row
    result = []
    for bssid, info in best.items():
        row, band = info["row"], info["row"].get("band") or info["row"].get("radio") or ""
        width = number(row.get("bw")) or 20
        span = span_mhz(
            band, row.get("channel"), width, row.get("center_freq"), row.get("center_channel"),
            row.get("extension_channel", row.get("secondary_channel", row.get("ext_channel"))),
        )
        result.append({
            "bssid": bssid, "name": printable(row.get("essid"), 40), "band": band,
            "channel": int(number(row.get("channel")) or 0) or None, "width": int(width),
            "signal": info["signal"], "security": str(row.get("security") or ""),
            "open": str(row.get("security") or "").strip().lower() == "open",
            "vendor": str(row.get("oui") or ""), "seen_by": sorted(info["seen_by"]), "span": span})
    return result


def radios(snap: Snapshot, idx: DeviceIndex) -> List[Radio]:
    """Every radio of every access point. An AP with no radio statistics (offline, or one the
    controller has not reported on) gets a single placeholder entry so it is not hidden."""
    rows: List[Radio] = []
    for d in sorted((d for d in snap.legacy_devices if d.get("type") == "uap"),
                    key=lambda d: idx.name((d.get("mac") or "").upper()).lower()):
        mac = (d.get("mac") or "").upper()
        stats = [r for r in d.get("radio_table_stats") or [] if isinstance(r, dict)]
        if not stats:
            rows.append({"ap": idx.name(mac), "mac": mac, "band": "", "channel": None, "online": not idx.offline(mac),
                         "width": None, "tx_power": None, "clients": None, "utilization": None,
                         "retries": None, "satisfaction": None, "span": None})
            continue
        for r in sorted(stats, key=lambda r: list(BANDS).index(r.get("radio")) if r.get("radio") in BANDS else 9):
            sat = number(r.get("satisfaction"))
            channel = number(r.get("channel"))
            rows.append({
                "ap": idx.name(mac), "mac": mac, "band": r.get("radio") or "",
                "channel": None if channel is None else int(channel), "online": not idx.offline(mac),
                "width": None if number(r.get("bw")) is None else int(number(r.get("bw"))),
                "tx_power": number(r.get("tx_power")),
                "clients": None if number(r.get("num_sta")) is None else int(number(r.get("num_sta"))),
                "utilization": number(r.get("cu_total")), "retries": number(r.get("tx_retries_pct")),
                "satisfaction": sat if sat is not None and sat >= 0 else None,      # -1 means unknown
                "span": span_mhz(
                    r.get("radio") or "", channel, r.get("bw"), r.get("center_freq"),
                    r.get("center_channel"),
                    r.get("extension_channel", r.get("secondary_channel", r.get("ext_channel"))),
                )})
    return rows


# -- the report ---------------------------------------------------------------------


def build_wifi(snap: Snapshot, min_signal: float = DEFAULT_MIN_SIGNAL, band: str = "", ap: str = "") -> Dict[str, Any]:
    idx = DeviceIndex(snap)
    all_radios = radios(snap, idx)
    needle = ap.strip().lower()
    mine = [r for r in all_radios
            if (not needle or needle in r["ap"].lower()) and (not band or r["band"] in (band, ""))]
    wanted_aps = {r["mac"].lower() for r in mine} if needle else None

    neighbors_available = snap.neighbors_available
    neigh = [n for n in unique_neighbors(snap, wanted_aps)
             if not band or n["band"] == band] if neighbors_available else []
    strong = [n for n in neigh if n["signal"] >= min_signal]

    plan: List[Dict[str, Any]] = []
    for code in BANDS:
        in_band = [n for n in neigh if n["band"] == code]
        radios_here = [r for r in mine if r["band"] == code and r["channel"] is not None]
        if not (in_band or radios_here) or (band and code != band):
            continue
        channels = []
        for ch in sorted({n["channel"] for n in in_band if n["channel"]} | {r["channel"] for r in radios_here}):
            here = [n for n in in_band if n["channel"] == ch]
            here_strong = sorted((n for n in here if n["signal"] >= min_signal), key=lambda n: -n["signal"])
            channels.append({
                "channel": ch, "neighbors": len(here) if neighbors_available else None,
                "strong": len(here_strong) if neighbors_available else None,
                "our_radios": [r["ap"] for r in radios_here if r["channel"] == ch],
                "strong_networks": [{"name": n["name"], "hidden": not n["name"], "bssid": n["bssid"],
                                     "signal": n["signal"], "security": n["security"], "open": n["open"],
                                     "vendor": n["vendor"], "seen_by_aps": len(n["seen_by"])} for n in here_strong]})
        plan.append({"band": code, "label": BANDS[code], "channels": channels,
                     "scanned": code != "6e"})      # the controller's neighbor scan reports no 6 GHz networks

    return {
        "ap_matched": not needle or any(r["ap"].lower().find(needle) >= 0 for r in all_radios),
        "min_signal": min_signal,
        "radios": [{k: v for k, v in r.items() if k != "span"} for r in mine],
        "neighbors": {
            "available": neighbors_available,
            "total": len(neigh) if neighbors_available else None,
            "strong": len(strong) if neighbors_available else None,
            "open": sum(n["open"] for n in neigh) if neighbors_available else None,
            "hidden": sum(not n["name"] for n in neigh) if neighbors_available else None,
        },
        "plan": plan,
        "observations": _observations(mine, strong, min_signal, neighbors_available),
    }


def _observations(
    mine: List[Radio], strong: List[Neighbor], min_signal: float, neighbors_available: bool = True
) -> List[str]:
    out: List[str] = []
    limit = f"stronger than {min_signal:g} dBm"
    live = [r for r in mine if r["band"] and r["channel"] is not None]

    quiet_radios: List[str] = []
    if neighbors_available:
        for r in live:
            label = f"{r['ap']} {BANDS[r['band']]} (channel {r['channel']})"
            if r["band"] == "6e":                      # the controller's neighbor scan has no 6 GHz data
                continue
            same = [n for n in strong if n["band"] == r["band"] and n["channel"] == r["channel"]]
            near = [n for n in strong if n["band"] == r["band"] and n["channel"] != r["channel"]
                    and overlaps(r["span"], n["span"])]
            if same or near:
                out.append(f"{label}: {plural(len(same), 'neighbor')} {limit} on the same channel, "
                           f"{len(near)} overlapping it")
            else:
                quiet_radios.append(label)
        if quiet_radios:
            out.append(f"No neighbors {limit} on or overlapping: " + "; ".join(quiet_radios))

    for i, a in enumerate(live):                       # our own radios competing with each other
        for b in live[i + 1:]:
            if a["band"] == b["band"] and a["mac"] != b["mac"] and overlaps(a["span"], b["span"]):
                kind = (f"both use {BANDS[a['band']]} channel {a['channel']}" if a["channel"] == b["channel"]
                        else f"use overlapping {BANDS[a['band']]} channels {a['channel']} and {b['channel']}")
                out.append(f"{a['ap']} and {b['ap']} {kind}, so they compete with each other")

    if neighbors_available and (
        any(r["band"] == "ng" for r in live) or any(n["band"] == "ng" for n in strong)
    ):
        crowd = {ch: [n for n in strong if n["band"] == "ng" and overlaps(span_mhz("ng", ch, 20), n["span"])]
                 for ch in USUAL_2G_CHANNELS}
        fewest = min(len(v) for v in crowd.values())
        quiet = [ch for ch, v in crowd.items() if len(v) == fewest]
        counts = ", ".join(f"channel {ch}: {len(v)}" for ch, v in crowd.items())
        if len(quiet) == len(crowd):
            out.append(f"On 2.4 GHz channels 1, 6 and 11 are equally busy ({counts}), {limit}")
        else:
            names = " and ".join(f"channel {c}" for c in quiet)
            out.append(f"Of the usual 2.4 GHz channels, {names} {'overlaps' if len(quiet) == 1 else 'overlap'} "
                       f"the fewest neighbors ({counts}), {limit}")
    return out


# -- rendering ---------------------------------------------------------------------

def _opt(value: Optional[float], fmt: str) -> str:
    return "" if value is None else fmt.format(value)


def render_text(wifi: Dict[str, Any], show_all: bool = False, ap: str = "") -> str:
    wifi, ap = clean_data(wifi), printable(ap)
    if not wifi.get("ap_matched", True):
        return f"No access point matches '{ap}'."
    lines = ["Access points"]
    rows, offline = [], []
    for r in wifi["radios"]:
        if not r["band"]:
            offline.append(r["ap"] + (" (offline)" if not r["online"] else ""))
            continue
        rows.append({"AP": r["ap"], "Band": BANDS.get(r["band"], r["band"]),
                     "Channel": "" if r["channel"] is None else r["channel"],
                     "Width": _opt(r["width"], "{:.0f} MHz"), "Power": _opt(r["tx_power"], "{:.0f} dBm"),
                     "Clients": "" if r["clients"] is None else r["clients"],
                     "Utilization": _opt(r["utilization"], "{:.0f}%"), "Retries": _opt(r["retries"], "{:.0f}%"),
                     "Satisfaction": _opt(r["satisfaction"], "{:.0f}%")})
    lines.append(format_table(rows, ["AP", "Band", "Channel", "Width", "Power", "Clients", "Utilization",
                                     "Retries", "Satisfaction"]) if rows else "  no radio data")
    for name in offline:
        lines.append(f"  {name}: no radio data")

    n = wifi["neighbors"]
    if n["available"]:
        lines += ["", f"Neighboring networks: {n['total']} seen by your APs ({n['strong']} stronger than "
                      f"{wifi['min_signal']:g} dBm, {n['open']} open, {n['hidden']} with a hidden name)"]
    else:
        lines += ["", "Neighboring networks: unavailable (scan failed)"]
    for band in wifi["plan"]:
        lines += ["", band["label"]]
        if n["available"] and not band["scanned"]:
            lines.append("  (the controller's neighbor scan does not report 6 GHz networks)")
        na = "n/a" if not n["available"] or not band["scanned"] else None
        table = [{"Channel": c["channel"], "Neighbors": na or c["neighbors"], "Strong": na or c["strong"],
                  "Your radios": ", ".join(c["our_radios"])} for c in band["channels"]]
        lines.append(format_table(table, ["Channel", "Neighbors", "Strong", "Your radios"]))
        for c in band["channels"]:
            nets = c["strong_networks"]
            if not nets:
                continue
            shown = nets if show_all else nets[:NAMES_PER_CHANNEL]
            lines += ["", f"  Channel {c['channel']}: strongest of {len(nets)}"
                          f" stronger than {wifi['min_signal']:g} dBm"]
            for net in shown:
                name = net["name"] or f"(hidden{', ' + net['vendor'] if net['vendor'] else ''})"
                extra = [f"{net['signal']:.0f} dBm", net["security"] or "unknown security"]
                if net["seen_by_aps"] > 1:
                    extra.append(f"heard by {net['seen_by_aps']} APs")
                lines.append(f"    {name}  ({', '.join(extra)})" + ("  [OPEN]" if net["open"] else ""))
            if len(shown) < len(nets):
                lines.append(f"    ... and {len(nets) - len(shown)} more (use --all)")
    if wifi["observations"]:
        lines += ["", "Observations"] + [f"  - {o}" for o in wifi["observations"]]
    return "\n".join(lines)


def to_json(wifi: Dict[str, Any]) -> str:
    return json.dumps(wifi, indent=2)
