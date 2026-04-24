# Siemens RF695R RFID Logger

Scripts for logging RFID reads from a Siemens SIMATIC RF695R reader (with RF662A antenna) to local files.

Default reader address: `https://192.168.0.254/`

---

## OPC UA Logger — Recommended (`rfid_opcua_logger.py`)

Connects via OPC UA and implements two core features:

**Feature 1 — Auto start/stop scanning**
Polls `Diagnostics > Presence` every 500 ms. Calls `ScanStart` when a cart arrives and `ScanStop` when it leaves.

**Feature 2 — Tag data saved to CSV**
Subscribes to `RfidScanEventType` events. Writes all tags from each cart pass to a daily CSV file.

### WBM setup (Settings > Communication > OPC UA)

| Setting | Required value |
|---------|----------------|
| Mode | Main application (uncheck Parallel) |
| Presence events | Enabled |
| Security | Allow anonymous access |
| Port | 4840 (default) |

### Quick start

```powershell
pip install asyncua
python rfid_opcua_logger.py
```

Or double-click `run_opcua_logger.bat` — it installs the dependency automatically on first run.

### Configuration (`rfid_opcua_logger.py` header)

| Variable | Default | Description |
|----------|---------|-------------|
| `OPCUA_URL` | `opc.tcp://192.168.0.254:4840` | OPC UA server endpoint |
| `OPCUA_USER` / `OPCUA_PASS` | empty | WBM credentials (leave empty for anonymous) |
| `OUTPUT_DIR` | `C:\rfid_logger\records` | CSV output directory |
| `READ_POINT` | `1` | Read point index (RF695R supports up to 4) |
| `POLL_INTERVAL` | `0.5` | Presence poll interval in seconds |

---

## TCP XML Logger (`rfid_auto_save.py`)

Connects to the reader's TCP XML channel. Detects cart arrival by the first tag seen and cart departure by an idle timeout (no tags for `IDLE_TIMEOUT` seconds).

```powershell
python rfid_auto_save.py
```

---

## HTTP Polling Logger (`rfid_logger.py`)

Polls the reader's web interface or API endpoint and logs every change.

```powershell
python rfid_logger.py --url "https://192.168.0.254/" --interval 1 --insecure
```

Common options:

| Flag | Description |
|------|-------------|
| `--url` | Reader URL (default `https://192.168.0.254/`) |
| `--interval` | Poll interval in seconds |
| `--timeout` | Request timeout in seconds |
| `--log-all` | Log every poll, not just on change |
| `--insecure` | Skip HTTPS certificate check |
| `--cookie` | Browser cookie for authenticated endpoints |
| `--basic-auth` | HTTP Basic auth (`user:pass`) |
| `--mode xml` | Use RF69xR XML channel (recommended) |
| `--xml-port` | XML channel TCP port (set in WBM) |
| `--discover-endpoints` | Auto-scan for data endpoints |
| `--debug-payload` | Dump raw response to file for debugging |

---

## Output files

| File | Description |
|------|-------------|
| `RFID_YYYY-MM-DD.csv` | Daily tag log (OPC UA / XML session loggers) |
| `logs/rfid_reads.csv` | Continuous log (HTTP polling logger) |
| `logs/rfid_reads.jsonl` | Full raw records in JSON Lines format |

### CSV columns (OPC UA / XML loggers)

| Column | Description |
|--------|-------------|
| `Timestamp` | Local time of the read |
| `EPC/Tag ID` | Tag identifier |
| `Antenna` | Antenna that detected the tag |
| `RSSI (dBm)` | Signal strength |
| `Session ID` | Unique ID per cart pass |

---

## Notes

- Make sure the PC and reader are on the same subnet and can reach each other.
- For 24/7 operation, consider setting the script to run on Windows startup.
- If `tags=0` with the HTTP logger, the endpoint likely requires authentication — use `--cookie` or `--basic-auth`.
