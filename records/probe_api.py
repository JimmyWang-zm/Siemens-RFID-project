import urllib.request
import gzip
import ssl
import re
import json

ctx = ssl._create_unverified_context()

def fetch(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "rfid-probe/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
            raw = r.read()
            ct = r.headers.get("Content-Type", "")
            status = r.status
        try:
            body = gzip.decompress(raw)
        except Exception:
            body = raw
        text = body.decode("utf-8", errors="replace")
        return status, ct, text
    except urllib.error.HTTPError as e:
        return e.code, "", ""
    except Exception as e:
        return None, "", str(e)


BASE = "http://192.168.0.254"

CANDIDATES = [
    "/",
    "/api/tags",
    "/api/v1/tags",
    "/api/v1/rfid/tags",
    "/api/rfid",
    "/api/diagnostics",
    "/api/monitor",
    "/api/read",
    "/api/data",
    "/TagData",
    "/tagdata",
    "/Monitor",
    "/monitor",
    "/Diagnostics",
    "/diagnostics",
    "/DiagnosticData",
    "/RfidData",
    "/rfid",
    "/data/tags",
    "/data/rfid",
    "/json/tags",
    "/json",
    "/status",
    "/api/status",
    "/api/v1/status",
    "/api/inventory",
    "/inventory",
    "/events",
    "/api/events",
    "/cgi-bin/data",
    "/cgi-bin/tags",
]

print(f"Probing {BASE} for RFID data endpoints...\n")
for path in CANDIDATES:
    url = BASE + path
    status, ct, text = fetch(url)
    preview = text[:120].replace("\n", " ").replace("\r", "") if text else ""
    tag_hint = any(k in text.lower() for k in ("epc", "tag", "uid", "transponder", "rfid")) if text else False
    marker = " *** TAG DATA?" if tag_hint else ""
    print(f"  [{status}] {path:40s}  {ct[:30]:30s}  {preview[:80]}{marker}")

# Also try HTTPS
print(f"\nProbing HTTPS base...")
status, ct, text = fetch("https://192.168.0.254/", timeout=15)
print(f"  [{status}] HTTPS /  ct={ct[:40]}")
if text:
    token_m = re.search(r'"token"\s*:\s*"([^"]+)"', text)
    if token_m:
        print(f"  Session token: {token_m.group(1)}")
