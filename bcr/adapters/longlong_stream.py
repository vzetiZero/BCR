# -*- coding: utf-8 -*-
"""Luồng WebSocket của bàn LONG-LONG (game ``twocoins``) qua CDP — nguồn dữ liệu chuẩn.

Sàn đẩy trạng thái ván bằng message ``{"type": "twocoins.state", "args": {...}}``; mỗi
ván có ``args.gameId`` riêng nên biết CHÍNH XÁC đang ở ván nào, còn mở cược hay không,
còn bao nhiêu giây, và kết quả là gì — không phải đoán theo class của ô thắng như cách
đọc DOM (nguồn sinh ra "ván ma" và báo thắng oan).

Chuỗi phase của một ván (đã bắt thật trên bàn DragonDragon0001):

    BetsOpen (kèm timeRemaining ~15000 ms)
      -> BetsClosedAnnounced -> BetsClosed
      -> BowlOneShook -> BowlTwoShook
      -> CoinOneReveal -> CoinTwoReveal -> CoinsRevealed
      -> GameResolved (kèm coins: ["Gold","Red"] …)

Ngoài ra còn ``twocoins.coinStatistics`` (lastResults = lịch sử kết quả) và
``phase.previousChips`` (chip của mình ở ván trước, để đối chiếu tiền đã vào bàn).

Module này chỉ ĐỌC frame của trang; không gửi lệnh cược, không sửa gì trên bàn.
"""

import json
import threading
import time

from bcr.adapters import browser as BR

OPEN_PHASE = "BetsOpen"
RESOLVED_PHASE = "GameResolved"
PHASES = ("BetsOpen", "BetsClosedAnnounced", "BetsClosed", "BowlOneShook", "BowlTwoShook",
          "CoinOneReveal", "CoinTwoReveal", "CoinsRevealed", "GameResolved")

# coins -> bộ kết quả của app (V = vàng/Gold, D = đỏ/Red).
COIN_RAW = {
    ("Gold", "Gold"): "VV",
    ("Red", "Red"): "DD",
    ("Gold", "Red"): "VD",
    ("Red", "Gold"): "DV",
}

RECONNECT_DELAY = 2.0
SILENT_LIMIT = 8.0        # trang im lặng quá lâu khi chưa nghe được gì -> thử tab khác
STREAM_TYPES = ("twocoins.state", "twocoins.coinStatistics")


def coins_to_raw(coins):
    """["Gold","Red"] -> 'VD'; lạ thì None (không đoán)."""
    if not isinstance(coins, (list, tuple)) or len(coins) != 2:
        return None
    return COIN_RAW.get((str(coins[0]), str(coins[1])))


def parse_message(obj, received_at=None):
    """Đổi một message WebSocket thành ảnh chụp trạng thái (None nếu không liên quan).

    Trả dict với các khoá: type, round, phase, time_remaining, coins, result,
    previous_chips, history. Chỉ đọc đúng định dạng đã bắt được của sàn.
    """
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    if kind not in STREAM_TYPES:
        return None
    args = obj.get("args")
    if not isinstance(args, dict):
        return None
    if kind == "twocoins.coinStatistics":
        last = args.get("lastResults")
        history = []
        if isinstance(last, list):
            for item in last:
                raw = coins_to_raw((item or {}).get("coins"))
                if raw:
                    history.append(raw)
        return {"type": kind, "history": history}
    phase = args.get("phase") if isinstance(args.get("phase"), dict) else {}
    name = str(phase.get("type") or "")
    if name not in PHASES:
        return None
    coins = phase.get("coins")
    snap = {
        "type": kind,
        "round": str(args.get("gameId") or "") or None,
        "number": args.get("number"),
        "phase": name,
        "coins": list(coins) if isinstance(coins, list) else None,
        "result": coins_to_raw(coins),
        "previous_chips": phase.get("previousChips") if isinstance(
            phase.get("previousChips"), dict) else None,
        "history": phase.get("history") if isinstance(phase.get("history"), list) else None,
    }
    remaining = phase.get("timeRemaining")
    if isinstance(remaining, (int, float)) and name == OPEN_PHASE:
        snap["time_remaining"] = int(remaining)
        snap["received_at"] = time.monotonic() if received_at is None else float(received_at)
    return snap


