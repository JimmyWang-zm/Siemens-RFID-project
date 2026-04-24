#!/usr/bin/env python3
"""
RFID polling logger for Siemens RF69x reader web output.

Default behavior:
- Polls http://192.168.0.254/
- Tries to parse JSON first, then falls back to text/HTML parsing
- Appends one record whenever payload changes (or every poll with --log-all)
- Writes both CSV and JSONL for easy viewing and post-processing
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import gzip
import hashlib
import json
import re
import socket as socket_lib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


TAG_KEYWORDS = {"tag", "uid", "epc", "tid", "id", "serial", "identifier"}
HEX_RE = re.compile(r"\b[0-9A-Fa-f]{8,64}\b")
DECIMAL_RE = re.compile(r"\b\d{8,20}\b")
WHITESPACE_RE = re.compile(r"\s+")
SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
PATH_CANDIDATE_RE = re.compile(
    r"""["'](/[^"'?#]{1,240}(?:api|json|ajax|rpc|tag|epc|uid|transponder|reader|monitor|data|svc|cgi)[^"']*)["']""",
    re.IGNORECASE,
)
JS_OBJECT_PATH_RE = re.compile(
    r"""(?<![A-Za-z0-9_])/(?:[A-Za-z0-9_.-]+/){0,6}[A-Za-z0-9_.-]+\.(?:json|cgi|ashx|php|xml)(?:\?[A-Za-z0-9_=&.-]+)?""",
    re.IGNORECASE,
)
XML_DECL_RE = re.compile(r"<\?xml[^>]*\?>", re.IGNORECASE)


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_headers(header_args: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in header_args:
        if ":" not in raw:
            raise ValueError(f"invalid header (missing ':'): {raw}")
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"invalid header (empty key): {raw}")
        parsed[key] = value
    return parsed


