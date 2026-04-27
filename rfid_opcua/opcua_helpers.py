"""
rfid_opcua.opcua_helpers
────────────────────────
OPC UA node discovery, event subscription setup, scan start/stop,
tree browsing, and LastScan* polling.
"""

from __future__ import annotations

from asyncua import Client, ua

from .config import (
    DI_CHANNEL,
    EVENT_PUBLISH_INTERVAL,
)
from .handlers import ScanEventHandler, epc_to_hex
from .session import Session


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
    print(f"[INFO] Read point : {(await rp.read_browse_name()).Name}")

    presence_node    = None
    scan_active_node = None
    scan_start_node  = None
    scan_stop_node   = None
    scan_nodes       = {"data": None, "antenna": None, "rssi": None, "timestamp": None}

    for c in await rp.get_children():
        bn = (await c.read_browse_name()).Name
        if bn == "ScanActive":
            scan_active_node = c
            print("       ScanActive ✓")
        elif bn == "ScanStart":
            scan_start_node = c
            print("       ScanStart  ✓")
        elif bn == "ScanStop":
            scan_stop_node = c
            print("       ScanStop   ✓")
        elif bn == "LastScanData":
            scan_nodes["data"] = c
        elif bn == "LastScanAntenna":
            scan_nodes["antenna"] = c
        elif bn == "LastScanRSSI":
            scan_nodes["rssi"] = c
        elif bn == "LastScanTimestamp":
            scan_nodes["timestamp"] = c

    if all(scan_nodes[k] is not None for k in ("data", "antenna", "rssi", "timestamp")):
        print("       LastScan*  ✓  (Data / Antenna / RSSI / Timestamp)")
    else:
        print(f"[WARN] LastScan* nodes incomplete: {[k for k,v in scan_nodes.items() if v is None]}")

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
                                print(f"       DI inputs  ✓  [IOData > DigitalIOPorts > DigitalInputs  (bit {di_channel})]")
                                return c3
    except Exception as e:
        print(f"[WARN] Direct IOData path failed: {e}")

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
        print(f"       DI{di_channel} node  ✓  [{name}]  (DFS fallback)")
    else:
        print(f"[WARN] DI{di_channel} node not found.")
        print("       Set DEBUG_BROWSE=True and restart to inspect the OPC UA tree.")
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
        print(f"[WARN] Event type discovery failed: {e}")

    for name in type_names:
        if name in candidates:
            return candidates[name]
    return None


async def setup_scan_event_subscription(
    client: Client, rp, session: Session, publish_interval: int = 100
):
    """
    Create an OPC UA event subscription for RfidScanEventType on the read point.
    Returns (subscription, handler) on success, (None, None) on failure.
    """
    evt_type = await find_event_type(client, "RfidScanEventType", "AutoIdScanEventType")
    if evt_type is None:
        print("[WARN] RfidScanEventType not found in server type hierarchy")
        print("       → Falling back to LastScan* variable polling (single-tag mode)")
        return None, None

    type_name = (await evt_type.read_browse_name()).Name
    print(f"[INFO] Event type   : {type_name}  ({evt_type.nodeid})")

    handler = ScanEventHandler(session)
    sub = None
    try:
        sub = await client.create_subscription(publish_interval, handler)
        await sub.subscribe_events(rp, evt_type)
        print(f"[SUB]  Subscribed to {type_name} events  (publish: {publish_interval} ms)")
        return sub, handler
    except Exception as e:
        print(f"[WARN] Event subscription failed: {e}")
        print("       → Falling back to LastScan* variable polling (single-tag mode)")
        if sub is not None:
            try:
                await sub.delete()
            except Exception:
                pass
        return None, None


# ═══════════════════════════════════════════════════════════════════════════════
#  Scan start / stop
# ═══════════════════════════════════════════════════════════════════════════════

async def start_scanning(rp, scan_start_node, scan_active_node, use_events: bool = False):
    """Start continuous scanning on the read point."""
    if use_events and scan_start_node is not None:
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
            print("[CMD] ScanStart (event mode — continuous)")
            return
        except Exception as e:
            print(f"[WARN] ScanStart method failed: {e} — trying ScanActive")

    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(True, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = True")
            return
        except Exception as e:
            print(f"[WARN] ScanActive write failed: {e} — trying ScanStart")

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
            print("[CMD] ScanStart")
        except Exception as e:
            print(f"[ERR] ScanStart failed: {e}")


async def stop_scanning(rp, scan_stop_node, scan_active_node, use_events: bool = False):
    """Stop scanning."""
    if use_events and scan_stop_node is not None:
        try:
            await rp.call_method(scan_stop_node)
            print("[CMD] ScanStop (event mode)")
            return
        except Exception as e:
            print(f"[WARN] ScanStop method failed: {e} — trying ScanActive")

    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(False, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = False")
            return
        except Exception as e:
            print(f"[WARN] ScanActive write failed: {e} — trying ScanStop")

    if scan_stop_node is not None:
        try:
            await rp.call_method(scan_stop_node)
            print("[CMD] ScanStop")
        except Exception as e:
            print(f"[ERR] ScanStop failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  LastScan* polling (fallback)
# ═══════════════════════════════════════════════════════════════════════════════

async def poll_last_scan(session: Session, scan_nodes: dict, prev_ts):
    """
    Poll LastScan* variables for new tag reads.
    Only suitable for single-tag mode (manual §3.1.3).
    Returns updated timestamp.
    """
    ts_node = scan_nodes.get("timestamp")
    if ts_node is None:
        return prev_ts
    try:
        ts = await ts_node.read_value()
    except Exception:
        return prev_ts

    if ts is None or ts == prev_ts:
        return prev_ts

    epc_raw, ant, rssi_raw = None, "?", None
    try:
        if scan_nodes.get("data"):
            epc_raw = await scan_nodes["data"].read_value()
        if scan_nodes.get("antenna"):
            ant = await scan_nodes["antenna"].read_value()
        if scan_nodes.get("rssi"):
            rssi_raw = await scan_nodes["rssi"].read_value()
    except Exception as e:
        print(f"[WARN] LastScan read error: {e}")
        return ts

    rssi = "?"
    if rssi_raw is not None:
        try:
            rssi = f"{int(rssi_raw) / 100:.1f}"
        except Exception:
            rssi = str(rssi_raw)

    epc = epc_to_hex(epc_raw)
    if epc:
        session.add_tag(epc, str(ant), rssi)
    return ts


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
                print("  " * depth + f"[{bn.NamespaceIndex}:{bn.Name}]  {nid}")
                await _print(child, depth + 1)
            except Exception:
                continue

    print("\n" + "=" * 60)
    print("  OPC UA node tree  (DEBUG_BROWSE = True)")
    print("  Search for 'Input', 'DI', 'IOLink', 'ProcessData' nodes")
    print("=" * 60)
    await _print(objects, 0)
    print("=" * 60 + "\n")
