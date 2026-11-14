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
from bcr.adapters import longlong_table
from bcr.controllers import longlong_auto
from bcr.models import longlong


class LongLongController:
    """Đọc bàn LONG-LONG, chốt kết quả và đặt chip theo thang đã cấu hình."""

    def __init__(self, state_path, emit, interval=2.0):
        self.state_path = pathlib.Path(state_path)
        self.emit = emit
        self.interval = float(interval)
        self.config = self._load_config()
        self.sim = longlong.LongLongSimulation(self.config["ladder"],
                                               capital=self.config["capital"] * 1000.0)
        self.tracker = longlong_auto.HandTracker(min_gap=self.config.get("min_gap", 15.0))
        self.table = longlong_table.LongLongTable()
        self.browser = BR.BrowserSession()
        self.logs = collections.deque(maxlen=200)
        self.tool_running = False
        self._stop = threading.Event()
        self._thread = None
        self._busy = threading.Lock()
        self.last_state = {}
        # Nhịp nền: đẩy state + phát hiện Chrome bị đóng kể cả khi tool đang dừng.
        self.offline_noted = False
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
            "place_real": False,                       # đặt chip thật hay chỉ mô phỏng
            "auto_result": True,                       # tự chốt kết quả ván
            "min_gap": 15.0,
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
        return config

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
            "pending": strategy.pending,
            "stopped": strategy.stopped,
            "plan": plan,
            "hands_seen": self.tracker.hands,
            "log": [dict(entry) for entry in strategy.log[-20:]],
            "profit_units": self.sim.profit / 1000.0,
            # Số liệu cho tab Thống kê: tỉ lệ thắng/thua, chuỗi thua dài nhất, đường lãi/lỗ.
            "stats": longlong.summarize_history(self.sim.log),
        }

    def _push_state(self):
        state = self.snapshot()
        self.emit.event("state", state)
        return state

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
                self.log("Chrome đã đóng · đã dừng tool. Bấm “Mở Chrome” để chạy lại.", "error")
                break
            try:
                self.tick()
            except Exception as exc:
                self.log("Lỗi vòng đọc: %s" % exc, "error")
            self._stop.wait(self.interval)
        self.tool_running = False
        self._push_state()

    def tick(self):
        """Một nhịp: đọc bàn -> chốt ván -> (nếu được bật) đặt chip -> phát state."""
        state = self.table.read()
        state.setdefault("read_at", time.time())
        self.last_state = state
        if state.get("ok") and self.config.get("auto_result", True):
            raw = self.tracker.feed(state.get("winner"), state.get("winner_raw"),
                                    state.get("read_at"))
            if raw:
                self._on_hand(raw)
        if state.get("ok"):
            self._try_place(state)
        return self._push_state()

    # ------------------------------------------------------------------ một ván
    def _on_hand(self, raw):
        strategy = self.sim.strategy
        had_pending = strategy.pending is not None
        parsed, delta = self.sim.append(raw)
        if parsed is None:
            return
        self.armed_this_hand = False
        if had_pending:
            self.log("Ván %d: ra %s · %s %s đv" % (
                self.tracker.hands, longlong.raw_label(raw),
                "THẮNG" if delta > 0 else "THUA",
                "{:,}".format(int(round(abs(delta)))).replace(",", ".")),
                "win" if delta > 0 else "loss")
        else:
            self.log("Ván %d: ra %s" % (self.tracker.hands, longlong.raw_label(raw)))
        plan = strategy.plan()
        if plan["action"] == "bet":
            self.log("Kế hoạch: đặt %s đv vào ô %s." % (
                "{:,}".format(plan["stake"]).replace(",", "."), longlong.DOOR_NAME))
            if not self.config.get("place_real", False):
                strategy.arm()      # mô phỏng: chốt trên giấy để sổ vốn phản ánh chiến thuật
        elif plan["action"] == "stop":
            self.log("Phiên đã dừng: chạm đáy thang.", "error")

    # --------------------------------------------------------------- đặt chip
    def _try_place(self, state):
        plan = self.sim.strategy.plan()
        if plan["action"] != "bet" or not self.config.get("place_real", False):
            return
        if not self._busy.acquire(blocking=False):
            return
        try:
            stake = plan["stake"]
            balance = state.get("balance_vn2")
            if balance is not None and balance + 0.001 < float(stake):
                self.log("Không đủ số dư (%s < %s VN2): bỏ ván này." % (
                    "{:,.2f}".format(balance), "{:,.2f}".format(stake)), "error")
                self.sim.strategy.arm()   # vẫn ghi sổ để không lệch thang
                return
            result = self.table.place(stake)
            if result.get("ok"):
                self.sim.strategy.arm()
                self.log("Đã đặt %s VN2 vào ô %s · chip %s · tổng của bạn %s" % (
                    "{:,.2f}".format(stake), longlong.DOOR_NAME,
                    " + ".join(str(c) for c in (result.get("placed") or [])),
                    "{:,.2f}".format(result.get("total") or 0.0)), "win")
            else:
                self.log("Đặt chip chưa được: %s" % (result.get("error") or "?"), "error")
        finally:
            self._busy.release()

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
        for key in ("place_real", "auto_result"):
            if key in params:
                self.config[key] = bool(params[key])
                changed.append(key)
        if "min_gap" in params:
            try:
                self.config["min_gap"] = float(params["min_gap"])
                self.tracker.min_gap = self.config["min_gap"]
                changed.append("min_gap")
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
