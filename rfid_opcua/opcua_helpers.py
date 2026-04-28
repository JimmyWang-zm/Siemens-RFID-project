"""
rfid_opcua.opcua_helpers
────────────────────────
OPC UA node discovery, event subscription setup, scan start/stop,
and tree browsing.
"""

from __future__ import annotations

import logging

from asyncua import Client, ua

from .config import DI_CHANNEL
from .handlers import ScanEventHandler
from .session import Session

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Node discovery
# ═══════════════════════════════════════════════════════════════════════════════

async def find_nodes(client: Client, rp_index: int):
    """
    Discover the read-point node and all relevant child nodes.

    Returns (rp, scan_active_node, scan_start_node,
             scan_stop_node, di_node, scan_nodes).
    """
    root    = client.get_root_node()
    objects = await root.get_child(["0:Objects"])

    device_set = None
    for ns in range(2, 8):
        try:
            device_set = await objects.get_child([f"{ns}:DeviceSet"])
            break
        except Exception:
            continue
    if device_set is None:
        raise RuntimeError("DeviceSet node not found. Check that OPC UA is enabled in WBM.")

    children = await device_set.get_children()
    readpoints = [
        c for c in children
        if "read_point" in (await c.read_browse_name()).Name.lower()
        or "readpoint"  in (await c.read_browse_name()).Name.lower()
    ]
    if not readpoints:
        raise RuntimeError("No read points found under DeviceSet")
    if rp_index > len(readpoints):
        raise RuntimeError(f"Read point {rp_index} not found (only {len(readpoints)} available)")

    rp = readpoints[rp_index - 1]
    log.debug("Read point : %s", (await rp.read_browse_name()).Name)

    scan_active_node = None
    scan_start_node  = None
    scan_stop_node   = None
    scan_nodes       = {"data": None, "antenna": None, "rssi": None, "timestamp": None}

    for c in await rp.get_children():
        bn = (await c.read_browse_name()).Name
        if bn == "ScanActive":
            scan_active_node = c
            log.debug("  ScanActive \u2713")
        elif bn == "ScanStart":
            scan_start_node = c
            log.debug("  ScanStart  \u2713")
        elif bn == "ScanStop":
            scan_stop_node = c
            log.debug("  ScanStop   \u2713")
        elif bn == "LastScanData":
            scan_nodes["data"] = c
        elif bn == "LastScanAntenna":
            scan_nodes["antenna"] = c
        elif bn == "LastScanRSSI":
            scan_nodes["rssi"] = c
        elif bn == "LastScanTimestamp":
            scan_nodes["timestamp"] = c

    if all(scan_nodes[k] is not None for k in ("data", "antenna", "rssi", "timestamp")):
        log.debug("  LastScan*  \u2713  (Data / Antenna / RSSI / Timestamp)")
    else:
        log.warning("LastScan* nodes incomplete: %s", [k for k,v in scan_nodes.items() if v is None])

    di_node = await _find_di_node(client, rp, DI_CHANNEL)

    return rp, scan_active_node, scan_start_node, scan_stop_node, di_node, scan_nodes


# ═══════════════════════════════════════════════════════════════════════════════
#  DI node discovery
# ═══════════════════════════════════════════════════════════════════════════════

async def _find_di_node(client: Client, rp, di_channel: int):
    """Locate the DigitalInputs variable node on the RF695R."""
    # Direct path: rp > IOData > DigitalIOPorts > DigitalInputs
    try:
        for c in await rp.get_children():
            if (await c.read_browse_name()).Name == "IOData":
                for c2 in await c.get_children():
                    if (await c2.read_browse_name()).Name == "DigitalIOPorts":
                        for c3 in await c2.get_children():
                            if (await c3.read_browse_name()).Name == "DigitalInputs":
                                log.debug("  DI inputs  \u2713  [IOData > DigitalIOPorts > DigitalInputs  (bit %d)]", di_channel)
                                return c3
    except Exception as e:
        log.warning("Direct IOData path failed: %s", e)

    # DFS fallback
    root    = client.get_root_node()
    objects = await root.get_child(["0:Objects"])

    device_set = None
    for ns in range(2, 8):
        try:
            device_set = await objects.get_child([f"{ns}:DeviceSet"])
            break
        except Exception:
            continue
    search_root = device_set or objects

    ch = str(di_channel)
    value_names = {
        f"input_{ch}", f"input{ch}", f"di_{ch}", f"di{ch}",
        f"digitalinput_{ch}", f"digitalinput{ch}",
        f"in{ch}", f"channel_{ch}", ch,
    }
    group_names = {
        "digitalio", "digitalinputs", "di", "inputs", "io",
        "iolink", "iolinkmaster",
        "processdata", "processinputdata", "inputdata",
    }

    async def dfs(node, in_group: bool, depth: int):
        if depth > 6:
            return None
        try:
            children = await node.get_children()
        except Exception:
            return None
        for child in children:
            try:
                raw = (await child.read_browse_name()).Name
                bn  = raw.lower().replace(" ", "").replace("-", "")
            except Exception:
                continue
            if in_group and bn in value_names:
                return child
            if bn in group_names:
                result = await dfs(child, in_group=True, depth=depth + 1)
                if result:
                    return result
            else:
                result = await dfs(child, in_group=False, depth=depth + 1)
                if result:
                    return result
        return None

    found = await dfs(search_root, in_group=False, depth=0)
    if found:
        name = (await found.read_browse_name()).Name
        log.debug("  DI%d node  \u2713  [%s]  (DFS fallback)", di_channel, name)
    else:
        log.warning("DI%d node not found.", di_channel)
        log.warning("       Set DEBUG_BROWSE=True and restart to inspect the OPC UA tree.")
    return found


