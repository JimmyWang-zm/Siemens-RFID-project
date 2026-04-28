"""
rfid_opcua.handlers
───────────────────
OPC UA subscription callback handlers:

  ScanEventHandler     — RfidScanEventType events (tag reads)
  DIHandler            — DigitalInputs data-change (sensor trigger)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .config import DI_CHANNEL, DI_DEBOUNCE_S, DI_STOP_DELAY_S

if TYPE_CHECKING:
    from .session import Session

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Utility
# ═══════════════════════════════════════════════════════════════════════════════

def epc_to_hex(raw) -> str:
    """Convert raw EPC/UID value to uppercase hex string."""
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.hex().upper()
    return str(raw)


# ═══════════════════════════════════════════════════════════════════════════════
#  Scan-event handler  (Step 1)
# ═══════════════════════════════════════════════════════════════════════════════

class ScanEventHandler:
    """
    OPC UA subscription handler for RfidScanEventType events.

    Each event carries an array of RfidScanResult structures — one per
    transponder detected in a scan cycle.  This avoids the LastScan*
    single-variable overwrite problem (manual §2.3.1) and reacts instantly
    instead of polling every 50 ms.
    """

    def __init__(self, session: Session, scan_nodes: dict | None = None):
        self._session = session
        self._scan_nodes = scan_nodes or {}
        self.event_count = 0
        self.last_event_time: float = 0.0
        self.watchdog_armed: bool = False
        self._last_epcs: list[str] = []   # EPCs from the most recent event

    def reset_watchdog(self):
        """Call when a new scan session begins to restart the silence timer."""
        self.watchdog_armed = False
        self.last_event_time = time.time()

    # -- asyncua callback ---------------------------------------------------

    def event_notification(self, event):
        self.event_count += 1
        self.last_event_time = time.time()
        self.watchdog_armed = True
        if self.event_count <= 2:
            log.info("Scan event #%d received — dumping fields for diagnostics",
                     self.event_count)
            self._dump_event_fields(event)
        self._last_epcs = []               # reset before _handle populates it
        try:
            self._handle(event)
        except Exception as e:
            log.warning("Scan event processing error: %s", e)

        # Fallback: supplement tags that still lack Ant/RSSI from LastScan*
        # Only useful for single-tag events where Sighting data was absent.
        missing = [epc for epc in self._last_epcs
                   if self._session.tags.get(epc)
                   and self._session.tags[epc][-1][1] == "?"
                   and self._session.tags[epc][-1][2] == "?"]
        if missing and len(missing) == 1 and self._scan_nodes:
            try:
                asyncio.get_event_loop().create_task(
                    self._supplement_from_last_scan(missing[0])
                )
            except Exception:
                pass

    # -- LastScan* supplement ------------------------------------------------

    async def _supplement_from_last_scan(self, epc: str):
        """
        Last-resort fallback: read LastScanAntenna / LastScanRSSI for a
        SINGLE tag whose Sighting data was empty.
        """
        if not self._session.active:
            return

        ant_node  = self._scan_nodes.get("antenna")
        rssi_node = self._scan_nodes.get("rssi")

        # Two attempts: 30 ms + 100 ms settle
        for attempt in range(2):
            settle = 0.03 if attempt == 0 else 0.10
            await asyncio.sleep(settle)

            ant_val:  str | None = None
            rssi_val: str | None = None

            try:
                if ant_node is not None:
                    raw = await ant_node.read_value()
                    if raw is not None:
                        s = str(raw).strip()
                        if s:
                            ant_val = s
            except Exception:
                pass

            try:
                if rssi_node is not None:
                    raw = await rssi_node.read_value()
                    if raw is not None:
                        num = int(raw)
                        rssi_val = self._format_rssi(num)
            except Exception:
                pass

            if ant_val is None and rssi_val is None:
                continue

            rows = self._session.tags.get(epc)
            if not rows:
                return

            new_rows = []
            updated = False
            for ts, ant, rssi in rows:
                if ant == "?" and ant_val is not None:
                    ant = ant_val; updated = True
                if rssi == "?" and rssi_val is not None:
                    rssi = rssi_val; updated = True
                new_rows.append((ts, ant, rssi))
            if updated:
                self._session.tags[epc] = new_rows
                log.info("    ↻ %s  Ant:%s  RSSI:%s (LastScan* fallback)",
                         epc, ant_val or "?", rssi_val or "?")
            return

    # -- debug --------------------------------------------------------------

    @staticmethod
    def _dump_event_fields(event):
        """Print every attribute of the first scan event for diagnostics."""
        log.info("Event fields:")
        for attr in sorted(vars(event)):
            val = getattr(event, attr, None)
            log.info("   %s = %s", attr, repr(val)[:200])
        results = (
            getattr(event, "ScanResult", None)
            or getattr(event, "Results", None)
            or getattr(event, "ScanResults", None)
        )
        if results:
            first = results[0] if isinstance(results, (list, tuple)) else results
            if hasattr(first, "Body") and first.Body is not None:
                first = first.Body
            log.info("First ScanResult fields:")
            ScanEventHandler._dump_obj(first, "  ScanResult", log)

    @staticmethod
    def _dump_obj(obj, prefix: str, logger):
        """Recursively dump an object's attributes (3 levels deep)."""
        if not hasattr(obj, "__dict__"):
            logger.info("   %s = %s", prefix, repr(obj)[:200])
            return
        for attr in sorted(vars(obj)):
            val = getattr(obj, attr, None)
            logger.info("   %s.%s = %s", prefix, attr, repr(val)[:200])
            # Two more levels for union/struct children
            if val is not None and hasattr(val, "__dict__") and not attr.startswith("_"):
                for a2 in sorted(vars(val)):
                    v2 = getattr(val, a2, None)
                    logger.info("   %s.%s.%s = %s", prefix, attr, a2, repr(v2)[:200])
                    if v2 is not None and hasattr(v2, "__dict__") and not a2.startswith("_"):
                        for a3 in sorted(vars(v2)):
                            v3 = getattr(v2, a3, None)
                            logger.info("   %s.%s.%s.%s = %s", prefix, attr, a2, a3, repr(v3)[:200])
            # Also dump list elements (e.g. Sighting array)
            if isinstance(val, (list, tuple)):
                for i, item in enumerate(val[:3]):
                    if hasattr(item, "__dict__"):
                        ScanEventHandler._dump_obj(item, f"  {prefix}.{attr}[{i}]", logger)

    # -- static helpers -----------------------------------------------------

    @staticmethod
    def _format_rssi(num) -> str:
        """Format RSSI integer (may be in cdBm or dBm)."""
        try:
            n = int(num)
            if abs(n) > 200:
                return f"{n / 100:.1f}"
            return str(n)
        except (ValueError, TypeError):
            s = str(num).strip()
            return s if s else "?"

    # -- internal -----------------------------------------------------------

    def _handle(self, event):
        """
        Extract tags from an RfidScanEventType event.

        Per the OPC UA AutoID spec (§9.3.12), each RfidScanResult contains:
          - ScanData (union: EPC/UID/ByteString)
          - Sighting[] (array of RfidSighting, each with Antenna, Strength)
          - Location (optional union)
          - Timestamp
        """
        results = (
            getattr(event, "ScanResult", None)
            or getattr(event, "Results", None)
            or getattr(event, "ScanResults", None)
        )
        if results is not None:
            if not isinstance(results, (list, tuple)):
                results = [results]
            for r in results:
                if hasattr(r, "Body") and r.Body is not None:
                    r = r.Body
                epc  = self._extract_epc(r)
                # Per-tag Antenna/RSSI from RfidSighting array (§9.3.12/13)
                ant, rssi = self._extract_from_sighting(r)
                # Fallback: top-level attributes
                if ant == "?":
                    ant = self._extract_antenna(r)
                if rssi == "?":
                    rssi = self._extract_rssi(r)
                # Fallback: Location union for antenna name
                if ant == "?":
                    ant = self._extract_location(r)
                # Fallback: ScanData sub-object
                scan_data = self._unwrap_scan_data(r)
                if scan_data is not None:
                    if ant == "?":
                        ant = self._extract_antenna(scan_data)
                    if rssi == "?":
                        rssi = self._extract_rssi(scan_data)
                if epc:
                    self._session.add_tag(epc, str(ant), str(rssi))
                    self._last_epcs.append(epc)
            return

        # Fallback: fields directly on the event object
        epc = self._extract_epc(event)
        if epc:
            ant, rssi = self._extract_from_sighting(event)
            if ant == "?":
                ant = self._extract_antenna(event)
            if rssi == "?":
                rssi = self._extract_rssi(event)
            if ant == "?":
                ant = self._extract_location(event)
            self._session.add_tag(epc, str(ant), str(rssi))
            self._last_epcs.append(epc)

    # -- Sighting extraction (primary source of Antenna/RSSI) ---------------

    @staticmethod
    def _extract_from_sighting(obj) -> tuple[str, str]:
        """
        Extract Antenna and Strength (RSSI) from the RfidSighting array.

        RfidScanResult.Sighting is an array of RfidSighting:
            RfidSighting { Antenna: Int32, Strength: Int32,
                           Timestamp: UtcTime, CurrentPowerLevel: Int32 }
        """
        ant_val = "?"
        rssi_val = "?"

        sighting = (
            getattr(obj, "Sighting", None)
            or getattr(obj, "Sightings", None)
            or getattr(obj, "sighting", None)
        )
        if sighting is None:
            return ant_val, rssi_val

        # May be wrapped in a Variant
        if hasattr(sighting, "Body") and sighting.Body is not None:
            sighting = sighting.Body

        if not isinstance(sighting, (list, tuple)):
            sighting = [sighting]

        if not sighting:
            return ant_val, rssi_val

        # Use the first sighting (strongest read in that cycle)
        s = sighting[0]
        if hasattr(s, "Body") and s.Body is not None:
            s = s.Body

        # Antenna
        a = getattr(s, "Antenna", None)
        if a is None:
            a = getattr(s, "AntennaId", None)
        if a is not None:
            inner = getattr(a, "Value", a)
            if inner is not None:
                ant_val = str(inner).strip()

        # Strength (RSSI)
        r = getattr(s, "Strength", None)
        if r is None:
            r = getattr(s, "RSSI", None)
        if r is None:
            r = getattr(s, "CurrentPowerLevel", None)
        if r is not None:
            inner = getattr(r, "Value", r)
            if inner is not None:
                rssi_val = ScanEventHandler._format_rssi(inner)

        return ant_val, rssi_val

    # -- Location union extraction ------------------------------------------

    @staticmethod
    def _extract_location(obj) -> str:
        """Extract antenna/location name from the Location union."""
        loc = getattr(obj, "Location", None)
        if loc is None:
            return "?"
        if hasattr(loc, "Body") and loc.Body is not None:
            loc = loc.Body
        # LocationName variant (Name field in the union)
        for attr in ("Name", "Value", "name", "NMEA"):
            val = getattr(loc, attr, None)
            if val is not None:
                s = str(val).strip()
                if s:
                    return s
        return "?"

    @staticmethod
    def _unwrap_scan_data(obj):
        """Return the innermost ScanData child object, or None."""
        raw = getattr(obj, "ScanData", None)
        if raw is None:
            return None
        if hasattr(raw, "Body") and raw.Body is not None:
            raw = raw.Body
        val = getattr(raw, "Value", None)
        if val is not None:
            return val
        return raw

    @staticmethod
    def _extract_epc(obj) -> str:
        for attr in ("ScanData", "Identifier", "TagId", "EPC", "UID", "Data"):
            raw = getattr(obj, attr, None)
            if raw is None:
                continue
            if hasattr(raw, "Body") and raw.Body is not None:
                raw = raw.Body
            val = getattr(raw, "Value", None)
            if val is not None:
                raw = val
            uid = getattr(raw, "UId", None) or getattr(raw, "UID", None)
            if uid is not None:
                return epc_to_hex(uid)
            return epc_to_hex(raw)
        if isinstance(obj, (bytes, bytearray)):
            return obj.hex().upper()
        return ""

    @staticmethod
    def _extract_antenna(obj) -> str:
        for attr in ("Antenna", "AntennaId", "AntennaName",
                     "antenna", "Location", "location", "Ant"):
            val = getattr(obj, attr, None)
            if val is None:
                continue
            # Unwrap union types (e.g. Location(Value='RF662A/1'))
            inner = getattr(val, "Value", val)
            if inner is None:
                continue
            s = str(inner).strip()
            if s:
                return s
        return "?"

    @staticmethod
    def _extract_rssi(obj) -> str:
        for attr in ("Strength", "RSSI", "CurrentPowerLevel", "CodedTagStrength",
                     "strength", "rssi", "SignalStrength", "Rssi",
                     "TagStrength", "Power", "power", "Level"):
            val = getattr(obj, attr, None)
            if val is None:
                continue
            # Unwrap union types
            inner = getattr(val, "Value", val)
            if inner is None:
                continue
            return ScanEventHandler._format_rssi(inner)
        return "?"


