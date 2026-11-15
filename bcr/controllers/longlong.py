# -*- coding: utf-8 -*-
"""Tầng Controller · LONG-LONG: đọc bàn, chốt ván, đặt chip, cấu hình phiên.

Chỉ điều phối Model (``bcr.models.longlong``) và Adapter (``bcr.adapters``);
không dùng tkinter, không biết gì về Electron. Tầng transport
(``bcr.service.engine``) gọi ``dispatch`` để phục vụ giao diện.

Mỗi trò chạy một tiến trình riêng nên Chrome, cổng CDP, cấu hình và phiên của
LONG-LONG tách hoàn toàn khỏi Baccarat.
"""

import collections
import json
import pathlib
import threading
import time

from bcr.adapters import browser as BR
from bcr.adapters import longlong_stream
from bcr.adapters import longlong_table
from bcr.controllers import longlong_auto
from bcr.models import longlong


def _fn(value):
    """Số kiểu Việt Nam cho log: 10.000 (không phần thập phân)."""
    try:
        return "{:,.0f}".format(float(value)).replace(",", ".")
    except (TypeError, ValueError):
        return str(value)


# Nhịp đọc nhanh khi đang chờ bấm chip: cửa sổ cược của sàn ngắn nên đọc mỗi 2 giây là
# quá trễ (ván đóng trước khi kịp bấm). Chỉ dùng khi thật sự sắp đặt tiền.
PLACE_POLL_SECONDS = 0.7