def build_basic_auth_header(credential: str) -> str:
    if ":" not in credential:
        raise ValueError("basic auth must be in USER:PASS format")
    token = base64.b64encode(credential.encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def make_ssl_context(insecure: bool) -> ssl.SSLContext | None:
    return ssl._create_unverified_context() if insecure else None


def fetch_bytes(
    url: str,
    timeout: float,
    insecure: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    headers = {
        "User-Agent": "rfid-logger/1.0",
        "Accept": "application/json,text/plain,text/html,application/javascript,*/*",
        "Accept-Encoding": "gzip,deflate",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url=url, headers=headers)
    context = make_ssl_context(insecure)
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        body = resp.read()
        content_type = resp.headers.get("Content-Type", "")
        content_encoding = (resp.headers.get("Content-Encoding", "") or "").lower()
        if "gzip" in content_encoding:
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
    return body, content_type


def fetch_payload(
    url: str,
    timeout: float,
    insecure: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> str:
    body, content_type = fetch_bytes(
        url=url,
        timeout=timeout,
        insecure=insecure,
        extra_headers=extra_headers,
    )
    encoding = "utf-8"
    lower_content_type = content_type.lower()
    if "charset=" in lower_content_type:
        encoding = lower_content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
    return body.decode(encoding or "utf-8", errors="replace")


def normalize_text(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip()


def collect_from_obj(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_lower = str(key).lower()
            if isinstance(value, (dict, list)):
                collect_from_obj(value, out)
                continue

            if value is None:
                continue

            val_str = normalize_text(str(value))
            if not val_str:
                continue

            if any(keyword in key_lower for keyword in TAG_KEYWORDS):
                out.add(val_str)
                continue

            if HEX_RE.fullmatch(val_str) or DECIMAL_RE.fullmatch(val_str):
                out.add(val_str)
    elif isinstance(obj, list):
        for item in obj:
            collect_from_obj(item, out)


def extract_tags(payload: str) -> list[str]:
    tags: set[str] = set()

    # Try JSON parsing first.
    try:
        data = json.loads(payload)
        collect_from_obj(data, tags)
    except json.JSONDecodeError:
        pass

    # Generic fallback: detect potential EPC/UID-like tokens.
    for token in HEX_RE.findall(payload):
        tags.add(token.upper())
    for token in DECIMAL_RE.findall(payload):
        tags.add(token)

    # Avoid huge text blocks accidentally captured.
    cleaned = {t for t in tags if 4 <= len(t) <= 64}
    return sorted(cleaned)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    ensure_parent(path)
    path.write_text(content, encoding="utf-8")


def append_csv(path: Path, row: dict[str, Any]) -> None:
    ensure_parent(path)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "source_url",
                "host_name",
                "tag_count",
                "tags",
                "payload_hash",
                "raw_preview",
            ],
        )
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def short_preview(payload: str, max_len: int = 200) -> str:
    normalized = normalize_text(payload)
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 3] + "..."


def make_record(url: str, payload: str, tags: list[str]) -> dict[str, Any]:
    payload_hash = hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()
    ts = now_iso()
    return {
        "timestamp": ts,
        "source_url": url,
        "host_name": socket.gethostname(),
        "tag_count": len(tags),
        "tags": "|".join(tags),
        "payload_hash": payload_hash,
        "raw_preview": short_preview(payload),
    }


def endpoint_score(url: str, payload: str, tags: list[str]) -> int:
    score = 0
    if tags:
        score += 100 + len(tags) * 10
    lower_url = url.lower()
    lower_payload = payload.lower()
    for token in ("api", "json", "tag", "epc", "uid", "transponder", "monitor"):
        if token in lower_url:
            score += 4
        if token in lower_payload:
            score += 1
    if "anonymous" in lower_payload and "globaluserdata" in lower_payload:
        score -= 30
    return score


def discover_candidates(base_url: str, homepage_payload: str) -> set[str]:
    candidates: set[str] = set()
    # Candidate paths from homepage HTML itself.
    for match in PATH_CANDIDATE_RE.finditer(homepage_payload):
        candidates.add(urllib.parse.urljoin(base_url, match.group(1)))
    for match in JS_OBJECT_PATH_RE.finditer(homepage_payload):
        candidates.add(urllib.parse.urljoin(base_url, match.group(0)))
    # Script assets to inspect.
    script_urls = [
        urllib.parse.urljoin(base_url, src)
        for src in SCRIPT_SRC_RE.findall(homepage_payload)
    ]
    for script_url in script_urls:
        candidates.add(script_url)
    return candidates


def probe_endpoints(
    base_url: str,
    timeout: float,
    insecure: bool,
    headers: dict[str, str],
    limit: int,
) -> None:
    print("Discover mode: scanning frontend for data endpoints...")
    try:
        homepage = fetch_payload(base_url, timeout, insecure=insecure, extra_headers=headers)
    except Exception as exc:
        print(f"discover failed: cannot fetch base url ({type(exc).__name__}: {exc})")
        return

    discovered = discover_candidates(base_url, homepage)
    queue: list[str] = sorted(discovered)

    # Expand with path candidates found inside script files.
    for item in list(queue):
        if len(queue) >= limit:
            break
        if not item.lower().endswith((".js", ".js.gz", ".json", ".cgi", ".xml")):
            continue
        try:
            text = fetch_payload(item, timeout, insecure=insecure, extra_headers=headers)
        except Exception:
            continue
        for match in PATH_CANDIDATE_RE.finditer(text):
            queue.append(urllib.parse.urljoin(base_url, match.group(1)))
        for match in JS_OBJECT_PATH_RE.finditer(text):
            queue.append(urllib.parse.urljoin(base_url, match.group(0)))

    deduped: list[str] = []
    seen: set[str] = set()
    for url in queue:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
        if len(deduped) >= limit:
            break

    if base_url not in seen:
        deduped.insert(0, base_url)

    print(f"Discover mode: testing {len(deduped)} candidate URLs...")
    ranked: list[tuple[int, str, int, str]] = []
    for url in deduped:
        try:
            payload = fetch_payload(url, timeout, insecure=insecure, extra_headers=headers)
            tags = extract_tags(payload)
            score = endpoint_score(url, payload, tags)
            note = "anonymous-page" if ("globaluserdata" in payload.lower() and "anonymous" in payload.lower()) else "-"
            ranked.append((score, url, len(tags), note))
        except Exception:
            continue

    if not ranked:
        print("Discover mode: no reachable candidate endpoints.")
        return

    ranked.sort(key=lambda x: x[0], reverse=True)
    print("\nTop candidate URLs:")
    for score, url, tag_count, note in ranked[:10]:
        print(f"- score={score:>3} tags={tag_count:<2} note={note:<15} url={url}")
    print("\nTip: pick top URL and run with --url <that_url> --log-all")


class XMLMessageExtractor:
    """
    Incremental XML parser for stream sockets.

    The reader can send multiple top-level XML fragments over one TCP stream.
    We wrap all incoming fragments in a synthetic root node and emit each
    completed top-level XML element as a standalone payload string.
    """

    def __init__(self) -> None:
        self._build_parser()

    def _build_parser(self) -> None:
        self._parser = ET.XMLPullParser(events=("end",))
        self._parser.feed("<stream-root>")

    def feed(self, chunk: str) -> list[str]:
        messages: list[str] = []
        cleaned = XML_DECL_RE.sub("", chunk)
        if not cleaned.strip():
            return messages

        try:
            self._parser.feed(cleaned)
        except ET.ParseError:
            # Defensive reset: malformed frame should not kill logging process.
            self._build_parser()
            return messages

        for _, elem in self._parser.read_events():
            if elem.tag == "stream-root":
                continue
            payload = ET.tostring(elem, encoding="unicode")
            if payload.strip():
                messages.append(payload)
            elem.clear()
        return messages


def process_payload_record(
    payload: str,
    source_url: str,
    csv_path: Path,
    jsonl_path: Path,
    debug_payload_path: Path | None,
    log_all: bool,
    last_hash: str | None,
    unchanged_count: int,
    anonymous_notice_printed: bool,
) -> tuple[str | None, int, bool]:
    if debug_payload_path:
        write_text(debug_payload_path, payload)

    tags = extract_tags(payload)
    record = make_record(source_url, payload, tags)
    lower_payload = payload.lower()
    if (
        not anonymous_notice_printed
        and "globaluserdata" in lower_payload
        and "anonymous" in lower_payload
    ):
        print(
            f"[{record['timestamp']}] info: response is anonymous page, likely needs login session/cookie"
        )
        anonymous_notice_printed = True

    changed = record["payload_hash"] != last_hash
    if log_all or changed:
        append_csv(csv_path, record)
        append_jsonl(
            jsonl_path,
            {
                **record,
                "tags_list": tags,
                "raw_payload": payload,
            },
        )
        print(
            f"[{record['timestamp']}] saved | tags={record['tag_count']} | {record['tags'] or '-'}"
        )
        last_hash = record["payload_hash"]
        unchanged_count = 0
    else:
        unchanged_count += 1
        if unchanged_count % 10 == 0:
            print(
                f"[{record['timestamp']}] waiting | no payload change for {unchanged_count} polls"
            )

    return last_hash, unchanged_count, anonymous_notice_printed


def run_xml_mode(
    *,
    host: str,
    port: int,
    timeout: float,
    reconnect_delay: float,
    buffer_size: int,
    csv_path: Path,
    jsonl_path: Path,
    debug_payload_path: Path | None,
    log_all: bool,
) -> int:
    print("RFID logger started (XML mode)")
    print(f"XML host : {host}")
    print(f"XML port : {port}")
    print(f"Timeout  : {timeout}s")
    print(f"CSV      : {csv_path}")
    print(f"JSONL    : {jsonl_path}")
    if debug_payload_path:
        print(f"DebugRaw : {debug_payload_path}")
    print("Press Ctrl+C to stop\n")

    last_hash: str | None = None
    unchanged_count = 0
    anonymous_notice_printed = False
    error_streak = 0
    idle_ticks = 0

    while True:
        try:
            with socket_lib.create_connection((host, port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                print(f"[{now_iso()}] info: connected to XML channel {host}:{port}")
                extractor = XMLMessageExtractor()
                error_streak = 0
                idle_ticks = 0

                while True:
                    try:
                        chunk = sock.recv(max(buffer_size, 512))
                    except socket_lib.timeout:
                        idle_ticks += 1
                        if idle_ticks % 4 == 0:
                            print(
                                f"[{now_iso()}] waiting: XML channel connected but no frames yet"
                            )
                        continue
                    if not chunk:
                        raise ConnectionError("XML socket closed by remote host")
                    idle_ticks = 0
                    print(
                        f"[{now_iso()}] raw: received {len(chunk)} bytes | "
                        f"{chunk[:120]!r}"
                    )
                    text = chunk.decode("utf-8", errors="replace")
                    for payload in extractor.feed(text):
                        (
                            last_hash,
                            unchanged_count,
                            anonymous_notice_printed,
                        ) = process_payload_record(
                            payload=payload,
                            source_url=f"xml://{host}:{port}",
                            csv_path=csv_path,
                            jsonl_path=jsonl_path,
                            debug_payload_path=debug_payload_path,
                            log_all=log_all,
                            last_hash=last_hash,
                            unchanged_count=unchanged_count,
                            anonymous_notice_printed=anonymous_notice_printed,
                        )
        except KeyboardInterrupt:
            print("\nStopped by user.")
            return 0
        except Exception as exc:
            error_streak += 1
            print(
                f"[{now_iso()}] warning: XML read failed ({type(exc).__name__}: {exc})"
            )
            time.sleep(min(reconnect_delay + error_streak * 0.5, 8.0))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll RFID reader webpage/API and persist each read result."
    )
    parser.add_argument(
        "--url",
        default="https://192.168.0.254/",
        help="Reader page/API URL. Default: %(default)s",
    )
    parser.add_argument(
        "--mode",
        choices=["http", "xml"],
        default="http",
        help="Data source mode: http (WBM/API polling) or xml (official XML socket).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds. Default: %(default)s",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="HTTP timeout in seconds. Default: %(default)s",
    )
    parser.add_argument(
        "--csv",
        default="logs/rfid_reads.csv",
        help="CSV output path. Default: %(default)s",
    )
    parser.add_argument(
        "--jsonl",
        default="logs/rfid_reads.jsonl",
        help="JSONL output path. Default: %(default)s",
    )
    parser.add_argument(
        "--log-all",
        action="store_true",
        help="Log every poll result. By default logs only when payload changes.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable HTTPS certificate verification (useful for device self-signed cert).",
    )
    parser.add_argument(
        "--cookie",
        default="",
        help="Cookie header value copied from browser (for authenticated endpoints).",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="KEY:VALUE",
        help="Extra request header (can be used multiple times).",
    )
    parser.add_argument(
        "--debug-payload",
        default="",
        help="Write latest raw response body to this file each poll (debugging).",
    )
    parser.add_argument(
        "--basic-auth",
        default="",
        metavar="USER:PASS",
        help="Send HTTP Basic Authorization header (if device is configured for basic auth).",
    )
    parser.add_argument(
        "--discover-endpoints",
        action="store_true",
        help="Scan page/scripts and test candidate data URLs, then exit.",
    )
    parser.add_argument(
        "--discover-limit",
        type=int,
        default=120,
        help="Maximum candidate URLs to test in discover mode. Default: %(default)s",
    )
    parser.add_argument(
        "--xml-host",
        default="",
        help="Reader host/IP for XML mode. If empty, host is taken from --url.",
    )
    parser.add_argument(
        "--xml-port",
        type=int,
        default=0,
        help="Reader XML channel TCP port configured in WBM (required in XML mode).",
    )
    parser.add_argument(
        "--xml-reconnect-delay",
        type=float,
        default=2.0,
        help="Reconnect delay in seconds for XML mode. Default: %(default)s",
    )
    parser.add_argument(
        "--xml-buffer-size",
        type=int,
        default=8192,
        help="Socket receive buffer size in XML mode. Default: %(default)s",
    )
    return parser.parse_args()


def run() -> int:
    args = parse_args()
    csv_path = Path(args.csv).resolve()
    jsonl_path = Path(args.jsonl).resolve()
    debug_payload_path = Path(args.debug_payload).resolve() if args.debug_payload else None

    try:
        request_headers = parse_headers(args.header)
    except ValueError as exc:
        print(f"argument error: {exc}")
        return 2

    if args.cookie:
        request_headers["Cookie"] = args.cookie
    if args.basic_auth:
        try:
            request_headers["Authorization"] = build_basic_auth_header(args.basic_auth)
        except ValueError as exc:
            print(f"argument error: {exc}")
            return 2

    if args.discover_endpoints:
        probe_endpoints(
            base_url=args.url,
            timeout=args.timeout,
            insecure=args.insecure,
            headers=request_headers,
            limit=max(10, args.discover_limit),
        )
        return 0

    if args.mode == "xml":
        xml_host = args.xml_host.strip()
        if not xml_host:
            parsed = urllib.parse.urlparse(args.url)
            xml_host = parsed.hostname or ""
        if not xml_host:
            print("argument error: XML mode requires --xml-host or a host in --url")
            return 2
        if args.xml_port <= 0:
            print("argument error: XML mode requires --xml-port (configured in WBM XML channel)")
            return 2
        return run_xml_mode(
            host=xml_host,
            port=args.xml_port,
            timeout=args.timeout,
            reconnect_delay=max(args.xml_reconnect_delay, 0.2),
            buffer_size=max(args.xml_buffer_size, 512),
            csv_path=csv_path,
            jsonl_path=jsonl_path,
            debug_payload_path=debug_payload_path,
            log_all=args.log_all,
        )

    print("RFID logger started")
    print(f"URL      : {args.url}")
    print(f"Interval : {args.interval}s")
    print(f"CSV      : {csv_path}")
    print(f"JSONL    : {jsonl_path}")
    if request_headers:
        print("Headers  : configured")
    if debug_payload_path:
        print(f"DebugRaw : {debug_payload_path}")
    print("Press Ctrl+C to stop\n")

    last_hash: str | None = None
    error_streak = 0
    unchanged_count = 0
    anonymous_notice_printed = False

    while True:
        try:
            payload = fetch_payload(
                args.url,
                args.timeout,
                insecure=args.insecure,
                extra_headers=request_headers,
            )
            (
                last_hash,
                unchanged_count,
                anonymous_notice_printed,
            ) = process_payload_record(
                payload=payload,
                source_url=args.url,
                csv_path=csv_path,
                jsonl_path=jsonl_path,
                debug_payload_path=debug_payload_path,
                log_all=args.log_all,
                last_hash=last_hash,
                unchanged_count=unchanged_count,
                anonymous_notice_printed=anonymous_notice_printed,
            )

            error_streak = 0
        except KeyboardInterrupt:
            print("\nStopped by user.")
            return 0
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error_streak += 1
            print(f"[{now_iso()}] warning: read failed ({type(exc).__name__}: {exc})")
            # Slightly larger wait after repeated failures, without becoming too slow.
            sleep_for = min(args.interval + error_streak * 0.5, 5.0)
            time.sleep(sleep_for)
            continue
        except Exception as exc:  # Defensive: keep logger alive in production.
            error_streak += 1
            print(f"[{now_iso()}] warning: unexpected error ({type(exc).__name__}: {exc})")

        try:
            time.sleep(max(args.interval, 0.1))
        except KeyboardInterrupt:
            print("\nStopped by user.")
            return 0


if __name__ == "__main__":
    sys.exit(run())