# ═══════════════════════════════════════════════════════════════════════════════
#  Event-type discovery
# ═══════════════════════════════════════════════════════════════════════════════

async def find_event_type(client: Client, *type_names: str):
    """
    Browse the OPC UA event type hierarchy for one of *type_names*.
    Returns the best matching event type Node, or None.
    Earlier names in the list are preferred.
    """
    base = client.get_node(ua.ObjectIds.BaseEventType)
    candidates: dict = {}

    async def _dfs(node, depth=0):
        if depth > 6:
            return
        # Stop early if we found the first (preferred) name
        if type_names and type_names[0] in candidates:
            return
        try:
            children = await node.get_children()
        except Exception:
            return
        for child in children:
            try:
                bn = (await child.read_browse_name()).Name
            except Exception:
                continue
            if bn in type_names:
                candidates[bn] = child
                if bn == type_names[0]:
                    return
            await _dfs(child, depth + 1)

    try:
        await _dfs(base)
    except Exception as e:
        log.warning("Event type discovery failed: %s", e)

    for name in type_names:
        if name in candidates:
            return candidates[name]
    return None


async def setup_scan_event_subscription(
    client: Client, rp, session: Session, publish_interval: int = 100,
    scan_nodes: dict | None = None,
):
    """
    Create an OPC UA event subscription for RfidScanEventType on the read point.
    Returns (subscription, handler).  Raises RuntimeError on failure.
    """
    evt_type = await find_event_type(client, "RfidScanEventType", "AutoIdScanEventType")
    if evt_type is None:
        raise RuntimeError(
            "RfidScanEventType not found in server type hierarchy — "
            "ensure the reader firmware supports OPC UA events"
        )

    type_name = (await evt_type.read_browse_name()).Name
    log.debug("Event type: %s  (%s)", type_name, evt_type.nodeid)

    handler = ScanEventHandler(session, scan_nodes=scan_nodes)
    sub = None
    try:
        sub = await client.create_subscription(publish_interval, handler)
        await sub.subscribe_events(rp, evt_type)
        log.debug("Subscribed to %s events  (publish: %d ms)", type_name, publish_interval)
        return sub, handler
    except Exception as e:
        if sub is not None:
            try:
                await sub.delete()
            except Exception:
                pass
        raise RuntimeError(f"Event subscription failed: {e}") from e


# ═══════════════════════════════════════════════════════════════════════════════
#  Scan start / stop
# ═══════════════════════════════════════════════════════════════════════════════

async def start_scanning(rp, scan_start_node, scan_active_node):
    """Start continuous scanning on the read point (ScanStart preferred)."""
    if scan_start_node is not None:
        try:
            scan_settings_cls = getattr(ua, "ScanSettings", None)
            if scan_settings_cls is not None:
                ss = scan_settings_cls()
                ss.Cycles        = 0
                ss.DataAvailable = False
                ss.Duration      = 0
                await rp.call_method(scan_start_node, ss)
            else:
                await rp.call_method(
                    scan_start_node,
                    ua.Variant(0,     ua.VariantType.UInt32),
                    ua.Variant(False, ua.VariantType.Boolean),
                    ua.Variant(0,     ua.VariantType.UInt32),
                )
            log.debug("ScanStart (continuous)")
            return
        except Exception as e:
            log.warning("ScanStart method failed: %s \u2014 trying ScanActive", e)

    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(True, ua.VariantType.Boolean))
            )
            log.debug("ScanActive = True")
            return
        except Exception as e:
            log.error("ScanActive write also failed: %s", e)


async def stop_scanning(rp, scan_stop_node, scan_active_node):
    """Stop scanning on the read point (ScanStop preferred)."""
    if scan_stop_node is not None:
        try:
            await rp.call_method(scan_stop_node)
            log.debug("ScanStop")
            return
        except Exception as e:
            log.warning("ScanStop method failed: %s \u2014 trying ScanActive", e)

    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(False, ua.VariantType.Boolean))
            )
            log.debug("ScanActive = False")
            return
        except Exception as e:
            log.error("ScanActive write also failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
#  Debug tree browse
# ═══════════════════════════════════════════════════════════════════════════════

async def browse_tree(client: Client, max_depth: int = 5):
    """Print the full OPC UA node tree to identify node paths."""
    root    = client.get_root_node()
    objects = await root.get_child(["0:Objects"])

    async def _print(node, depth: int):
        if depth > max_depth:
            return
        try:
            children = await node.get_children()
        except Exception:
            return
        for child in children:
            try:
                bn  = await child.read_browse_name()
                nid = child.nodeid
                log.info("%s[%d:%s]  %s", "  " * depth, bn.NamespaceIndex, bn.Name, nid)
                await _print(child, depth + 1)
            except Exception:
                continue

    log.info("")
    log.info("=" * 60)
    log.info("  OPC UA node tree  (DEBUG_BROWSE = True)")
    log.info("  Search for 'Input', 'DI', 'IOLink', 'ProcessData' nodes")
    log.info("=" * 60)
    await _print(objects, 0)
    log.info("=" * 60)