class LongLongController:
    """Đọc bàn LONG-LONG, chốt kết quả và đặt chip theo thang đã cấu hình."""

    def __init__(self, state_path, emit, interval=2.0):
        self.state_path = pathlib.Path(state_path)
        self.emit = emit
        self.interval = float(interval)
        self.config = self._load_config()
        self.sim = longlong.LongLongSimulation(self.config["ladder"],
                                               capital=self.config["capital"] * 1000.0)
        self.tracker = longlong_auto.HandTracker(min_gap=self.config.get("min_gap", 15.0),
                                                 min_hand_gap=self.config.get(
                                                     "min_hand_gap",
                                                     longlong_auto.DEFAULT_MIN_HAND_GAP),
                                                 confirm=self.config.get(
                                                     "confirm_reads",
                                                     longlong_auto.DEFAULT_CONFIRM))
        self.table = longlong_table.LongLongTable()
        self.browser = BR.BrowserSession()
        self.stream = longlong_stream.LongLongStream(session=self.browser)
        self._ws_settled_round = None   # ván cuối cùng đã tất toán từ luồng WebSocket
        self.logs = collections.deque(maxlen=200)
        self.tool_running = False
        self._stop = threading.Event()
        self._thread = None
        self._busy = threading.Lock()
        self.last_state = {}
        # Nhịp nền: đẩy state + phát hiện Chrome bị đóng kể cả khi tool đang dừng.
        self.offline_noted = False
        self._last_place_fail = None    # (ván, lỗi) đã báo — chống spam nhật ký
        self._chip_gone_at = None       # mốc bắt đầu thấy tiền đặt biến mất khỏi bàn
        self._heartbeat = None
        self._heartbeat_stop = None
        self.log("Engine LONG-LONG sẵn sàng.")

    # ------------------------------------------------------------- vòng nền
    def start(self):
        """Bật nhịp nền (engine gọi một lần sau khi tạo controller)."""
        if self._heartbeat is not None:
            return
        self._heartbeat_stop = threading.Event()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True,
                                           name="ll-heartbeat")
        self._heartbeat.start()

    def stop_heartbeat(self):
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.is_set():
            try:
                self._heartbeat_once()
            except Exception as exc:
                self.log("Lỗi vòng nền: %s" % exc, "error")
            self._heartbeat_stop.wait(self.interval)

    def _heartbeat_once(self):
        """Cập nhật trạng thái Chrome và đẩy state để UI luôn thấy nút “Mở Chrome”."""
        connected = bool(getattr(self.browser, "connected", False))
        if connected:
            if self.offline_noted:
                self.offline_noted = False
                self.log("Chrome đã kết nối lại.")
        elif not self.offline_noted:
            self.offline_noted = True
            self.last_state = {}
            if self.tool_running:
                self.tool_running = False
                self._stop.set()
            self.log(getattr(self.browser, "note", "")
                     or "Chrome đã đóng · bấm “Mở Chrome” để chạy lại.", "error")
        self._push_state()

    # ------------------------------------------------------------- cấu hình
    def _load_config(self):
        defaults = {
            "ladder": list(longlong.DEFAULT_LADDER),
            "base": longlong.DEFAULT_BASE,
            "capital": longlong.DEFAULT_CAPITAL,       # đơn vị sảnh (10.000 = 10 triệu)
            # Đơn vị tiền trên KHAY CHIP của sàn: "vn2" (chip 40 = 40.000đ) hay "vnd"
            # (chip 10.000 = 10.000đ). Xem bcr.models.longlong.UNIT_*.
            "unit_mode": longlong.DEFAULT_UNIT_MODE,
            "place_real": False,                       # đặt chip thật hay chỉ mô phỏng
            "auto_result": True,                       # tự chốt kết quả ván
            "min_gap": 15.0,
            # Chống "ván ma": phải thấy cùng một ô thắng 2 lần đọc liên tiếp và hai ván
            # phải cách nhau ≥ 6 giây, nếu không sổ vốn sẽ tính tiền cho tay chưa mở.
            "confirm_reads": longlong_auto.DEFAULT_CONFIRM,
            "min_hand_gap": longlong_auto.DEFAULT_MIN_HAND_GAP,
            # Bao lâu không thấy tiền đặt trên bàn (mà chưa có kết quả) thì coi như sàn
            # đã huỷ/hoàn tiền -> bỏ ván, không tính tiền. Xem _watch_chips().
            "chip_watch_seconds": longlong_auto.DEFAULT_CHIP_WATCH,
            # Luồng WebSocket của sàn: biết đúng ván / cửa sổ cược / kết quả.
            "use_ws": True,
            "min_bet_ms": 3000,      # còn ít hơn mức này thì không bấm (tránh vào muộn)
            "start_url": BR.START_URL,                 # link Chrome mở khi bấm “Mở Chrome”
            # Cửa sổ Chrome: cùng bộ tham số với tab Baccarat.
            "chrome_window": {"width": 1280, "height": 800, "percent": 100,
                              "fullscreen": False},
        }
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            saved = None
        if isinstance(saved, dict):
            for key in defaults:
                if key in saved:
                    defaults[key] = saved[key]
        return self._normalize_config(defaults)

    @staticmethod
    def _normalize_config(config):
        """Kẹp link mở đầu và cửa sổ Chrome về giá trị hợp lệ (không tin file state)."""
        url = config.get("start_url")
        config["start_url"] = (url.strip() if isinstance(url, str)
                               and BR.valid_site_url(url.strip()) else BR.START_URL)
        window = config.get("chrome_window")
        window = window if isinstance(window, dict) else {}
        config["chrome_window"] = BR.normalize_window(
            (window.get("width"), window.get("height")),
            window.get("percent"), window.get("fullscreen"))
        config["unit_mode"] = longlong.normalize_unit_mode(config.get("unit_mode"))
        try:
            config["confirm_reads"] = max(1, min(int(config.get("confirm_reads")
                                                    or longlong_auto.DEFAULT_CONFIRM), 5))
        except (TypeError, ValueError):
            config["confirm_reads"] = longlong_auto.DEFAULT_CONFIRM
        try:
            config["min_hand_gap"] = max(0.0, float(config.get("min_hand_gap")
                                                    if config.get("min_hand_gap") is not None
                                                    else longlong_auto.DEFAULT_MIN_HAND_GAP))
        except (TypeError, ValueError):
            config["min_hand_gap"] = longlong_auto.DEFAULT_MIN_HAND_GAP
        try:
            config["chip_watch_seconds"] = max(0.0, min(float(config.get("chip_watch_seconds")
                                                             if config.get("chip_watch_seconds")
                                                             is not None
                                                             else longlong_auto.DEFAULT_CHIP_WATCH),
                                                       30.0))
        except (TypeError, ValueError):
            config["chip_watch_seconds"] = longlong_auto.DEFAULT_CHIP_WATCH
        config["use_ws"] = bool(config.get("use_ws", True))
        try:
            config["min_bet_ms"] = max(0, min(int(config.get("min_bet_ms") or 3000), 60000))
        except (TypeError, ValueError):
            config["min_bet_ms"] = 3000
        return config

    @property
    def unit_mode(self):
        """Chế độ đơn vị tiền đang chọn (vn2 / vnd)."""
        return longlong.normalize_unit_mode(self.config.get("unit_mode"))

    @property
    def window(self):
        return self.config["chrome_window"]

    @staticmethod
    def _window_pos(window):
        return None if window.get("fullscreen") else (40, 40)

    @staticmethod
    def _window_text(window):
        if window.get("fullscreen"):
            return "Chrome toàn màn hình · tỉ lệ %d%%" % window["percent"]
        return "Chrome %d × %d · tỉ lệ %d%%" % (window["width"], window["height"],
                                                window["percent"])

    def _save_config(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
            return True
        except Exception as exc:
            self.log("Không lưu được cấu hình (%s)." % type(exc).__name__, "error")
            return False

    def log(self, text, level="info"):
        entry = {"at": time.strftime("%H:%M:%S"), "level": level, "text": str(text)}
        self.logs.append(entry)
        self.emit.event("log", entry)

    # --------------------------------------------------------------- trạng thái
    def snapshot(self):
        """Toàn bộ trạng thái để UI vẽ (không đọc CDP — dùng last_state)."""
        strategy = self.sim.strategy
        plan = strategy.plan()
        connected = bool(getattr(self.browser, "connected", False))
        # Chrome đã đóng thì đừng hiện số liệu cũ của bàn như đang đọc được.
        state = (self.last_state or {}) if connected else {}
        return {
            "game": "longlong",
            "chrome_connected": connected,
            "chrome_note": getattr(self.browser, "note", ""),
            "tool_running": self.tool_running,
            "config": dict(self.config),
            "balance_vn2": state.get("balance_vn2"),
            "total_bet_vn2": state.get("total_bet_vn2"),
            "status": state.get("status"),
            "winner": state.get("winner"),
            "winner_raw": state.get("winner_raw"),
            "doors": state.get("doors") or {},
            "read_at": state.get("read_at"),
            "read_error": None if state.get("ok") else state.get("error"),
            "step": strategy.step,
            "step_stake": strategy.ladder[min(strategy.step, len(strategy.ladder) - 1)],
            "ladder": list(strategy.ladder),
            # Đơn vị tiền của sàn: UI dùng để in đúng nhãn và quy ra VNĐ.
            "unit_mode": self.unit_mode,
            "site_scale": longlong.site_scale(self.unit_mode),
            "plan_amount": longlong.to_site_amount(plan.get("stake") or 0, self.unit_mode),
            "pending": strategy.pending,
            "stopped": strategy.stopped,
            "plan": plan,
            "hands_seen": self.tracker.hands,
            # Luồng WebSocket của sàn (nguồn chuẩn): ván nào, phase nào, còn bao lâu.
            "ws": self._ws_state(),
            "log": [dict(entry) for entry in strategy.log[-20:]],
            "profit_units": self.sim.profit / 1000.0,
            # Số liệu cho tab Thống kê: tỉ lệ thắng/thua, chuỗi thua dài nhất, đường lãi/lỗ.
            "stats": longlong.summarize_history(self.sim.log),
        }

    def _push_state(self):
        state = self.snapshot()
        self.emit.event("state", state)
        return state

    def _ws_state(self):
        """Ảnh chụp luồng WebSocket để UI hiện phase / thời gian còn lại / ván."""
        try:
            snap = self.stream.snapshot()
        except Exception:
            return {"ok": False}
        return {
            "ok": bool(snap.get("ok")),
            "phase": snap.get("phase"),
            "round": snap.get("round"),
            "number": snap.get("number"),
            "remaining_ms": snap.get("remaining_ms"),
            "result": snap.get("result"),
            "result_round": snap.get("result_round"),
            "age_ms": snap.get("age_ms"),
            "error": snap.get("error") if not snap.get("ok") else None,
        }

    def _push_ok(self):
        """Đẩy state mới và trả về kèm ok=True cho các method làm thay đổi dữ liệu."""
        state = self._push_state()
        state["ok"] = True
        return state

    # ------------------------------------------------------------------ Chrome
    def open_chrome(self):
        if getattr(self.browser, "connected", False):
            return {"ok": True, "note": "Chrome đã kết nối."}
        window = self.window
        pos = self._window_pos(window)
        url = self.config["start_url"]
        self.log("Đang mở Chrome cho LONG-LONG · %s · profile riêng, cổng %d…"
                 % (self._window_text(window), BR.CDP_PORT))

        def work():
            try:
                self.browser.start(url=url, pos=pos, cdp=True, window=window)
                if getattr(self.browser, "connected", False):
                    try:
                        self.browser.resize_window(window, pos=pos)
                    except Exception:
                        pass
                    self.log("Chrome đã mở. Hãy đăng nhập và vào bàn LONG-LONG.")
                else:
                    self.log(self.browser.note or "Mở Chrome chưa thành công.", "error")
            except Exception as exc:
                self.log("Mở Chrome lỗi: %s" % exc, "error")
            finally:
                self._push_state()

        threading.Thread(target=work, daemon=True, name="ll-chrome").start()
        return {"ok": True, "note": "Đang mở Chrome…"}

    def apply_window(self, params):
        """Đổi kích thước/tỉ lệ/toàn màn hình của Chrome (lưu lại cho lần mở sau)."""
        params = params or {}
        window = BR.normalize_window(
            (params.get("width", self.window["width"]),
             params.get("height", self.window["height"])),
            params.get("percent", self.window["percent"]),
            params.get("fullscreen", self.window["fullscreen"]))
        self.config["chrome_window"] = window
        self._save_config()
        if not getattr(self.browser, "connected", False):
            note = "Đã lưu %s · áp dụng khi mở Chrome." % self._window_text(window)
            self.log(note)
            self._push_state()          # để panel cấu hình hiện đúng số vừa lưu
            return {"ok": True, "note": note}
        pos = self._window_pos(window)

        def work():
            try:
                ok, note = self.browser.resize_window(window, pos=pos)
                self.log(note if ok else "Không đổi được cửa sổ Chrome: %s" % note,
                         "info" if ok else "error")
            except Exception as exc:
                self.log("Không đổi được cửa sổ Chrome (%s)." % type(exc).__name__, "error")
            finally:
                self._push_state()

        threading.Thread(target=work, daemon=True, name="ll-window").start()
        return {"ok": True, "note": "Đang áp dụng %s…" % self._window_text(window)}

    def chrome_status(self):
        return {"connected": bool(getattr(self.browser, "connected", False)),
                "mode": getattr(self.browser, "mode", ""),
                "note": getattr(self.browser, "note", "")}

    def close_chrome(self):
        try:
            self.browser.stop()
        except Exception as exc:
            self.log("Đóng Chrome lỗi: %s" % exc, "error")
        return {"ok": True}

    # -------------------------------------------------------------- tool (vòng)
    def tool_start(self):
        if self.tool_running:
            return {"ok": True, "note": "Tool đang chạy."}
        if not getattr(self.browser, "connected", False):
            return {"ok": False, "error": "Chưa mở Chrome. Bấm “Mở Chrome” và vào bàn trước."}
        self.tool_running = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ll-tool")
        self._thread.start()
        self.stream.start()          # nghe luồng WebSocket của sàn (nguồn chuẩn)
        self.log("Chạy tool: %s." % ("ĐẶT CHIP THẬT" if self.config["place_real"]
                                     else "chế độ mô phỏng (không bấm chip)"))
        return {"ok": True}

    def tool_stop(self):
        self.tool_running = False
        self._stop.set()
        self.log("Đã dừng tool.")
        return {"ok": True}

    def _loop(self):
        while not self._stop.is_set():
            if not getattr(self.browser, "connected", False):
                self.tool_running = False
                self.last_state = {}
                self.stream.stop()
                self.log("Chrome đã đóng · đã dừng tool. Bấm “Mở Chrome” để chạy lại.", "error")
                break
            try:
                self.tick()
            except Exception as exc:
                self.log("Lỗi vòng đọc: %s" % exc, "error")
            self._stop.wait(self._next_wait())
        self.tool_running = False
        self.stream.stop()
        self._push_state()

    def _next_wait(self):
        """Nhịp chờ tới lần đọc sau.

        Bình thường đọc mỗi ``interval`` giây. Khi đang cần bấm chip mà chưa bấm được
        (cửa sổ cược đang mở dở) thì đọc nhanh hơn để không trễ ván.
        """
        try:
            waiting_to_place = (bool(self.config.get("place_real"))
                                and self.sim.strategy.pending is None
                                and self.sim.strategy.plan().get("action") == "bet")
        except Exception:
            waiting_to_place = False
        # Không đủ tiền thì đọc nhanh cũng vô ích -> giữ nhịp thường, khỏi quay CPU.
        low_balance = bool(self._last_place_fail) and self._last_place_fail[-1] == "low_balance"
        if waiting_to_place and not low_balance:
            return min(self.interval, PLACE_POLL_SECONDS)
        return self.interval

    def tick(self):
        """Một nhịp: đọc bàn + luồng sàn -> chốt ván -> (nếu được bật) đặt chip."""
        state = self.table.read()
        state.setdefault("read_at", time.time())
        self.last_state = state
        wsnap = self.stream.snapshot() if self.config.get("use_ws", True) else {"ok": False}
        if state.get("ok") and wsnap.get("ok"):
            # Luồng WebSocket là nguồn chuẩn: biết đúng ván, đúng kết quả.
            self._settle_from_stream(wsnap)
        elif state.get("ok") and self.config.get("auto_result", True):
            # Không có luồng: lùi về đọc class ô thắng như trước.
            raw = self.tracker.feed(state.get("winner"), state.get("winner_raw"),
                                    state.get("read_at"), state.get("result_sig"))
            if raw:
                self._on_hand(raw, hand=self.tracker.hands)
        if state.get("ok"):
            self._watch_chips(state, wsnap)
            self._try_place(state, wsnap)
        return self._push_state()

    def _settle_from_stream(self, wsnap):
        """Chốt ván khi luồng sàn báo ``GameResolved`` của một ván mới."""
        round_id = wsnap.get("round")
        result_round = wsnap.get("result_round")
        raw = wsnap.get("result")
        if not result_round or not raw:
            return
        if result_round == self._ws_settled_round:
            return
        self._ws_settled_round = result_round
        self.tracker.hands = max(self.tracker.hands, 0) + 1
        self._on_hand(raw, hand=self.tracker.hands, round_id=result_round)

    def _watch_chips(self, state, wsnap=None):
        """Chip đã bấm có còn nằm trên bàn không?

        Sàn có thể nhận cú bấm (tổng nhảy lên) rồi huỷ/hoàn tiền khi cửa sổ cược đã
        đóng. Nếu tiền của mình biến mất TRƯỚC khi có kết quả ván mới thì lệnh đó không
        được tính vào sổ — đây chính là chỗ sinh ra "báo THẮNG cho tay không có tiền".
        Có luồng WebSocket thì còn đối chiếu thêm ``previousChips`` của sàn.
        """
        strategy = self.sim.strategy
        pending = strategy.pending
        if not pending or not self.config.get("place_real", False):
            self._chip_gone_at = None
            return
        amount = float(pending.get("amount_site") or 0)
        total = state.get("total_bet_vn2")
        if amount <= 0 or total is None:
            return
        if total + 0.01 >= amount:
            self._chip_gone_at = None       # còn tiền trên bàn: yên tâm chờ kết quả
            return
        now = time.monotonic()
        if self._chip_gone_at is None:
            self._chip_gone_at = now
            return
        if now - self._chip_gone_at < float(self.config.get("chip_watch_seconds",
                                                           longlong_auto.DEFAULT_CHIP_WATCH)):
            return
        strategy.pending = None
        self._chip_gone_at = None
        self.log("Tiền đặt (%s) biến mất trước khi có kết quả — sàn đã huỷ/hoàn tiền: "
                 "bỏ ván, không tính." % _fn(amount), "error")

    # ------------------------------------------------------------------ một ván
    def _on_hand(self, raw, hand=None, round_id=None):
        """Nhận một ván đã chốt; chỉ tất toán lệnh nếu lệnh thuộc ĐÚNG ván này."""
        strategy = self.sim.strategy
        if hand is None:
            # Tay do người dùng nhập tay: đánh số tiếp theo bộ đếm ván để lệnh vẫn khớp.
            self.tracker.hands += 1
            hand = self.tracker.hands
        hand = int(hand)
        pending = strategy.pending
        # Lệnh đặt cho ván khác (bấm trễ / lệch nhịp đọc): bỏ, không cộng trừ tiền —
        # đây chính là lỗi "tay chưa mở mà đã tính thắng". Có mã ván của sàn thì so mã
        # ván; không có thì so số ván.
        if pending is not None and round_id and pending.get("round"):
            stale = pending.get("round") != round_id
        else:
            stale = pending is not None and pending.get("hand") not in (None, hand)
        if stale:
            strategy.pending = None
        had_pending = strategy.pending is not None
        parsed, delta = self.sim.append(raw)
        if parsed is None:
            return
        self.armed_this_hand = False
        if stale:
            self.log("Ván %d: ra %s · BỎ lệnh đặt cho ván %s (không kịp vào tiền) — "
                     "không tính tiền." % (hand, longlong.raw_label(raw), pending.get("hand")),
                     "error")
        elif had_pending:
            self.log("Ván %d: ra %s · %s %s đv" % (
                hand, longlong.raw_label(raw),
                "THẮNG" if delta > 0 else "THUA",
                "{:,}".format(int(round(abs(delta)))).replace(",", ".")),
                "win" if delta > 0 else "loss")
        else:
            self.log("Ván %d: ra %s" % (hand, longlong.raw_label(raw)))
        self._last_place_fail = None
        self._chip_gone_at = None
        plan = strategy.plan()
        if plan["action"] == "bet":
            amount = longlong.to_site_amount(plan["stake"], self.unit_mode)
            self.log("Kế hoạch: đặt %s đv (= %sđ) — gửi %s lên sàn vào ô %s." % (
                _fn(plan["stake"]), _fn(plan["stake"] * longlong.DONG_PER_UNIT),
                _fn(amount), longlong.DOOR_NAME))
            if not self.config.get("place_real", False):
                # Mô phỏng: chốt trên giấy để sổ vốn phản ánh chiến thuật.
                strategy.arm(hand=hand + 1)
        elif plan["action"] == "stop":
            self.log("Phiên đã dừng: chạm đáy thang.", "error")

    # --------------------------------------------------------------- đặt chip
    def _try_place(self, state, wsnap=None):
        plan = self.sim.strategy.plan()
        if plan["action"] != "bet" or not self.config.get("place_real", False):
            return
        if not self._busy.acquire(blocking=False):
            return
        try:
            # Cửa sổ cược: luồng WebSocket của sàn nói CHÍNH XÁC đang mở hay đóng, nên
            # không còn cảnh bấm chip khi "ô chưa mở" rồi bị hoàn tiền.
            round_id = None
            if wsnap and wsnap.get("ok"):
                round_id = wsnap.get("round")
                phase = wsnap.get("phase")
                if phase != longlong_stream.OPEN_PHASE:
                    self._note_place_fail("Sàn chưa mở cược (%s)." % (phase or "?"))
                    return
                left = wsnap.get("remaining_ms")
                need_ms = int(self.config.get("min_bet_ms", 3000))
                if isinstance(left, int) and left < need_ms:
                    self._note_place_fail("Chỉ còn %d ms là đóng cược — bỏ ván này." % left)
                    return
            mode = self.unit_mode
            stake = plan["stake"]                                  # đv (1 đv = 1.000đ)
            amount = longlong.to_site_amount(stake, mode)          # đúng thang khay chip
            if amount <= 0:
                self.log("Bậc %s đv quá nhỏ so với đơn vị đang chọn (%s)." % (
                    _fn(stake), longlong.UNIT_MODE_NAME[mode]), "error")
                return
            balance = state.get("balance_vn2")                     # số tiền đọc trên sàn
            if balance is not None:
                spendable = longlong.from_site_amount(balance, mode)   # -> đv
                if spendable + 1e-6 < stake:
                    # Không đủ tiền: BỎ ván, không chốt sổ — chốt sổ mà không có tiền
                    # trên bàn chính là kiểu "tính tiền cho tay chưa đặt".
                    self._note_place_fail("Không đủ số dư (%sđ < %sđ): bỏ ván này, không đặt."
                                          % (_fn(spendable * longlong.DONG_PER_UNIT),
                                             _fn(stake * longlong.DONG_PER_UNIT)),
                                          tag="low_balance")
                    return
            result = self.table.place(amount)
            if result.get("ok"):
                self.sim.strategy.arm(hand=self.tracker.hands + 1)
                if self.sim.strategy.pending is not None:
                    # Ghi lại số tiền + mã ván để canh chip và tất toán đúng ván.
                    self.sim.strategy.pending["amount_site"] = amount
                    self.sim.strategy.pending["round"] = round_id
                self._chip_gone_at = None
                self._last_place_fail = None
                self.log("Đã đặt %s %s (= %sđ) vào ô %s · chip %s · tổng của bạn %s" % (
                    _fn(amount), longlong.UNIT_MODE_SHORT[mode],
                    _fn(stake * longlong.DONG_PER_UNIT), longlong.DOOR_NAME,
                    " + ".join(str(c) for c in (result.get("placed") or [])),
                    _fn(result.get("total") or 0.0)), "win")
            else:
                status = result.get("status")
                note = (" [sàn báo: %s]" % status) if status else ""
                self._note_place_fail("%s%s" % (result.get("error") or "?", note))
                hint = self._unit_hint(stake, result)
                if hint:
                    self._note_place_fail(hint)
        finally:
            self._busy.release()

    def _note_place_fail(self, text, tag=None):
        """Ghi lỗi đặt chip MỘT lần cho mỗi ván, tránh nhật ký đầy dòng lặp."""
        key = (self.tracker.hands, tag or text)
        if key == getattr(self, "_last_place_fail", None):
            return
        self._last_place_fail = key
        self.log("Đặt chip chưa được: %s" % text, "error")

    def _unit_hint(self, stake, result):
        """Gợi ý đổi "Đơn vị tiền trên sàn" khi khay chip toàn mệnh giá lớn.

        Chỉ gợi ý, KHÔNG tự đổi và càng không tự đặt số tiền lớn hơn: đổi đơn vị là
        quyết định của người dùng vì nó thay đổi số tiền thật trên bàn.
        """
        denominations = [int(v) for v in (result.get("denominations") or [])
                         if isinstance(v, (int, float)) and float(v) > 0]
        if not denominations:
            return ""
        mode = self.unit_mode
        other = longlong.UNIT_VND if mode == longlong.UNIT_VN2 else longlong.UNIT_VN2
        amount = longlong.to_site_amount(stake, other)
        if amount <= 0 or not longlong_table.decompose_amount(amount, denominations):
            return ""
        return ("Khay chip toàn mệnh giá lớn (%s) — có vẻ sàn hiển thị %s. "
                "Đổi “Đơn vị tiền trên sàn” sang %s rồi thử lại." % (
                    ", ".join(_fn(v) for v in denominations[:6]),
                    longlong.UNIT_MODE_NAME[other], longlong.UNIT_MODE_NAME[other]))

    # ------------------------------------------------------------------ cấu hình
    def config_set(self, params):
        changed = []
        if "ladder" in params:
            ladder = longlong.normalize_ladder(params.get("ladder"))
            self.config["ladder"] = ladder
            self.sim.set_ladder(ladder)
            changed.append("ladder")
        if "base" in params:
            try:
                self.config["base"] = int(params["base"])
                changed.append("base")
            except (TypeError, ValueError):
                pass
        if "capital" in params:
            try:
                self.config["capital"] = int(params["capital"])
                self.sim.reset(capital=self.config["capital"] * 1000.0)
                changed.append("capital")
            except (TypeError, ValueError):
                pass
        if "unit_mode" in params:
            mode = longlong.normalize_unit_mode(params.get("unit_mode"))
            if mode != self.config.get("unit_mode"):
                self.config["unit_mode"] = mode
                changed.append("unit_mode")
                self.log("Đơn vị tiền trên sàn: %s — bậc 1 (%s đv) sẽ gửi %s." % (
                    longlong.UNIT_MODE_NAME[mode],
                    _fn(self.sim.strategy.ladder[0]),
                    _fn(longlong.to_site_amount(self.sim.strategy.ladder[0], mode))))
        for key in ("place_real", "auto_result"):
            if key in params:
                self.config[key] = bool(params[key])
                changed.append(key)
        if "min_gap" in params:
            try:
                self.config["min_gap"] = float(params["min_gap"])
                self.tracker.configure(min_gap=self.config["min_gap"])
                changed.append("min_gap")
            except (TypeError, ValueError):
                pass
        if "min_hand_gap" in params:
            try:
                self.config["min_hand_gap"] = max(0.0, float(params["min_hand_gap"]))
                self.tracker.configure(min_hand_gap=self.config["min_hand_gap"])
                changed.append("min_hand_gap")
            except (TypeError, ValueError):
                pass
        if "confirm_reads" in params:
            try:
                self.config["confirm_reads"] = max(1, min(int(params["confirm_reads"]), 5))
                self.tracker.configure(confirm=self.config["confirm_reads"])
                changed.append("confirm_reads")
            except (TypeError, ValueError):
                pass
        if "chip_watch_seconds" in params:
            try:
                self.config["chip_watch_seconds"] = max(0.0, min(float(params["chip_watch_seconds"]),
                                                                30.0))
                changed.append("chip_watch_seconds")
            except (TypeError, ValueError):
                pass
        if "start_url" in params:
            url = str(params["start_url"] or "").strip()
            if not BR.valid_site_url(url):
                return {"ok": False, "error": "Link mở đầu không hợp lệ (cần http/https)."}
            self.config["start_url"] = url
            changed.append("start_url")
        if isinstance(params.get("chrome_window"), dict):
            incoming = params["chrome_window"]
            self.config["chrome_window"] = BR.normalize_window(
                (incoming.get("width", self.window["width"]),
                 incoming.get("height", self.window["height"])),
                incoming.get("percent", self.window["percent"]),
                incoming.get("fullscreen", self.window["fullscreen"]))
            changed.append("chrome_window")
        if self._save_config():
            self.log("Đã lưu cấu hình: %s." % (", ".join(changed) or "không đổi"))
        return self._push_ok()

    def ladder_from_capital(self, params):
        stakes = longlong.ladder_for_capital(params.get("capital"),
                                            params.get("base", self.config["base"]),
                                            int(params.get("steps") or len(self.config["ladder"])))
        self.config["ladder"] = stakes
        self.sim.set_ladder(stakes)
        self._save_config()
        self.log("Chia thang theo vốn: %s." % " → ".join(str(s) for s in stakes))
        return self._push_ok()

    def session_reset(self):
        self.sim.reset(capital=self.config["capital"] * 1000.0)
        self.tracker.reset()
        self._last_place_fail = None
        self.log("Đã reset phiên và bộ đếm ván.")
        return self._push_ok()

    def manual_hand(self, params):
        """Ghi một tay do người dùng nhập: đi đúng luồng như vòng đọc thật.

        Nhờ vậy sổ vốn và tab Thống kê cũng được cập nhật (mô phỏng tự chốt lệnh),
        thay vì chỉ thêm vào lịch sử đọc mà không tính tiền.
        """
        parsed = longlong.normalize_result(params.get("result"))
        if parsed is None:
            return {"ok": False, "error": "Kết quả không hợp lệ (VD/DV/VV/DD)."}
        self._on_hand(parsed)
        self.log("Ghi tay thủ công: %s" % longlong.raw_label(parsed))
        return self._push_ok()

    def shutdown(self):
        self.stop_heartbeat()
        self.tool_stop()
        try:
            self.table.close()
        except Exception:
            pass



    def dispatch(self, method, params):
        """Gọi method theo tên; trả (result, error) cho tầng transport."""
        table = {
            "ping": lambda p: {"ok": True, "pong": True, "game": "longlong"},
            "state": lambda p: self.snapshot(),
            "chrome.open": lambda p: self.open_chrome(),
            "chrome.close": lambda p: self.close_chrome(),
            "chrome.status": lambda p: self.chrome_status(),
            "chrome.window": self.apply_window,
            "tool.start": lambda p: self.tool_start(),
            "tool.stop": lambda p: self.tool_stop(),
            "config.set": self.config_set,
            "ladder.fromCapital": self.ladder_from_capital,
            "session.reset": lambda p: self.session_reset(),
            "hand.manual": self.manual_hand,
            "tick": lambda p: self.tick(),
        }
        handler = table.get(method)
        if handler is None:
            return None, "Method không hỗ trợ: %s" % method
        try:
            return handler(params or {}), None
        except Exception as exc:
            return None, "%s: %s" % (type(exc).__name__, exc)
