# -*- coding: utf-8 -*-
"""Tầng Service: engine chạy nền cho MỘT trò chơi, nói chuyện JSON qua stdin/stdout.

Mỗi trò là một tiến trình riêng (Electron spawn), có:
    - Chrome profile riêng và cổng CDP riêng  -> hai trò không giẫm lên nhau
    - file cấu hình/phiên riêng              -> sửa cấu hình trò này không ảnh hưởng trò kia
    - vòng đọc + đặt cược riêng

Giao thức (mỗi dòng một JSON):

    yêu cầu : {"id": 1, "method": "tool.start", "params": {}}
    trả lời : {"id": 1, "ok": true, "result": {...}}
              {"id": 1, "ok": false, "error": "..."}
    sự kiện : {"event": "state", "data": {...}}
              {"event": "log", "data": {"level": "info", "text": "..."}}

Chạy tay để kiểm tra:
    python -m bcr.service.engine --game longlong --cdp-port 9223

Không import tkinter: engine thuần logic + CDP.
"""

import argparse
import collections
import json
import pathlib
import sys
import threading
import time

from bcr.adapters import browser as BR
from bcr.adapters import longlong_table
from bcr.controllers import longlong_auto
from bcr.models import longlong

ROOT = pathlib.Path(__file__).resolve().parents[2]

GAMES = {
    # key: (cổng CDP, thư mục profile Chrome, file cấu hình/phiên)
    "baccarat": (9222, "chrome_profile", "bcr_state_baccarat.json"),
    "longlong": (9223, "chrome_profile_longlong", "bcr_state_longlong.json"),
}


