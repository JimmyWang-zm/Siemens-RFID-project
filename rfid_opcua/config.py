"""
rfid_opcua.config
─────────────────
All user-configurable constants for the OPC UA RFID logger.
Edit this file to match your reader / network / sensor setup.
"""

# ── OPC UA connection ─────────────────────────────────────────────────────────
OPCUA_URL      = "opc.tcp://192.168.0.254:4840"
OPCUA_USER     = ""          # leave empty for anonymous / WBM anonymous access
OPCUA_PASS     = ""

# ── Reader / read-point ───────────────────────────────────────────────────────
READ_POINT     = 1           # read-point index (1-based; RF695R supports up to 4)

# ── Output ────────────────────────────────────────────────────────────────────
OUTPUT_DIR     = r"C:\rfid_logger\records"

# ── Trigger (photoelectric sensor via IO-Link / digital input) ─────────────────
DI_CHANNEL     = 0           # IO-Link / DI channel index (0-based)

# ── Timing / polling ─────────────────────────────────────────────────────────
POLL_INTERVAL  = 0.2         # seconds — fallback DI trigger poll
RETRY_DELAY    = 5           # seconds before reconnect attempt
DI_SAMPLE_MS   = 100         # ms — OPC UA sampling interval for DI subscription

# ── Event-based tag reading ───────────────────────────────────────────────────
# True = use RfidScanEventType subscription (recommended by Siemens manual §2.3.2).
# Automatically falls back to LastScan* polling if the server doesn't support events.
PREFER_EVENTS          = True
EVENT_PUBLISH_INTERVAL = 100   # ms — OPC UA subscription publish interval for events

# ── Debug ─────────────────────────────────────────────────────────────────────
# Set True once to print the full OPC UA node tree and locate the DI node path.
# Set back to False for normal operation after the node path is confirmed.
DEBUG_BROWSE   = False
