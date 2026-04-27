"""
rfid_opcua.handlers
───────────────────
OPC UA subscription callback handlers:

  ScanEventHandler     — RfidScanEventType events (tag reads)
  DIHandler            — DigitalInputs data-change (sensor trigger)
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from .config import DI_CHANNEL

if TYPE_CHECKING:
    from .session import Session


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

    def __init__(self, session: Session):
        self._session = session
        self.event_count = 0
        self.last_event_time: float = 0.0

    # -- asyncua callback ---------------------------------------------------

    def event_notification(self, event):
        self.event_count += 1
        self.last_event_time = time.time()
        if self.event_count == 1:
            print("[EVT]  First scan event received — event pipeline active")
            self._dump_event_fields(event)
        try:
            self._handle(event)
        except Exception as e:
            print(f"[WARN] Scan event processing error: {e}")

    # -- debug --------------------------------------------------------------

    @staticmethod
    def _dump_event_fields(event):
        """Print every attribute of the first scan event for diagnostics."""
        print("[DEBUG] Event fields:")
        for attr in sorted(vars(event)):
            val = getattr(event, attr, None)
            preview = repr(val)[:120]
            print(f"         {attr} = {preview}")
        results = (
            getattr(event, "ScanResult", None)
            or getattr(event, "Results", None)
            or getattr(event, "ScanResults", None)
        )
        if results:
            first = results[0] if isinstance(results, (list, tuple)) else results
            if hasattr(first, "Body") and first.Body is not None:
                first = first.Body
            print("[DEBUG] First ScanResult fields:")
            for attr in sorted(vars(first)) if hasattr(first, "__dict__") else []:
                val = getattr(first, attr, None)
                preview = repr(val)[:120]
                print(f"         {attr} = {preview}")

    # -- internal -----------------------------------------------------------

    def _handle(self, event):
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
                ant  = self._extract_antenna(r)
                rssi = self._extract_rssi(r)
                if epc:
                    self._session.add_tag(epc, str(ant), str(rssi))
            return

        # Fallback: fields directly on the event object
        epc = self._extract_epc(event)
        if epc:
            ant  = self._extract_antenna(event)
            rssi = self._extract_rssi(event)
            self._session.add_tag(epc, str(ant), str(rssi))

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
            if val is not None:
                return str(val)
        return "?"

    @staticmethod
    def _extract_rssi(obj) -> str:
        for attr in ("Strength", "RSSI", "CurrentPowerLevel",
                     "strength", "rssi", "SignalStrength", "Rssi"):
            val = getattr(obj, attr, None)
            if val is not None:
                try:
                    return f"{int(val) / 100:.1f}"
                except (ValueError, TypeError):
                    return str(val)
        return "?"


# ═══════════════════════════════════════════════════════════════════════════════
#  DI data-change handler  (Step 2)
# ═══════════════════════════════════════════════════════════════════════════════

class DIHandler:
    """Handles data-change notifications for the DigitalInputs node."""

    def __init__(self, initial_val: bool, on_edge):
        """
        Parameters
        ----------
        initial_val : current DI state at subscription time.
        on_edge     : async callable(di_val: bool) invoked on rising/falling edge.
        """
        self.last_val: bool = initial_val
        self.change_count = 0
        self._on_edge = on_edge

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
        asyncio.get_event_loop().create_task(self._on_edge(di_val))