class LongLongStream:
    """Nghe WebSocket của trang game và giữ ảnh chụp trạng thái mới nhất (chỉ đọc).

    Chạy ở thread riêng vì message chỉ đến theo nhịp phase (không phải lúc nào cũng có);
    đọc kiểu hỏi-đáp từng nhịp sẽ bỏ lỡ mất ``GameResolved``.
    """

    def __init__(self, session=None):
        self.session = session or BR.BrowserSession()
        self.last_error = ""
        self._lock = threading.Lock()
        self._snapshot = {}
        self._stop = threading.Event()
        self._thread = None
        self._preferred = None      # webSocketDebuggerUrl của tab đang nghe được dữ liệu

    # ---------------------------------------------------------------- vòng đời
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="longlong-stream", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ đọc
    def snapshot(self):
        """Ảnh chụp mới nhất (kèm thời gian còn lại được tính lại theo đồng hồ hiện tại)."""
        with self._lock:
            snap = dict(self._snapshot)
        if not snap:
            return {"ok": False, "error": self.last_error or "Chưa nhận được dữ liệu từ sàn."}
        snap["ok"] = True
        remaining = snap.get("time_remaining")
        received = snap.get("received_at")
        if snap.get("phase") == OPEN_PHASE and isinstance(remaining, int) \
                and isinstance(received, (int, float)):
            left = remaining - (time.monotonic() - received) * 1000.0
            snap["remaining_ms"] = int(max(0, left))
        else:
            snap["remaining_ms"] = None
        last = snap.get("last_frame_at")
        snap["age_ms"] = (int((time.monotonic() - last) * 1000) if isinstance(last, (int, float))
                          else None)
        snap["error"] = self.last_error
        return snap

    # --------------------------------------------------------------- nội bộ
    def _update(self, snap):
        with self._lock:
            if snap["type"] == "twocoins.coinStatistics":
                self._snapshot["history"] = snap.get("history") or []
                self._snapshot["last_frame_at"] = time.monotonic()
                return
            merged = dict(self._snapshot)
            merged.update({k: v for k, v in snap.items() if v is not None})
            merged.setdefault("history", [])
            merged["last_frame_at"] = time.monotonic()
            # Kết quả chỉ giữ khi ván đó thực sự đã có kết quả.
            if snap["phase"] == RESOLVED_PHASE and snap.get("result"):
                merged["result"] = snap["result"]
                merged["result_round"] = snap["round"]
                merged["result_coins"] = snap.get("coins")
            self._snapshot = merged

    def _candidates(self):
        """Các trang có thể đang chạy bàn LONG-LONG (mỗi sàn một tab là chuyện thường).

        Sàn nào cũng nhúng cùng một vỏ game của Evolution nên định dạng message giống
        nhau; chỉ cần chọn đúng TAB đang có bàn. Trang nghe được dữ liệu lần trước được
        ưu tiên trước.
        """
        pages = [item for item in BR.BrowserSession._cdp_json("list")
                 if item.get("type") == "page" and item.get("webSocketDebuggerUrl")]
        preferred = [page for page in pages if page.get("webSocketDebuggerUrl") == self._preferred]
        evo = [page for page in pages if "evo" in (page.get("url") or "")
               and page not in preferred]
        evo.sort(key=lambda page: 0 if ("twocoins" in (page.get("url") or "")
                                        or "table_id=" in (page.get("url") or "")) else 1)
        rest = [page for page in pages if page not in preferred and page not in evo]
        return preferred + evo + rest

    @staticmethod
    def _reason(exc):
        closed = BR.cdp_port_closed(exc)
        return (BR.CDP_CLOSED_NOTE if closed else
                "Không nghe được WebSocket bàn (%s)." % type(exc).__name__)

    def _run(self):
        while not self._stop.is_set():
            heard_on_any = False
            try:
                candidates = self._candidates()
            except Exception as exc:
                # Chrome đóng/cổng CDP tắt: KHÔNG được để thread chết, chỉ báo lỗi rồi thử lại.
                self.last_error = self._reason(exc)
                self._stop.wait(RECONNECT_DELAY)
                continue
            if not candidates:
                self.last_error = "Chưa thấy tab game LONG-LONG trong Chrome."
            for page in candidates:
                if self._stop.is_set():
                    break
                try:
                    heard = self._connect_once(page)
                except Exception as exc:
                    self.last_error = self._reason(exc)
                    heard = False
                if heard:
                    heard_on_any = True
                    self._preferred = page.get("webSocketDebuggerUrl")
                    break
                self._stop.wait(0.5)
            if not heard_on_any:
                self._stop.wait(RECONNECT_DELAY)

    def _connect_once(self, page):
        """Nghe một trang; trả True nếu trang đó có phát message của bàn (twocoins.*)."""
        import websocket

        ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True,
                                         timeout=BR.SCRIPT_TIMEOUT, redirect_limit=0,
                                         http_no_proxy=["127.0.0.1", "localhost", "::1"])
        heard = False
        try:
            ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
            ws.settimeout(1.0)
            started = time.monotonic()
            while not self._stop.is_set():
                try:
                    message = json.loads(ws.recv())
                except Exception:
                    # Trang im lặng quá lâu -> thử tab khác (có thể là sàn khác).
                    if not heard and time.monotonic() - started > SILENT_LIMIT:
                        return False
                    continue
                if message.get("method") != "Network.webSocketFrameReceived":
                    continue
                payload = ((message.get("params") or {}).get("response") or {}).get("payloadData")
                if not payload or "twocoins." not in payload:
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                snap = parse_message(obj)
                if snap:
                    heard = True
                    self.last_error = ""
                    self._update(snap)
            return heard
        finally:
            try:
                ws.close()
            except Exception:
                pass
