# Siemens RF695R RFID Logger

Scripts for logging RFID reads from a Siemens SIMATIC RF695R reader (with RF662A antenna) to local files.

Default reader address: `https://192.168.0.254/`

---

## OPC UA Logger — Recommended (`rfid_opcua_logger.py`)

Connects via OPC UA and implements two core features:

**Feature 1 — Auto start/stop scanning (DI sensor trigger)**
Polls Digital Input 0 (IO-Link photoelectric sensor) every 200 ms.
- Sensor sees reflector → DI HIGH (yellow LED on) → `ScanStart`
- Beam blocked by object → DI LOW (yellow LED off) → `ScanStop`

A legacy `"Presence"` mode (OPC UA built-in field detection) is also supported via `TRIGGER_SOURCE`.

**Feature 2 — Tag data saved to CSV**
Subscribes to `RfidScanEventType` events. Writes all tags from each scan session to a daily CSV file.

---

### WBM setup — OPC UA (Settings › Communication › OPC UA)

| Setting | Required value |
|---------|----------------|
| Mode | Main application (uncheck Parallel) |
| Security | Allow anonymous access |
| Port | 4840 (default) |

---

### WBM setup — DI/DO (Settings › DIDO) for Feature 1

These steps configure the RF695R's IO-Link / digital input to drive the yellow indicator and let the software detect sensor state via OPC UA.

**1. Select IO-Link mode**

At the top of the DIDO page, select **IO-Link** from the mode dropdown.

**2. Wire the sensor**

Connect the photoelectric sensor signal wire (NPN/PNP output) to the reader's **Input 0** terminal (DI0).

**3. Add Output 0 events (yellow indicator)**

On the **Output 0** tab, click **+** twice and add these two event rules:

| # | On | Input | Edge | Then | Output | Action |
|---|----|-------|------|------|--------|--------|
| 1 | Input change | Input 0 | Rising | → | Output 0 | On |
| 2 | Input change | Input 0 | Falling | → | Output 0 | Off |

- **Rising / On**: sensor detects reflector → yellow LED turns on → software issues `ScanStart`
- **Falling / Off**: beam blocked by object → yellow LED turns off → software issues `ScanStop`

Click **Save** and apply the configuration.

> **Note:** The DO event rules control the physical indicator output.
> The actual `ScanStart` / `ScanStop` commands are issued by `rfid_opcua_logger.py`
> based on polling the same DI0 state via OPC UA.

---

### Quick start

```powershell
pip install asyncua
python rfid_opcua_logger.py
```

Or double-click `run_opcua_logger.bat` — it installs the dependency automatically on first run.

---

### Configuration (`rfid_opcua_logger.py` header)

| Variable | Default | Description |
|----------|---------|-------------|
| `OPCUA_URL` | `opc.tcp://192.168.0.254:4840` | OPC UA server endpoint |
| `OPCUA_USER` / `OPCUA_PASS` | empty | WBM credentials (leave empty for anonymous) |
| `OUTPUT_DIR` | `C:\rfid_logger\records` | CSV output directory |
| `READ_POINT` | `1` | Read point index (RF695R supports up to 4) |
| `POLL_INTERVAL` | `0.2` | Trigger poll interval in seconds |
| `TRIGGER_SOURCE` | `"DI"` | `"DI"` = IO-Link sensor; `"Presence"` = OPC UA built-in |
| `DI_CHANNEL` | `0` | IO-Link / digital input channel index (0-based) |
| `DEBUG_BROWSE` | `False` | Set `True` once to print the OPC UA tree and locate the DI node |

#### First-run node discovery

If the logger prints `DI0 node not found`, the OPC UA path to the digital input differs on your firmware version. Set `DEBUG_BROWSE = True`, run the script once, and search the printed tree for nodes named `Input`, `DI`, `IOLink`, or `ProcessData`. Then set `DEBUG_BROWSE = False` for normal operation.

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
