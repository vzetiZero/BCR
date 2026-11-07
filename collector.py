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
# Chỉ những lệnh này được phép đi qua cầu nối tới extension. Không có lệnh đặt tiền
# tự động ngoài phạm vi người dùng đã bật trong app.
ALLOWED_COMMANDS = {
    "click_table",      # mở một bàn trong sảnh theo table_id/tên
    "back_to_lobby",    # đưa về danh sách bàn
    "open_lobby",       # bấm thẻ Evolution ở trang gốc
    "read_chips",       # đọc mệnh giá chip + mệnh giá đang chọn
    "place_chips",      # bấm chip mệnh giá và ô cược đúng số lần
    "ping",             # kiểm tra extension còn sống
}
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
        # Số dư thật đọc từ API profile qua extension (người dùng tự đăng nhập).
        self.balance = None
        self.balance_at = None
        self.balance_source = ""
        # Cầu nối lệnh app -> extension (chỉ lệnh trong ALLOWED_COMMANDS).
        self._lock_cmd = threading.Lock()
        self._pending = []          # lệnh đang chờ extension nhận
        self._results = {}          # id -> kết quả extension trả
        self._cmd_seq = 0
        self._ext_seen_at = 0.0

    # ---------- cầu nối lệnh app -> extension ----------
    def push_command(self, name, args=None, ttl=25.0):
        """Xếp một lệnh cho extension. Trả id hoặc None nếu tên lệnh không được phép."""
        if name not in ALLOWED_COMMANDS:
            return None
        now = time.time()
        with self._lock_cmd:
            self._cmd_seq += 1
            cmd_id = "c%d" % self._cmd_seq
            self._pending.append({"id": cmd_id, "name": name, "args": args or {},
                                  "expires": now + ttl})
            del self._pending[:-8]
            return cmd_id

    def poll_command(self, now=None):
        """Extension gọi định kỳ để nhận lệnh kế tiếp; dọn lệnh hết hạn."""
        now = now if now is not None else time.time()
        with self._lock_cmd:
            self._ext_seen_at = now
            self._pending = [c for c in self._pending if c["expires"] > now]
            return self._pending.pop(0) if self._pending else None

    def post_result(self, cmd_id, result, now=None):
        """Extension trả kết quả một lệnh."""
        now = now if now is not None else time.time()
        with self._lock_cmd:
            self._ext_seen_at = now
            if cmd_id:
                self._results[cmd_id] = {"result": result, "ts": now}
                if len(self._results) > 40:
                    for stale in sorted(self._results, key=lambda k: self._results[k]["ts"])[:-20]:
                        self._results.pop(stale, None)

    def get_result(self, cmd_id):
        with self._lock_cmd:
            entry = self._results.pop(cmd_id, None)
        return entry["result"] if entry else None

    def extension_alive(self, now=None, window=20.0):
        now = now if now is not None else time.time()
        with self._lock_cmd:
            return bool(self._ext_seen_at) and (now - self._ext_seen_at) <= window

    def extension_age(self, now=None):
        now = now if now is not None else time.time()
        with self._lock_cmd:
            return None if not self._ext_seen_at else max(0.0, now - self._ext_seen_at)

    def set_balance(self, payload, now=None):
        """Lưu số dư thật do extension gửi. Trả về bản ghi đã chuẩn hoá hoặc None.

        Extension gửi ``{"balance": 3010.73, "currency": "VND2", "player": "****2093",
        "name": "nguyễn mạnh hưng", "vip": "VIP4"}`` (số dư thô của ví + tên hiển thị).
        App tự nhân đơn vị hiển thị khi cần, không nhân ở đây.
        """
        now = now if now is not None else time.time()
        if not isinstance(payload, dict):
            self.dropped += 1
            return None
        try:
            raw = float(payload.get("balance"))
        except (TypeError, ValueError):
            self.dropped += 1
            return None
        if raw < 0 or raw != raw or raw in (float("inf"), float("-inf")):
            self.dropped += 1
            return None
        record = {
            "balance": raw,
            "currency": clean_label(payload.get("currency"), 12),
            "player": clean_label(payload.get("player"), 32),
            "name": clean_label(payload.get("name"), 48),
            "vip": clean_label(payload.get("vip"), 12),
            "source": clean_label(payload.get("source"), 16) or "profile-api",
            "ts": now,
        }
        with self.lock:
            self.balance = record
            self.balance_at = now
            self.balance_source = record["source"]
        return record

    def balance_snapshot(self, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            record = dict(self.balance) if self.balance else None
        if record is not None:
            record["age"] = max(0.0, now - record["ts"])
        return record

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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept")
        # Chrome chặn trang HTTPS công khai gọi xuống 127.0.0.1 (Private Network Access)
        # trừ khi máy chủ nội bộ đồng ý rõ ràng. Thiếu header này thì request từ
        # extension trên trang thật luôn thất bại với "Không gửi được app".
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        if self.path.startswith("/tables"):
            self._send(200, self.store.snapshot())
        elif self.path.startswith("/balance"):
            self._send(200, {"ok": True, "balance": self.store.balance_snapshot()})
        elif self.path.startswith("/command"):
            command = self.store.poll_command()
            self._send(200, {"ok": True, "command": command,
                             "alive": self.store.extension_alive()})
        elif self.path.startswith("/ping"):
            self._send(200, {"ok": True, "app": "BCR-3-3", "port": self.store.port,
                             "extension": self.store.extension_alive()})
        else:
            self._send(404, {"error": "unknown path"})

    def do_POST(self):
        if self.path.startswith("/result"):
            self._read_json(self._handle_result)
            return
        if self.path.startswith("/balance"):
            self._read_json(self._handle_balance)
            return
        if not self.path.startswith("/report"):
            self._send(404, {"error": "unknown path"})
            return
        self._read_json(self._handle_report)

    def _read_json(self, handler):
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
        handler(payload)

    def _handle_balance(self, payload):
        record = self.store.set_balance(payload)
        if record is None:
            self._send(400, {"error": "bad balance"})
            return
        self._send(200, {"ok": True, "balance": record["balance"]})

    def _handle_result(self, payload):
        if not isinstance(payload, dict):
            self._send(400, {"error": "bad result"})
            return
        self.store.post_result(payload.get("id"), payload.get("result"))
        self._send(200, {"ok": True})

    def _handle_report(self, payload):
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
