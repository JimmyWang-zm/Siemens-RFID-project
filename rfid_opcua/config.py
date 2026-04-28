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
import pathlib as _pathlib
_PROJECT_DIR   = _pathlib.Path(__file__).resolve().parent.parent
OUTPUT_DIR     = str(_PROJECT_DIR / "records")
CSV_FILENAME   = "RFID_log.csv"             # single file — appended to forever

# ── Trigger (photoelectric sensor via IO-Link / digital input) ─────────────────
DI_CHANNEL     = 0           # IO-Link / DI channel index (0-based)
DI_DEBOUNCE_S  = 0.3         # seconds — ignore re-triggers within this window
DI_STOP_DELAY_S = 0.15       # seconds — wait before ending session on falling edge
                              #   (absorbs brief sensor flicker / beam interruptions)

# ── Timing ────────────────────────────────────────────────────────────────────
RETRY_DELAY    = 5           # seconds before reconnect attempt
DI_SAMPLE_MS   = 100         # ms — OPC UA sampling interval for DI subscription

# ── Event-based tag reading ───────────────────────────────────────────────────
# RfidScanEventType subscription (Siemens manual §2.3.2).
EVENT_PUBLISH_INTERVAL = 100   # ms — OPC UA subscription publish interval for events

# ── Watchdog ──────────────────────────────────────────────────────────────────
# If no scan event arrives for this many seconds during an active scan session,
# the watchdog treats the subscription as dead and forces a reconnect.
# Set to 0 to disable.
WATCHDOG_TIMEOUT       = 30    # seconds — max silence before reconnect

# ── Debug ─────────────────────────────────────────────────────────────────────
# Set True once to print the full OPC UA node tree and locate the DI node path.
# Set back to False for normal operation after the node path is confirmed.
DEBUG_BROWSE   = False
# ── Logging ───────────────────────────────────────────────────────────────
LOG_DIR        = str(_PROJECT_DIR / "logs")    # directory for rotating log files
LOG_LEVEL      = "INFO"                   # DEBUG, INFO, WARNING, ERROR
LOG_MAX_BYTES  = 5 * 1024 * 1024          # 5 MB per log file
LOG_BACKUP_COUNT = 5                      # keep 5 rotated files