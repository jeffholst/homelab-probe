"""Groups of findings that share a cause (issue #230): a finding that another finding explains, with the evidence.

This module is pure standard library and **never guesses**: a finding gets a ``group`` only where the data of the
controller supports the claim, and the claim says how far it goes. Nothing is hidden or merged: every finding stays in
the list, the group only points at the finding that probably explains it.

The one case so far is ``offline_behind_offline_uplink``: a device that is offline (its ``device.offline`` finding)
whose last reported uplink leads, through devices that are all offline too, to an offline device that has its own
``device.offline`` finding. The device at the far end of that run is the cause. It is **not** grouped when

- it has no known uplink, or the uplink names a device the controller does not list;
- its uplink parent is online (then the parent cannot be why it is gone);
- the offline devices above it have no finding (an ignore rule hid it: there is nothing to point at);
- the uplinks form a loop (the data contradict themselves, so no device in it is the root).

**An offline device keeps its last known uplink** in the controller's records, so the chain shows where the device
*was* connected. That is why the group is "probably caused by" and carries a limitation saying so: it is evidence of a
probable cause, not a confirmation (a power cut would look the same, and so would two unrelated failures).
"""

from typing import Any, Dict, List, Mapping, Optional, TypedDict

from .triage import finding_id
from .util import normalize_mac

KIND = "offline_behind_offline_uplink"
OFFLINE_CODE = "device.offline"
SOURCE = "the uplink each device last reported to the controller"
LIMITATIONS = [
    "An offline device keeps its last known uplink, so the chain shows where each device was connected, not where "
    "it is now.",
    "This is a probable cause, not a confirmed one: a device can be offline for its own reason, and a power cut "
    "or a cabling fault upstream would look the same.",
]


class Hop(TypedDict):
    """One device of the chain, from the grouped device up to the cause."""

    name: str
    mac: str
    offline: bool
    finding: Optional[str]                   # the id of this device's own offline finding
    uplink_port: Optional[int]               # the port of the next device in the chain it was plugged into


class Evidence(TypedDict):
    source: str
    chain: List[Hop]


class Group(TypedDict):
    kind: str
    cause: str                               # the id of the finding that probably explains this one
    cause_code: str
    summary: str
    evidence: Evidence
    limitations: List[str]


def _port(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def group_findings(findings: List[Dict[str, Any]], links: Mapping[str, Mapping[str, Any]]) -> Dict[str, Group]:
    """``{finding id: group}`` for the findings explained by another one. ``findings`` are the ``Finding.to_dict``
    forms, ``links`` the devices by MAC as ``topology.device_links`` gives them. A finding that is not in the result
    has no group. The same input always gives the same result."""
    offline: Dict[str, str] = {}             # MAC -> id of that device's offline finding
    for finding in findings:
        mac = normalize_mac(finding.get("mac"))
        if finding.get("code") == OFFLINE_CODE and mac:
            offline[mac] = finding_id(finding["code"], finding["subject"], mac)
    known = {normalize_mac(mac): link for mac, link in links.items()}

    def chain_of(start: str) -> Optional[List[str]]:
        """The devices above ``start`` up to the cause, or None when there is no evidence of a cause."""
        seen, above, cause_at, current = {start}, [], 0, start
        while True:
            parent = normalize_mac((known.get(current) or {}).get("parent"))
            if not parent or parent not in known:
                break
            if parent in seen:
                return None                  # a loop: no device in it is the root
            if known[parent].get("online") is not False:
                break                        # the parent is online (or its state is not known): not its fault
            seen.add(parent)
            above.append(parent)
            if parent in offline:
                cause_at = len(above)        # only a device with a finding of its own can be pointed at
            current = parent
        return above[:cause_at] if cause_at else None

    groups: Dict[str, Group] = {}
    for mac, ident in sorted(offline.items()):
        above = chain_of(mac)
        if not above:
            continue
        hops: List[Hop] = []
        devices = [mac] + above
        for number, device in enumerate(devices):
            link = known.get(device) or {}
            hops.append({"name": str(link.get("name") or device), "mac": device, "offline": True,
                         "finding": offline.get(device),
                         "uplink_port": _port(link.get("parent_port")) if number < len(above) else None})
        cause = devices[-1]
        groups[ident] = {
            "kind": KIND, "cause": offline[cause], "cause_code": OFFLINE_CODE,
            "summary": f"Probably caused by {hops[-1]['name']} being offline: {hops[0]['name']} was last reported "
                       f"connected {'through it' if len(above) > 1 else 'to it'}.",
            "evidence": {"source": SOURCE, "chain": hops}, "limitations": list(LIMITATIONS)}
    return groups