# ═══════════════════════════════════════════════════════════════════════════════
#  DI data-change handler  (Step 2)
# ═══════════════════════════════════════════════════════════════════════════════

class DIHandler:
    """
    Handles data-change notifications for the DigitalInputs node.

    Debounce logic (handles noisy / flickering sensor):

    * **Rising edge (beam clear):**  ignored if it arrives within
      ``DI_DEBOUNCE_S`` of the last confirmed session-stop.
    * **Falling edge (beam blocked):**  a stop is *scheduled* after
      ``DI_STOP_DELAY_S``.  If the beam clears again before the delay
      expires the stop is cancelled and scanning continues uninterrupted.
    """

    def __init__(self, initial_val: bool, on_edge):
        """
        Parameters
        ----------
        initial_val : current DI state at subscription time.
        on_edge     : async callable(di_val: bool) invoked on confirmed edge.
        """
        self.last_val: bool = initial_val
        self.change_count = 0
        self._on_edge = on_edge
        self._last_stop_ts: float = 0.0          # monotonic time of last confirmed stop
        self._pending_stop: asyncio.TimerHandle | None = None

    # ───────────────────────────────────────────────────────────────────

    def _cancel_pending_stop(self):
        if self._pending_stop is not None:
            self._pending_stop.cancel()
            self._pending_stop = None
            log.debug("Pending stop cancelled — beam cleared again")

    def _fire_stop(self):
        """Called after DI_STOP_DELAY_S if no rising edge cancelled it."""
        self._pending_stop = None
        self._last_stop_ts = time.monotonic()
        asyncio.get_event_loop().create_task(self._on_edge(False))

    # ───────────────────────────────────────────────────────────────────

    def datachange_notification(self, node, val, data):
        self.change_count += 1
        try:
            di_val = bool((int(val) >> DI_CHANNEL) & 1)
        except Exception:
            return
        old = self.last_val
        self.last_val = di_val
        if di_val == old:
            return

        now = time.monotonic()

        if di_val:
            # Rising edge — beam clear
            self._cancel_pending_stop()

            elapsed = now - self._last_stop_ts
            if elapsed < DI_DEBOUNCE_S:
                log.debug(
                    "DI rising edge suppressed (%.1fs < %.1fs debounce)",
                    elapsed, DI_DEBOUNCE_S,
                )
                return

            asyncio.get_event_loop().create_task(self._on_edge(True))
        else:
            # Falling edge — schedule delayed stop
            self._cancel_pending_stop()
            loop = asyncio.get_event_loop()
            self._pending_stop = loop.call_later(DI_STOP_DELAY_S, self._fire_stop)
            log.debug("Beam interrupted — will stop in %.1fs if not cleared", DI_STOP_DELAY_S)


