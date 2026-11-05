# -*- coding: utf-8 -*-
"""Server localhost nhận cầu từ extension Chrome (chỉ bind 127.0.0.1, chỉ nhận dữ liệu bảng).

Payload mà extension gửi tới POST /report:
    {"page":   {"url": "...", "table_id": "LuckyBaccaratB04"},
     "tables": [{"label": "B04", "letters": "BBPBT...", "source": "text"}]}

letters: B = Banker, P = Player, T = Hoa, theo thứ tự tay đã đi (tay cũ -> tay mới).
Mọi ký tự ngoài BPT bị loại, tối đa 400 tay / bàn.

Nguồn cầu chỉ đi từ máy này sang máy này; không có endpoint bên ngoài.
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8777
MAX_BODY = 200_000
MAX_LETTERS = 400
MAX_TABLES_PER_REPORT = 60
MIN_LETTERS = 4
MAX_ENTRIES = 120
LOG_BYTES = 1_000_000
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "live_reports.jsonl")


def coord(raw):
    """To độ góc trái của bảng cầu trong frame, dùng làm dấu vết để tìm lại bàn trên lobby."""
    try:
        return max(0, min(99999, int(float(raw))))
    except (TypeError, ValueError):
        return None


def continuous(prev, letters):
    """True nếu lần đọc này ăn khớp lần trước: cầu dài ra, đọc lại như cũ,
    hoặc cửa sổ cầu trên site trượt (vài tay cũ nhất bị xoá khi bàn đầy)."""
    if not prev:
        return True
    if letters.startswith(prev) or prev.startswith(letters) or letters.endswith(prev):
        return True
    for drop in range(1, 4):
        if len(prev) - drop >= 6 and letters.startswith(prev[drop:]):
            return True
    return False


def clean_letters(raw):
    if not isinstance(raw, str):
        return ""
    return "".join(ch for ch in raw.upper() if ch in "BPT")[:MAX_LETTERS]


def clean_label(raw, limit=34):
    if not isinstance(raw, str):
        return ""
    return re.sub(r"\s+", " ", raw).strip()[:limit]


def table_code(label, table_id):
    """Đoán mã bàn (đoạn số cuối) để hiển thị gọn trong bảng xếp hạng."""
    for src in (table_id or "", label or ""):
        found = re.findall(r"(\d{1,3})", src)
        if found:
            return found[-1].lstrip("0") or "0"
    return (clean_label(label, 8) or "?").split(" ")[0]


def name_family(label):
    """'Lucky Baccarat B04' và 'LuckyBaccaratB04' quy về cùng một họ để gộp hai cách gọi."""
    return re.sub(r"[^a-z]", "", (label or "").lower()) or clean_label(label, 20).lower()


class TableStore:
    """Bảng tra keyed theo label bàn, cập nhật bởi thread HTTP, đọc bởi thread Tkinter."""

    def __init__(self, log_path=LOG_FILE):
        self.lock = threading.Lock()
        self.tables = {}
        self.events = []
        self.log_path = log_path
        self.started_at = None
        self.port = None
        self.received = 0
        self.dropped = 0

    def _log(self, payload):
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > LOG_BYTES:
                with open(self.log_path, "w", encoding="utf-8"):
                    pass
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def merge(self, report, now=None):
        """Gộp một báo cáo. Trả về số bàn được cập nhật."""
        now = now if now is not None else time.time()
        if not isinstance(report, dict):
            self.dropped += 1
            return 0
        page = report.get("page") if isinstance(report.get("page"), dict) else {}
        page_url = clean_label(page.get("url"), 160)
        tid = clean_label(page.get("table_id"), 40)
        rows = report.get("tables")
        if not isinstance(rows, list):
            self.dropped += 1
            return 0

        self.received += 1
        self._log({"ts": now, "page": page_url, "table_id": tid,
                   "tables": [{k: v for k, v in r.items() if k in ("label", "letters", "source", "x", "y")}
                              for r in rows if isinstance(r, dict)][:MAX_TABLES_PER_REPORT]})

        touched = 0
        with self.lock:
            for item in rows[:MAX_TABLES_PER_REPORT]:
                if not isinstance(item, dict):
                    continue
                letters = clean_letters(item.get("letters"))
                if len(letters) < MIN_LETTERS:
                    continue
                source = clean_label(item.get("source"), 12) or "?"
                label = clean_label(item.get("label")) or tid or "ban khong ten"
                x, y = coord(item.get("x")), coord(item.get("y"))
                pos = "%d,%d" % (x, y) if x is not None and y is not None else ""
                code = table_code(label, tid)
                # Họ tên + số bàn: gộp "LuckyBaccaratB04" và "Lucky Baccarat B04",
                # nhưng không gộp "Lucky Baccarat B07" với "Baccarat Dragon 7".
                key = "%s#%s" % (name_family(label), code)
                entry = self.tables.get(key)
                if entry is None:
                    if len(self.tables) >= MAX_ENTRIES:
                        victim = min(self.tables, key=lambda k: self.tables[k]["ts"])
                        self.tables.pop(victim, None)
                    entry = {"label": label, "code": code, "page_table_id": tid,
                             "letters": "", "source": "", "ts": 0, "reports": 0,
                             "conflict": False, "resyncs": 0, "pos": "", "page_url": page_url}
                    self.tables[key] = entry
                prev = entry["letters"]
                if continuous(prev, letters):
                    # Cầu dài hơn = mới hơn; cầu bằng nhau thì lấy lần đọc mới nhất.
                    if len(letters) >= len(prev):
                        entry["letters"] = letters
                        entry["source"] = source
                        entry["pos"] = pos or entry["pos"]
                else:
                    # Không nối được vào cầu cũ: hoặc đọc lệch, hoặc site vừa sang ván mới.
                    if len(prev) - len(letters) > 3:
                        entry["resyncs"] = 0     # ván mới: cầu ngắn lại hẳn, tính lại độ tin cậy
                        entry["conflict"] = False
                    else:
                        entry["resyncs"] += 1
                        if len(letters) == len(prev):
                            entry["conflict"] = True
                    entry["letters"] = letters
                    entry["source"] = source
                    entry["pos"] = pos or entry["pos"]
                entry["ts"] = now
                entry["page_url"] = page_url or entry["page_url"]
                entry["page_table_id"] = tid or entry["page_table_id"]
                entry["reports"] += 1
                touched += 1
            self.events.append({"ts": now, "tables": touched, "page": page_url or tid})
            del self.events[:-12]
        return touched

    def snapshot(self, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            rows = []
            for entry in self.tables.values():
                rows.append({
                    "label": entry["label"], "code": entry["code"],
                    "letters": entry["letters"], "n": len(entry["letters"]),
                    "source": entry["source"], "ts": entry["ts"],
                    "age": max(0.0, now - entry["ts"]), "reports": entry["reports"],
                    "conflict": entry["conflict"], "resyncs": entry["resyncs"],
                    "pos": entry.get("pos", ""),
                    "page_table_id": entry["page_table_id"],
                })
        rows.sort(key=lambda r: (-r["n"], r["label"].lower()))
        return {"rows": rows, "events": list(self.events), "received": self.received,
                "dropped": self.dropped, "port": self.port, "started_at": self.started_at}


class Handler(BaseHTTPRequestHandler):
    store = None
    server_version = "BCRCollector/1"

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        if self.path.startswith("/tables"):
            self._send(200, self.store.snapshot())
        elif self.path.startswith("/ping"):
            self._send(200, {"ok": True, "app": "BCR-3-3", "port": self.store.port})
        else:
            self._send(404, {"error": "unknown path"})

    def do_POST(self):
        if not self.path.startswith("/report"):
            self._send(404, {"error": "unknown path"})
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
        except ValueError:
            self._send(400, {"error": "bad content-length"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "json parse failed"})
            return
        touched = self.store.merge(payload)
        self._send(200, {"ok": True, "updated": touched, "known": len(self.store.tables)})


def start_server(store, port=DEFAULT_PORT):
    """Khởi động server trên 127.0.0.1. Trả về (httpd, thread). Port bận -> OSError."""
    Handler.store = store
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="BCRCollector", daemon=True)
    thread.start()
    store.port = port
    store.started_at = time.time()
    return httpd, thread
