import ssl, requests, warnings, time, random, re
from requests.adapters import HTTPAdapter
warnings.filterwarnings("ignore")

class LegacyTLS(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except Exception:
            pass
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

BASE = "https://192.168.0.254"
USER = "admin"
PASS = "Siemens123$"
READPOINT = "Readpoint_1"

sess = requests.Session()
sess.mount("https://", LegacyTLS())

# ??
print("1. ??...")
r = sess.get(f"{BASE}/Pages/Login.mwsl", timeout=8)
print(f"   GET ???: {r.status_code}")
fields = re.findall(r'name=["\x27]([^"\x27]+)["\x27]', r.text, re.I)
print(f"   ????: {fields[:10]}")

data = {"Login": USER, "Password": PASS}
for f in fields:
    if f.lower() not in ("login","password") and f not in data:
        data[f] = ""
r2 = sess.post(f"{BASE}/Pages/Login.mwsl", data=data, timeout=8)
print(f"   POST ??: {r2.status_code}  cookies: {dict(sess.cookies)}")

# ?? API
mid = str(int(time.time() * 1000))
def api(path):
    url = f"{BASE}{path}&r={random.random()}" if "?" in path else f"{BASE}{path}?r={random.random()}"
    return sess.get(url, timeout=6)

print(f"\n2. TagMonitorStart (mid={mid})")
r3 = api(f"/Diagnosis/TagMonitorStart?monitorID={mid}")
print(f"   {r3.status_code}: {r3.text[:200]}")

print("\n3. TriggerSource start")
r4 = api(f"/Diagnosis/TriggerSource?source={READPOINT}&type=start&monitorID={mid}")
print(f"   {r4.status_code}: {r4.text[:200]}")

print("\n4. GetTagMonitorData x3")
for i in range(3):
    time.sleep(1)
    r5 = api(f"/Diagnosis/GetTagMonitorData?monitorID={mid}")
    print(f"   [{i+1}] {r5.status_code}: {r5.text[:400]}")