class Emitter:
    """Ghi JSON ra stdout theo dòng, có khoá để nhiều thread không xen kẽ."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.lock = threading.Lock()

    def send(self, payload):
        line = json.dumps(payload, ensure_ascii=False)
        with self.lock:
            self.stream.write(line + "\n")
            self.stream.flush()

    def event(self, name, data):
        self.send({"event": name, "data": data})

    def reply(self, request_id, result=None, error=None):
        if error is None:
            self.send({"id": request_id, "ok": True, "result": result})
        else:
            self.send({"id": request_id, "ok": False, "error": str(error)})


class LongLongService:
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
        self.log("Engine LONG-LONG sẵn sàng.")

    # ------------------------------------------------------------- cấu hình
    def _load_config(self):
        defaults = {
            "ladder": list(longlong.DEFAULT_LADDER),
            "base": longlong.DEFAULT_BASE,
            "capital": longlong.DEFAULT_CAPITAL,       # đơn vị sảnh (10.000 = 10 triệu)
            "place_real": False,                       # đặt chip thật hay chỉ mô phỏng
            "auto_result": True,                       # tự chốt kết quả ván
            "min_gap": 15.0,
        }
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return defaults
        if isinstance(saved, dict):
            for key in defaults:
                if key in saved:
                    defaults[key] = saved[key]
        return defaults

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
        state = self.last_state or {}
        return {
            "game": "longlong",
            "chrome_connected": bool(getattr(self.browser, "connected", False)),
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
        pos = (40, 40)
        self.log("Đang mở Chrome cho LONG-LONG (profile riêng, cổng %d)…" % BR.CDP_PORT)

        def work():
            try:
                self.browser.start(url=BR.START_URL, pos=pos, cdp=True)
                if getattr(self.browser, "connected", False):
                    self.log("Chrome đã mở. Hãy đăng nhập và vào bàn LONG-LONG.")
                else:
                    self.log(self.browser.note or "Mở Chrome chưa thành công.", "error")
            except Exception as exc:
                self.log("Mở Chrome lỗi: %s" % exc, "error")
            finally:
                self._push_state()

        threading.Thread(target=work, daemon=True, name="ll-chrome").start()
        return {"ok": True, "note": "Đang mở Chrome…"}

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
        raw = params.get("result")
        parsed, _ = self.sim.append(raw)
        if parsed is None:
            return {"ok": False, "error": "Kết quả không hợp lệ (VD/DV/VV/DD)."}
        self.log("Ghi tay thủ công: %s" % longlong.raw_label(parsed))
        return self._push_ok()

    def shutdown(self):
        self.tool_stop()
        try:
            self.table.close()
        except Exception:
            pass


class BaccaratService:
    """Bản tối thiểu cho tab Baccarat: mở Chrome + quét bàn (chưa đặt cược)."""

    def __init__(self, state_path, emit):
        self.state_path = pathlib.Path(state_path)
        self.emit = emit
        self.browser = BR.BrowserSession()
        self.tool_running = False
        self._stop = threading.Event()
        self.logs = collections.deque(maxlen=100)
        self.tables = []
        self.log("Engine Baccarat sẵn sàng (đọc bàn, chưa đặt cược).")

    def log(self, text, level="info"):
        entry = {"at": time.strftime("%H:%M:%S"), "level": level, "text": str(text)}
        self.logs.append(entry)
        self.emit.event("log", entry)

    def snapshot(self):
        return {"game": "baccarat",
                "chrome_connected": bool(getattr(self.browser, "connected", False)),
                "tool_running": self.tool_running,
                "tables": self.tables,
                "read_error": getattr(self.browser, "last_error", "")}

    def _push_state(self):
        state = self.snapshot()
        self.emit.event("state", state)
        return state

    def open_chrome(self):
        if getattr(self.browser, "connected", False):
            return {"ok": True, "note": "Chrome đã kết nối."}
        self.log("Đang mở Chrome cho Baccarat (profile riêng, cổng %d)…" % BR.CDP_PORT)

        def work():
            try:
                self.browser.start(url=BR.START_URL, pos=(40, 40), cdp=True)
                self.log("Chrome đã mở. Đăng nhập và vào sảnh Baccarat.")
            except Exception as exc:
                self.log("Mở Chrome lỗi: %s" % exc, "error")
            finally:
                self._push_state()

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def chrome_status(self):
        return {"connected": bool(getattr(self.browser, "connected", False)),
                "note": getattr(self.browser, "note", "")}

    def close_chrome(self):
        try:
            self.browser.stop()
        except Exception:
            pass
        return {"ok": True}

    def scan(self):
        try:
            reports = self.browser.scan() or []
        except Exception as exc:
            self.log("Quét lỗi: %s" % exc, "error")
            return self._push_state()
        rows = []
        for report in reports:
            for table in report.get("tables") or []:
                rows.append({"id": table.get("id"), "label": table.get("title") or table.get("id"),
                             "letters": table.get("letters"), "note": table.get("read_error")})
        self.tables = rows
        self.log("Quét được %d bàn." % len(rows))
        return self._push_state()

    def tool_start(self):
        if self.tool_running:
            return {"ok": True}
        if not getattr(self.browser, "connected", False):
            return {"ok": False, "error": "Chưa mở Chrome."}
        self.tool_running = True
        self._stop = threading.Event()

        def loop():
            while not self._stop.is_set():
                self.scan()
                self._stop.wait(4.0)
            self.tool_running = False
            self._push_state()

        threading.Thread(target=loop, daemon=True).start()
        self.log("Chạy tool Baccarat: quét bàn định kỳ.")
        return {"ok": True}

    def tool_stop(self):
        self.tool_running = False
        self._stop.set()
        self.log("Đã dừng tool Baccarat.")
        return {"ok": True}

    def shutdown(self):
        self.tool_stop()


def build_service(game, emit, state_path):
    if game == "longlong":
        return LongLongService(state_path, emit)
    if game == "baccarat":
        return BaccaratService(state_path, emit)
    raise SystemExit("Trò không hỗ trợ: %s" % game)


def dispatch(service, method, params):
    """Gọi method theo tên; trả về (result, error)."""
    table = {
        "ping": lambda p: {"ok": True, "pong": True, "game": type(service).__name__},
        "state": lambda p: service.snapshot(),
        "chrome.open": lambda p: service.open_chrome(),
        "chrome.close": lambda p: service.close_chrome(),
        "chrome.status": lambda p: service.chrome_status(),
        "tool.start": lambda p: service.tool_start(),
        "tool.stop": lambda p: service.tool_stop(),
    }
    if isinstance(service, LongLongService):
        table.update({
            "config.set": service.config_set,
            "ladder.fromCapital": service.ladder_from_capital,
            "session.reset": lambda p: service.session_reset(),
            "hand.manual": service.manual_hand,
            "tick": lambda p: service.tick(),
        })
    elif isinstance(service, BaccaratService):
        table.update({"scan": lambda p: service.scan()})
    handler = table.get(method)
    if handler is None:
        return None, "Method không hỗ trợ: %s" % method
    try:
        return handler(params or {}), None
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description="BCR engine (JSON qua stdin/stdout)")
    parser.add_argument("--game", required=True, choices=sorted(GAMES))
    parser.add_argument("--cdp-port", type=int, default=None)
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--state", default=None)
    args = parser.parse_args(argv)

    port, profile, state_name = GAMES[args.game]
    # Cách ly từng trò: đặt lại hằng số của adapter TRƯỚC khi tạo service.
    BR.CDP_PORT = args.cdp_port or port
    BR.PROFILE_DIR = str(ROOT / (args.profile_dir or profile))
    state_path = pathlib.Path(args.state) if args.state else ROOT / state_name

    emit = Emitter()
    service = build_service(args.game, emit, state_path)
    emit.event("ready", {"game": args.game, "cdp_port": BR.CDP_PORT,
                         "profile": BR.PROFILE_DIR})
    emit.event("state", service.snapshot())

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            emit.reply(None, error="JSON không hợp lệ")
            continue
        request_id = request.get("id")
        method = request.get("method") or ""
        if method in ("shutdown", "quit"):
            emit.reply(request_id, {"ok": True})
            break
        result, error = dispatch(service, method, request.get("params") or {})
        emit.reply(request_id, result, error)
    service.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
