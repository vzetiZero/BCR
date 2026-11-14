# -*- coding: utf-8 -*-
"""Tầng Controller · Baccarat: đọc cầu, chốt kế hoạch, đặt chip, săn bàn, phiên.

Chỉ điều phối Model (``bcr.models``) và Adapter (``bcr.adapters``); không dùng tkinter
và không biết gì về Electron. Tầng transport (``bcr.service.engine``) gọi
``dispatch`` để phục vụ giao diện, giống tab LONG-LONG.

Trách nhiệm:
    - mở/gắn Chrome (CDP 9222), vào sảnh, quét sảnh, mở bàn, rời bàn
    - đọc cầu bàn, chạy LiveSimulation, chốt kế hoạch cược 3-3 / 3-2
    - tự đặt chip thật theo kế hoạch (gate "chờ ván mới" + xác nhận đã đặt)
    - tự săn bàn (quét lại theo chu kỳ), chốt lời, đủ số bàn thì dừng
    - lưu/khôi phục phiên (vốn, lãi/lỗ, thang cược, cấu hình) trong file state riêng

Mọi tính toán tiền/cầu nằm ở ``bcr.models``; mọi thao tác web nằm ở ``bcr.adapters``.
Module này chỉ điều phối; mọi tính toán nằm ở Model, mọi thao tác web ở Adapter.
"""

import collections
import json
import pathlib
import queue
import threading
import time

from bcr.adapters import browser as BR
from bcr.adapters import lobby_stream as LS
from bcr.adapters import table_bets as TB
from bcr.config import *  # noqa: F401,F403
from bcr.models import engine as E
from bcr.models import method as ME
from bcr.models import money as M
from bcr.models.live_sim import (LiveSimulation, is_fast_table, road_is_current,
                                 row_problem, same_shoe, table_is_clear)

# Cấu hình mặc định của tab Baccarat (đơn vị VN2 như hiển thị trên sàn).
DEFAULT_CONFIG = {
    "capital": 3000.0,          # vốn phiên (VN2)
    "take_profit_pct": 25.0,    # chốt lời % vốn
    "profile": ME.DEFAULT_PROFILE,
    "ladder": None,             # None = dùng thang mặc định của phương pháp
    "ladder_mode": "manual",    # manual | multiply
    "ladder_base": 100.0,       # bậc 1 khi dùng cơ chế tự nhân
    "block_mode": ME.BLOCK_MODE_RECALCULATE,
    "min_hands": ME.MIN_HANDS,
    "max_hands": ME.MAX_HANDS,
    "max_streak": ME.MAX_STREAK_ALLOWED,
    "min_lead": ME.DEFAULT_MIN_LEAD,   # lệch B/P tối thiểu (bên mạnh phải hơn bên yếu)
    "max_lead": ME.DEFAULT_MAX_LEAD,   # lệch B/P tối đa còn nhận (4-5 tay)
    "tie_side": ME.DEFAULT_TIE_SIDE,   # B = P thì ưu tiên bên đỏ (Banker)
    "live_exit_streak": ME.DEFAULT_LIVE_EXIT_STREAK,
    "favorites_only": True,
    "human_mouse": True,
    "auto_place": True,
    "auto_cycle": True,
    "auto_hunt": True,
    "auto_recover": True,
    "tables_to_play": 1,
    "start_url": BR.START_URL,
    "chrome_window": {"width": 1280, "height": 800, "percent": 100, "fullscreen": False},
}


def _as_float(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return number if number == number else float(fallback)


def _as_int(value, fallback):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(fallback)


def _finite(value, fallback=0.0):
    """JSON của Electron không nhận NaN/Infinity; mọi số ra ngoài đều phải hữu hạn."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if number != number or number in (float("inf"), float("-inf")):
        return float(fallback)
    return number


class BaccaratController:
    """Điều phối một phiên Baccarat: Chrome → sảnh → bàn → cược → chuyển bàn."""

    def __init__(self, state_path, emit, interval=2.0):
        self.state_path = pathlib.Path(state_path)
        self.emit = emit
        self.interval = float(interval or 2.0)
        self.lock = threading.RLock()
        self.events = queue.Queue()
        self.logs = collections.deque(maxlen=300)
        self.config = self._load_config()

        self.browser = BR.BrowserSession()
        self.stream = LS.LobbyStream(self.browser, human_mouse=self.config["human_mouse"],
                                     game="baccarat")
        self.sim = self._build_sim()

        self.tables = {}
        self.tool_running = False
        self._stop = threading.Event()
        self._thread = None

        # cờ bận cho các thao tác web (giống controller Tk)
        self.chrome_busy = False
        self.scan_busy = False
        self.sweep_busy = False
        self.open_table_busy = False
        self.chip_busy = False
        self.place_busy = False
        self.recover_busy = False
        # Đã báo "Chrome đã đóng" chưa (tránh spam log mỗi 2 giây khi mất kết nối).
        self.offline_noted = False

        self.next_scan = 0.0
        self.chip_live = None
        self.chip_read_at = 0.0
        self.place_status = {"ok": None, "note": ""}
        self.wallet_text = "Số dư ví trên web: chưa đọc được"
        self.sweep_note = "Mở Chrome, vào sảnh Baccarat rồi quét sảnh."
        self.connection_text = "Chưa kết nối Chrome."

        # vòng tự săn bàn
        self.cycle_waiting = False
        self.cycle_attempts = 0
        self.cycle_retry_at = 0.0
        self.cycle_table_key = None
        self.tables_done = 0

        # xác nhận bàn vừa mở trước khi tự theo dõi
        self.auto_start_key = None
        self.auto_start_letters = ""
        self.auto_start_deadline = 0.0

        self.session_lost = False
        self.recover_attempts = 0

        self.log("Engine Baccarat sẵn sàng (đọc bàn + tự đặt cược theo kế hoạch).")

    # ------------------------------------------------------------- cấu hình
    def _load_config(self):
        config = dict(DEFAULT_CONFIG)
        saved = {}
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            saved = {}
        settings = saved.get("settings") if isinstance(saved, dict) else None
        if isinstance(settings, dict):
            for key in config:
                if key in settings and settings[key] is not None:
                    config[key] = settings[key]
            # tương thích state của app Tk (khoá khác tên)
            if "hands_min" in settings:
                config["min_hands"] = settings["hands_min"]
            if "hands_max" in settings:
                config["max_hands"] = settings["hands_max"]
        self._saved_session = saved if isinstance(saved, dict) else {}
        self.config = self._normalize_config(config)
        return self.config

    def _normalize_config(self, config):
        profile = config.get("profile")
        if profile not in ME.PROFILES:
            profile = ME.DEFAULT_PROFILE
        config["profile"] = profile
        config["capital"] = max(1.0, _as_float(config.get("capital"), DEFAULT_CONFIG["capital"]))
        config["take_profit_pct"] = max(1.0, _as_float(config.get("take_profit_pct"),
                                                       DEFAULT_CONFIG["take_profit_pct"]))
        config["ladder_mode"] = "multiply" if config.get("ladder_mode") == "multiply" else "manual"
        config["ladder_base"] = max(1.0, _as_float(config.get("ladder_base"),
                                                   DEFAULT_CONFIG["ladder_base"]))
        if config.get("block_mode") not in (ME.BLOCK_MODE_RECALCULATE, ME.BLOCK_MODE_REPEAT):
            config["block_mode"] = ME.BLOCK_MODE_RECALCULATE
        low, high = ME.normalize_hand_range(config.get("min_hands"), config.get("max_hands"))
        config["min_hands"], config["max_hands"] = low, high
        config["max_streak"] = ME.normalize_streak(config.get("max_streak"))
        config["min_lead"] = ME.normalize_min_lead(config.get("min_lead"))
        # "" / 0 / None / "none" = không giới hạn chênh lệch tối đa.
        raw_max = config.get("max_lead", ME.DEFAULT_MAX_LEAD)
        if raw_max is None or str(raw_max).strip().lower() in ("", "none", "off", "0"):
            config["max_lead"] = None
        else:
            config["max_lead"] = max(config["min_lead"],
                                     ME.normalize_max_lead(raw_max, ME.DEFAULT_MAX_LEAD))
        config["tie_side"] = ME.normalize_tie_side(config.get("tie_side"))
        config["live_exit_streak"] = ME.normalize_streak(config.get("live_exit_streak"),
                                                         ME.DEFAULT_LIVE_EXIT_STREAK)
        for key in ("favorites_only", "human_mouse", "auto_place", "auto_cycle",
                    "auto_hunt", "auto_recover"):
            config[key] = bool(config.get(key, DEFAULT_CONFIG[key]))
        config["tables_to_play"] = max(1, _as_int(config.get("tables_to_play"), 1))
        url = config.get("start_url")
        config["start_url"] = url if isinstance(url, str) and BR.valid_site_url(url.strip()) \
            else BR.START_URL
        window = config.get("chrome_window")
        if not isinstance(window, dict):
            window = dict(DEFAULT_CONFIG["chrome_window"])
        config["chrome_window"] = BR.normalize_window(
            (window.get("width"), window.get("height")),
            window.get("percent"), window.get("fullscreen"))
        config["ladder"] = self._normalize_ladder(config.get("ladder"), config)
        return config

    @staticmethod
    def _normalize_ladder(ladder, config):
        """Chuẩn hoá thang cược; sai/thiếu thì về thang mặc định của phương pháp."""
        profile = config["profile"]
        if config.get("ladder_mode") == "multiply" and ladder in (None, "", []):
            ladder = ME.multiply_ladder(config["ladder_base"], profile)
        try:
            return [float(s) for s in ME.ladder_stakes(profile, ladder)]
        except (ValueError, TypeError):
            return [float(s) for s in ME.ladder_stakes(profile, None)]

    def _build_sim(self):
        config = self.config
        stakes = config["ladder"]
        book = M.MoneyBook(capital=config["capital"], unit=stakes[0],
                           take_profit_pct=config["take_profit_pct"])
        sim = LiveSimulation(book, profile=config["profile"], ladder=stakes,
                             min_hands=config["min_hands"], max_hands=config["max_hands"],
                             max_streak=config["max_streak"],
                             live_exit_streak=config["live_exit_streak"],
                             min_lead=config["min_lead"],
                             block_mode=config["block_mode"],
                             max_lead=config["max_lead"], tie_side=config["tie_side"])
        sim.on_arm = self._place_armed_bet
        # Khôi phục sổ vốn + lịch sử tay của phiên trước (nếu có).
        saved = getattr(self, "_saved_session", {}) or {}
        if saved.get("format") == "live-v2-engine":
            money = saved.get("money") or {}
            for key in ("balance", "hands_played", "wins", "losses", "pushes", "ties_bet",
                        "total_staked", "total_returned", "peak", "max_drawdown",
                        "loss_streak"):
                if key in money:
                    try:
                        setattr(book, key, float(money[key]))
                    except (TypeError, ValueError):
                        pass
            if isinstance(saved.get("log"), list):
                sim.log = list(saved["log"])
        return sim

    def _save_state(self):
        with self.lock:
            book = self.sim.money
            data = {
                "format": "live-v2-engine",
                "game": "baccarat",
                "settings": dict(self.config),
                "money": {key: getattr(book, key) for key in
                          ("start_capital", "balance", "hands_played", "wins", "losses",
                           "pushes", "ties_bet", "total_staked", "total_returned",
                           "peak", "max_drawdown", "loss_streak")},
                "log": self.sim.log[-500:],
            }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_path)
            return True
        except OSError as exc:
            self.log("Không lưu được phiên (%s)." % type(exc).__name__, "error")
            return False

    # --------------------------------------------------------------- tiện ích
    def log(self, text, level="info"):
        entry = {"at": time.strftime("%H:%M:%S"), "level": level, "text": str(text)}
        self.logs.append(entry)
        try:
            self.emit.event("log", entry)
        except Exception:
            pass
        return entry

    def _busy(self):
        return (self.chrome_busy or self.scan_busy or self.sweep_busy or self.open_table_busy)

    def _cdp_available(self):
        try:
            if self.browser.cdp_attached:
                return True
            if self.browser.driver is None:
                return False
            return self.browser._cdp_page() is not None
        except AttributeError:
            return False
        except Exception:
            return False

    def _auth_blame(self):
        if getattr(self.stream, "session_lost", False):
            return True
        diag = getattr(self.browser, "last_diag", None)
        return isinstance(diag, dict) and diag.get("reason") == "authentication_required"

    def _window(self):
        return self.config["chrome_window"]

    def _window_pos(self, window):
        if window.get("fullscreen"):
            return None
        return (40, 40)

    def _window_text(self, window):
        return ("CHROME · toàn màn hình · tỉ lệ %d%%" % window["percent"] if window.get("fullscreen")
                else "CHROME · %d × %d · tỉ lệ %d%%" % (window["width"], window["height"],
                                                        window["percent"]))

    def _tables_target(self):
        return max(1, _as_int(self.config.get("tables_to_play"), 1))

    def _session_complete(self):
        return self.tables_done >= self._tables_target()

    # ---------------------------------------------------------------- snapshot
    def table_rows(self):
        """Danh sách bàn đọc được, kèm kết luận bộ lọc cầu cho UI."""
        now = time.time()
        rows = []
        # Không còn Chrome thì không có dữ liệu sống: đừng hiện bàn cũ trên UI.
        if not getattr(self.browser, "connected", False):
            return rows
        for key, row in list(self.tables.items()):
            age = now - row["ts"]
            if age > LIVE_STALE and key != self.sim.table_key:
                continue
            info = ME.check_table(list(row["letters"]), self.sim.min_hands, self.sim.max_hands,
                                  self.sim.max_streak, self.sim.min_lead,
                                  max_lead=self.sim.max_lead, tie_side=self.sim.tie_side)
            problem = row_problem(row)
            ready = info["eligible"] and not problem and road_is_current(row)
            passed = sum(1 for check in info["checks"] if check["ok"])
            total = len(info["checks"])
            if row.get("read_error"):
                verdict = row["read_error"]
            elif info["eligible"]:
                verdict = "Đạt %d/%d · cần đối chiếu" % (passed, total)
            else:
                verdict = "%d/%d · %s" % (passed, total, next(c["detail"] for c in info["checks"]
                                                              if not c["ok"]))
            if row.get("conflict"):
                status = "Xung đột"
            elif row.get("stable", 0) < 2:
                status = "Chờ xác nhận đọc"
            elif age > LIVE_STALE:
                status = "Dữ liệu cũ"
            else:
                status = "Mới · %ds" % int(age)
            if row.get("game_type") and row["game_type"] != "baccarat":
                status = "Bàn đặc biệt"
            if is_fast_table(row.get("label")):
                status += " · siêu tốc"
            rows.append({
                "key": key,
                "id": row.get("table_id") or key,
                "table_id": row.get("table_id", ""),
                "label": row.get("label", key),
                "letters": row.get("letters", ""),
                "hands": len(row.get("letters") or ""),
                "strong": info["strong"],
                "strong_name": E.SIDE_NAME.get(info["strong"], "—"),
                "banker": info.get("banker"),
                "player": info.get("player"),
                "lead": info.get("lead"),
                "tie_break": info.get("tie_break"),
                "tie_side": info.get("tie_side"),
                "eligible": bool(info["eligible"]),
                "ready": bool(ready),
                "verdict": verdict,
                "status": status,
                "checks": info["checks"],
                "problem": problem,
                "read_error": row.get("read_error", ""),
                "current": key == self.sim.table_key,
            })
        rows.sort(key=lambda item: (not item["ready"], not item["eligible"],
                                    item["label"].casefold()))
        return rows

    def _plan(self):
        plan = self.sim.pending or self.sim.plan()
        return {
            "action": plan.get("action"),
            "side": plan.get("side"),
            "side_name": E.SIDE_NAME.get(plan.get("side"), ""),
            "stake": float(plan.get("stake") or 0),
            "reason": plan.get("reason", ""),
            "block_pos": plan.get("block_pos", 0),
            "ladder_step": plan.get("ladder_step", 0),
            "strong": plan.get("strong"),
            "checks": plan.get("checks", []),
            "armed": self.sim.pending is not None,
        }

    def snapshot(self):
        with self.lock:
            book = self.sim.money
            sim = self.sim
            decisive = book.wins + book.losses
            money = {
                "capital": _finite(book.start_capital),
                "balance": _finite(book.balance),
                "profit": _finite(book.profit),
                "roi": _finite((book.profit / book.start_capital * 100)
                               if book.start_capital else 0.0),
                "wins": int(book.wins),
                "losses": int(book.losses),
                "pushes": int(book.pushes),
                "hands": int(book.hands_played),
                "drawdown": _finite(book.max_drawdown),
                "win_rate": _finite((book.wins / decisive * 100) if decisive else 0.0),
                "blocked": list(book.blocked) if book.blocked else None,
            }
            history = [{
                "time": _finite(entry.get("time")),
                "at": time.strftime("%H:%M:%S", time.localtime(_finite(entry.get("time"),
                                                                      time.time()))),
                "table": str(entry.get("table") or ""),
                "hand": int(_finite(entry.get("hand"))),
                "bet": str(entry.get("bet") or ""),
                "result": str(entry.get("result") or ""),
                "stake": _finite(entry.get("stake")),
                "delta": _finite(entry.get("delta")),
                "outcome": str(entry.get("outcome") or ""),
                "balance_after": _finite(entry.get("balance_after")),
            } for entry in sim.log[-200:]]
            plan = self._plan()
            chip_check = self.chip_check() if self.sim.table_key else None
            now = time.time()
            return {
                "game": "baccarat",
                "chrome_connected": bool(getattr(self.browser, "connected", False)),
                "chrome_mode": getattr(self.browser, "mode", ""),
                "chrome_note": (getattr(self.browser, "note", "") or self.connection_text),
                "driver_info": getattr(self.browser, "driver_info", "Chưa kết nối Chrome."),
                "cdp_ready": self._cdp_available(),
                "tool_running": self.tool_running,
                "session_lost": self.session_lost,
                "busy": {
                    "chrome": self.chrome_busy, "scan": self.scan_busy,
                    "sweep": self.sweep_busy, "open_table": self.open_table_busy,
                    "place": self.place_busy, "chip": self.chip_busy,
                },
                "sweep_note": self.sweep_note,
                "wallet_text": self.wallet_text,
                "cycle": {
                    "waiting": bool(self.cycle_waiting),
                    "attempts": self.cycle_attempts,
                    "retry_in": max(0.0, (self.cycle_retry_at or 0) - now),
                },
                "tables_done": self.tables_done,
                "tables_target": self._tables_target(),
                "table": {
                    "key": sim.table_key,
                    "name": sim.table_name,
                    "letters": sim.letters,
                    "hands": len(sim.letters or ""),
                    "running": sim.running,
                    "entry_waiting": sim.entry_waiting,
                    "note": sim.note,
                    "profile": sim.profile,
                    "profile_label": ME.PROFILE_LABEL.get(sim.profile, sim.profile),
                    "block_mode": sim.block_mode,
                    "cycle_hands": sim.cycle_hands,
                    "strong_hands": ME.strong_hands(sim.profile),
                    "ladder": [float(s) for s in sim.ladder],
                    "block_hands": sim.block_hands,
                    "place_skips": sim.place_skips,
                    "take_profit_done": bool(sim.take_profit_done),
                    "filter_exit_done": bool(sim.filter_exit_done),
                },
                "plan": plan,
                "money": money,
                "chips": {
                    "live": self.chip_live,
                    "check": chip_check,
                    "at": self.chip_read_at,
                },
                "place": dict(self.place_status),
                "tables": self.table_rows(),
                "config": json.loads(json.dumps(self.config)),
                "history": history,
                "log_tail": list(sim.log[-15:]),
            }

    def _push_state(self):
        state = self.snapshot()
        try:
            self.emit.event("state", state)
        except Exception:
            pass
        return state

    # ----------------------------------------------------------------- Chrome
    def chrome_open(self):
        if self.browser.connected:
            return {"ok": True, "note": "Chrome đã kết nối."}
        if self.chrome_busy:
            return {"ok": False, "error": "Đang mở Chrome, chờ một chút."}
        self.chrome_busy = True
        self.connection_text = "Đang mở Chrome / gắn CDP 9222…"
        window = self._window()
        pos = self._window_pos(window)

        def work():
            try:
                attached = self.browser.attach()
                if not attached and self.browser.last_diag.get("reason") == "evo_entry_auth_error":
                    self.events.put(("open_error", self.browser.note))
                    return
                if not attached:
                    ok = self.browser.start(self.config["start_url"], pos=pos, cdp=True,
                                            window=window)
                    if not ok:
                        self.events.put(("open_error", self.browser.last_error or "Không mở được Chrome."))
                        return
                    self.browser.wait_for_cdp()
                try:
                    self.browser.resize_window(window, pos=pos)
                except Exception:
                    pass
                self.events.put(("opened", "%s · Đăng nhập, vào sảnh Baccarat rồi bấm “Nhận tab sảnh”."
                                 % self.browser.driver_info))
            except Exception as exc:
                self.events.put(("open_error", "Không mở được Chrome (%s)." % type(exc).__name__))

        threading.Thread(target=work, daemon=True, name="ba-chrome").start()
        self._push_state()
        return {"ok": True, "note": "Đang mở Chrome…"}

    def chrome_attach(self):
        if self.chrome_busy:
            return {"ok": False, "error": "Đang bận thao tác Chrome."}
        self.chrome_busy = True

        def work():
            try:
                ok = self.browser.attach()
                if ok:
                    try:
                        self.browser.resize_window(self._window(), pos=self._window_pos(self._window()))
                    except Exception:
                        pass
                self.events.put(("opened" if ok else "open_error",
                                 self.browser.note or "Không gắn được Chrome."))
            except Exception as exc:
                self.events.put(("open_error", "Không gắn được Chrome (%s)." % type(exc).__name__))

        threading.Thread(target=work, daemon=True, name="ba-attach").start()
        self._push_state()
        return {"ok": True, "note": "Đang gắn Chrome…"}

    def chrome_close(self):
        self.tool_running = False
        with self.lock:
            self.sim.pause("Đã ngắt kết nối Chrome.")
        self.chrome_busy = True

        def work():
            try:
                self.browser.stop()
            finally:
                self.events.put(("stopped", "Đã ngắt kết nối Chrome."))

        threading.Thread(target=work, daemon=True, name="ba-stop").start()
        self._push_state()
        return {"ok": True, "note": "Đang ngắt kết nối…"}

    def apply_window(self, params):
        window = BR.normalize_window(
            (params.get("width", self._window()["width"]),
             params.get("height", self._window()["height"])),
            params.get("percent", self._window()["percent"]),
            params.get("fullscreen", self._window()["fullscreen"]))
        self.config["chrome_window"] = window
        self._save_state()
        if not self.browser.connected:
            self.sweep_note = "Đã lưu cửa sổ Chrome (%s). Mở Chrome là áp dụng ngay." % \
                              self._window_text(window)
            self._push_state()
            return {"ok": True, "note": self.sweep_note}
        self.chrome_busy = True

        def work():
            try:
                ok, note = self.browser.resize_window(window, pos=self._window_pos(window))
                self.events.put(("window_applied", (ok, note or self._window_text(window))))
            except Exception as exc:
                self.events.put(("window_applied", (False, "Không đổi được cửa sổ (%s)."
                                                  % type(exc).__name__)))

        threading.Thread(target=work, daemon=True, name="ba-window").start()
        return {"ok": True, "note": "Đang áp dụng cửa sổ Chrome…"}

    # ------------------------------------------------------------------ sảnh
    def lobby_enter(self):
        if self.sweep_busy or self.chrome_busy:
            return {"ok": False, "error": "Đang bận thao tác sảnh."}
        if not self._cdp_available():
            self.sweep_note = "Chưa bật CDP: mở trang Baccarat trong Chrome rồi gắn Chrome trước."
            self._push_state()
            return {"ok": False, "error": self.sweep_note}
        self.sweep_busy = True
        self.session_lost = False
        self.recover_attempts = 0
        self.sweep_note = "Đang tìm tab sảnh Baccarat bạn đã tự mở…"

        def work():
            try:
                ok, note = self.stream.find_lobby_tab()
                self.events.put(("lobby_entered", (ok, note)))
            except Exception as exc:
                self.events.put(("lobby_entered", (False, "Không nhận được tab sảnh (%s)."
                                                  % type(exc).__name__)))

        threading.Thread(target=work, daemon=True, name="ba-lobby").start()
        self._push_state()
        return {"ok": True, "note": self.sweep_note}

    def lobby_sweep(self, params=None):
        params = params or {}
        if self.sweep_busy or self.chrome_busy:
            return {"ok": False, "error": "Đang bận thao tác sảnh."}
        if not self._cdp_available():
            self.sweep_note = "Gắn Chrome đang mở trước rồi quét sảnh."
            self._push_state()
            return {"ok": False, "error": self.sweep_note}
        favorites = params.get("favorites_only")
        favorites = self.config["favorites_only"] if favorites is None else bool(favorites)
        self.config["favorites_only"] = favorites
        self.sweep_busy = True
        self.sweep_note = ("Đang quét sảnh · chỉ bàn Ưa thích…" if favorites else "Đang quét sảnh…")

        def work():
            try:
                rows = self.stream.sweep(favorites_only=favorites)
                self.events.put(("sweep", (rows, self.stream.last_error, time.time())))
            except Exception as exc:
                self.events.put(("sweep", ([], "Quét sảnh lỗi (%s)." % type(exc).__name__, time.time())))

        threading.Thread(target=work, daemon=True, name="ba-sweep").start()
        self._push_state()
        return {"ok": True, "note": self.sweep_note}

    def lobby_back(self):
        if self._busy():
            return {"ok": False, "error": "Đang bận, chờ một chút."}
        if not self._cdp_available():
            return {"ok": False, "error": "Chưa gắn Chrome CDP."}
        self.sweep_busy = True

        def work():
            try:
                self.events.put(("cycle_back", self.stream.back_to_lobby()))
            except Exception as exc:
                self.events.put(("cycle_back", (False, "Không về được sảnh (%s)."
                                                % type(exc).__name__)))

        threading.Thread(target=work, daemon=True, name="ba-back").start()
        self._push_state()
        return {"ok": True, "note": "Đang rời bàn…"}

    # ------------------------------------------------------------------- bàn
    def open_table(self, key):
        """Mở bàn từ sảnh (bấm đúng ô bàn) rồi chờ xác nhận cầu trước khi theo dõi."""
        if self.open_table_busy or self.sweep_busy:
            return {"ok": False, "error": "Đang bận, chờ một chút."}
        row = self.tables.get(key)
        if not row:
            return {"ok": False, "error": "Không tìm thấy bàn trong danh sách đã quét."}
        if not row.get("table_id"):
            return {"ok": False, "error": "Bàn chưa có mã bàn nên không mở được từ sảnh."}
        if row_problem(row):
            return {"ok": False, "error": "Bàn chưa đạt: " + row_problem(row)}
        self.open_table_busy = True
        self.auto_start_letters = row["letters"]
        self.sweep_note = "Đang mở %s…" % row["label"]

        def work():
            try:
                ok, note = self.stream.open_table(row["table_id"])
                self.events.put(("table_opened", (ok, note, key)))
            except Exception as exc:
                self.events.put(("table_opened", (False, "Không mở được bàn (%s)."
                                                  % type(exc).__name__, key)))

        threading.Thread(target=work, daemon=True, name="ba-open-table").start()
        self._push_state()
        return {"ok": True, "note": self.sweep_note}

    def select_table(self, key):
        """Theo dõi ngay bàn đã đọc được (không cần mở tab bàn)."""
        row = self.tables.get(key)
        if not row:
            return {"ok": False, "error": "Không tìm thấy bàn trong danh sách đã quét."}
        with self.lock:
            self.sim.bind(row)
            started = self.sim.start(row, verified=True)
            note = self.sim.note
        self.chip_live = None
        self.chip_read_at = 0.0
        self._save_state()
        self.log(("Bắt đầu theo dõi %s · %s" % (row["label"], note)) if started
                 else ("Chưa theo dõi được %s · %s" % (row["label"], note)),
                 "info" if started else "error")
        self._push_state()
        return {"ok": True, "note": note, "started": bool(started)}

    def leave_table(self, take_profit=False):
        """Rời bàn hiện tại về sảnh; take_profit=True nghĩa là đã chốt lời."""
        take_profit = bool(take_profit or getattr(self.sim, "take_profit_done", False))
        filter_exit = bool(getattr(self.sim, "filter_exit_done", False))
        if not take_profit and not filter_exit and not self.config["auto_cycle"]:
            return {"ok": False, "error": "Đang tắt chu trình bàn nên không tự rời bàn."}
        self.cycle_waiting = True
        self.cycle_attempts = 0
        self.cycle_table_key = self.sim.table_key
        self.cycle_retry_at = 0.0
        with self.lock:
            self.sim.pause("Đang rời bàn…")
        return self.lobby_back()

    # ------------------------------------------------------------------ tool
    def tool_start(self):
        if self.tool_running:
            return {"ok": True, "note": "Tool đang chạy."}
        if not self.browser.connected:
            return {"ok": False, "error": "Chưa mở Chrome. Bấm “Mở Chrome” và vào sảnh trước."}
        self.tool_running = True
        self.next_scan = 0.0
        self.session_lost = False
        self.log("Chạy tool Baccarat: đọc bàn, chốt kế hoạch và %s." %
                 ("tự đặt chip thật" if self.config["auto_place"] else "chỉ mô phỏng (không bấm chip)"))
        self._push_state()
        return {"ok": True, "note": "Tool đang chạy."}

    def tool_stop(self):
        self.tool_running = False
        self.cycle_waiting = False
        self.cycle_retry_at = 0.0
        self.log("Đã dừng tool Baccarat.")
        self._push_state()
        return {"ok": True, "note": "Đã dừng tool."}

    # -------------------------------------------------------------- đọc chip
    def chip_check(self):
        plan = self.sim.pending or self.sim.plan()
        unit = (self.chip_live or {}).get("unit") or TB.DEFAULT_CHIP_UNIT
        try:
            return ME.compare_chips(plan.get("side"), plan.get("stake") or 0,
                                    self.chip_live, unit=unit)
        except Exception:
            return None

    def scan(self):
        """Quét bàn một lần (đồng bộ) rồi trả snapshot — dùng cho CLI/test."""
        try:
            reports = self.browser.scan() or []
        except Exception as exc:
            self.log("Quét bàn lỗi: %s" % type(exc).__name__, "error")
            return self.snapshot()
        rows = []
        for report in reports:
            for table in (report.get("tables") or []):
                rows.append(table)
        self._ingest(rows)
        with self.lock:
            if self.sim.table_key:
                self.sim.process(self.tables.get(self.sim.table_key))
        return self.snapshot()

    def scan_now(self):
        """Đọc lại cầu bàn ngay (nút “Đọc lại”)."""
        if not self.browser.connected:
            return {"ok": False, "error": "Hãy mở Chrome trước khi đọc dữ liệu."}
        if self.scan_busy:
            return {"ok": False, "error": "Đang đọc bàn, chờ một chút."}
        self.next_scan = 0.0
        self._scan()
        return {"ok": True, "note": "Đang đọc lại cầu bàn…"}

    def chips_read(self):
        if self.chip_busy:
            return {"ok": False, "error": "Đang đọc chip."}
        if not self.browser.connected or not self.sim.table_key:
            return {"ok": False, "error": "Chưa ở trong bàn để đọc chip."}
        self.chip_busy = True

        def work():
            self.events.put(("chips", self._read_chip_state()))

        threading.Thread(target=work, daemon=True, name="ba-chips").start()
        return {"ok": True, "note": "Đang đọc chip…"}

    def _read_chip_state(self):
        try:
            return TB.read(self.browser, unit=TB.DEFAULT_CHIP_UNIT)
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    # ------------------------------------------------------- đặt chip tự động
    def _place_armed_bet(self, plan):
        """Hook ``sim.on_arm``: đặt chip thật theo kế hoạch (chạy ở thread nền)."""
        if not self.config["auto_place"]:
            with self.lock:
                self.sim.mark_placing(plan)
                self.sim.mark_placed(False, "Chưa đặt: bật “Tự đặt chip” để chỉ tính tay đã được xác nhận.",
                                     plan)
            self.place_status = {"ok": False, "note": "Tay chưa đặt: đang tắt tự đặt chip."}
            return
        side, stake = plan.get("side"), float(plan.get("stake") or 0)
        with self.lock:
            self.sim.mark_placing(plan)
        if not side or stake <= 0:
            with self.lock:
                self.sim.mark_placed(False, "Chưa đặt: kế hoạch cược không hợp lệ.", plan)
            return
        if self.place_busy:
            with self.lock:
                self.sim.mark_placed(False, "Lượt bấm chip trước chưa xong nên tay này chưa được bấm.",
                                     plan)
            return
        self.place_busy = True
        self.place_status = {"ok": None,
                            "note": "Đang tự đặt %s VN2 vào %s…" % (M.fmt_money(stake),
                                                                   E.SIDE_NAME.get(side, side))}

        def work():
            try:
                ok_gate, gate_note = self._wait_for_next_hand()
                if not ok_gate:
                    self.events.put(("placed", (False, gate_note, plan)))
                    return
                row_letters = (self.tables.get(self.sim.table_key) or {}).get("letters")
                if row_letters and row_letters != plan.get("baseline"):
                    self.events.put(("placed", (False, "Kết quả tay này đã về trước khi bấm chip; "
                                                       "không bấm sang cửa sổ tay sau.", plan)))
                    return

                def record_click():
                    plan["placed_at"] = time.time()

                ok, note, _placed = TB.place_bet(
                    self.browser, side, [], unit=TB.DEFAULT_CHIP_UNIT,
                    human_mouse=self.config["human_mouse"], amount=stake,
                    on_bet_click=record_click)
                self.events.put(("placed", (ok, note, plan)))
            except Exception as exc:
                self.events.put(("placed", (False, "Lỗi khi tự đặt chip (%s)." % type(exc).__name__,
                                            plan)))

        threading.Thread(target=work, daemon=True, name="ba-place").start()

    def _wait_for_next_hand(self, timeout=BET_GATE_TIMEOUT, poll=BET_GATE_POLL):
        """Chờ bàn gom hết chip ván trước rồi mới bấm (tránh bấm chồng ván cũ)."""
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if not self.tool_running:
                return False, "Tool đã dừng, huỷ tự đặt chip."
            live = self._read_chip_state()
            if table_is_clear(live):
                return True, ""
            last_error = "" if (live or {}).get("ok") else ((live or {}).get("error") or "no_read")
            time.sleep(poll)
        if last_error:
            return False, ("Không tự đặt: sau %ds vẫn không đọc được bàn (%s) nên chưa bấm chip."
                           % (int(timeout), last_error))
        return False, ("Không tự đặt: sau %ds bàn vẫn còn chip ván trước, chưa chắc đang ở lượt "
                       "đặt cược mới." % int(timeout))

    # ------------------------------------------------------------- cấu hình
    def config_set(self, params):
        params = params or {}
        changed = []
        with self.lock:
            config = dict(self.config)
            if "profile" in params:
                profile = params["profile"]
                if profile in ME.PROFILES and profile != config["profile"]:
                    config["profile"] = profile
                    config["ladder"] = None      # về thang mặc định của phương pháp mới
                    changed.append("profile")
            for key in ("min_hands", "max_hands"):
                if key in params:
                    config[key] = params[key]
                    changed.append(key)
            for key in ("max_streak", "min_lead", "max_lead", "tie_side", "live_exit_streak"):
                if key in params:
                    config[key] = params[key]
                    changed.append(key)
            if "block_mode" in params:
                config["block_mode"] = params["block_mode"]
                changed.append("block_mode")
            if "ladder_mode" in params:
                config["ladder_mode"] = params["ladder_mode"]
                changed.append("ladder_mode")
            if "ladder_base" in params:
                config["ladder_base"] = params["ladder_base"]
                changed.append("ladder_base")
            if isinstance(params.get("ladder"), list) and params["ladder"]:
                try:
                    ME.ladder_stakes(config["profile"], params["ladder"])
                except ValueError as exc:
                    return {"ok": False, "error": str(exc)}
                config["ladder"] = params["ladder"]
                changed.append("ladder")
            if (config["ladder_mode"] == "multiply" and "ladder" not in changed
                    and ("ladder_base" in changed or "ladder_mode" in changed)):
                config["ladder"] = ME.multiply_ladder(config["ladder_base"], config["profile"])
            for key in ("favorites_only", "human_mouse", "auto_place", "auto_cycle",
                        "auto_hunt", "auto_recover"):
                if key in params:
                    config[key] = bool(params[key])
                    changed.append(key)
            if "tables_to_play" in params:
                config["tables_to_play"] = params["tables_to_play"]
                changed.append("tables_to_play")
            if "start_url" in params:
                config["start_url"] = params["start_url"]
                changed.append("start_url")

            normalized = self._normalize_config(config)
            # đổi vốn / chốt lời chỉ khi chưa có tay nào, tránh làm sai lãi/lỗ
            for key in ("capital", "take_profit_pct"):
                if key in params:
                    value = _as_float(params[key], normalized[key])
                    if key == "capital" and value < float(normalized["ladder"][0]):
                        return {"ok": False,
                                "error": "Vốn phải ≥ bậc 1 (%s VN2)." % M.fmt_money(normalized["ladder"][0])}
                    if self.sim.money.hands_played and abs(value - float(self.config[key])) >= 0.005:
                        return {"ok": False,
                                "error": "Phiên đã có %d tay; không đổi %s giữa phiên. "
                                         "Dùng “Phiên mới” nếu muốn áp dụng."
                                         % (self.sim.money.hands_played,
                                            "vốn" if key == "capital" else "chốt lời")}
                    normalized[key] = value
                    changed.append(key)

            self.config = normalized
            # áp vào model
            self.sim.profile = normalized["profile"]
            self.sim.ladder = [float(s) for s in normalized["ladder"]]
            self.sim.set_road_filter(normalized["min_hands"], normalized["max_hands"],
                                     normalized["max_streak"], normalized["min_lead"])
            self.sim.set_lead_range(normalized["min_lead"], normalized["max_lead"])
            self.sim.set_tie_side(normalized["tie_side"])
            self.sim.set_live_exit_streak(normalized["live_exit_streak"])
            self.sim.block_mode = normalized["block_mode"]
            book = self.sim.money
            book.take_profit_pct = normalized["take_profit_pct"]
            if not book.hands_played:
                book.unit = float(normalized["ladder"][0])
                book.capital = book.start_capital = book.balance = normalized["capital"]
                book.peak = normalized["capital"]
                book.max_drawdown = 0.0
            book.check_limits()
            self.stream.human_mouse = normalized["human_mouse"]
            self._save_state()

        self.log("Đã lưu cấu hình: %s." % (", ".join(sorted(set(changed))) or "không đổi"))
        self._push_state()
        return {"ok": True, "note": "Đã lưu cấu hình.", "changed": sorted(set(changed))}

    # ---------------------------------------------------------------- phiên
    def session_reset(self):
        """Reset phiên: xoá lịch sử tay, đưa thang về bậc 1 (giữ cấu hình)."""
        with self.lock:
            stakes = [float(s) for s in self.config["ladder"]]
            book = M.MoneyBook(capital=self.config["capital"], unit=stakes[0],
                               take_profit_pct=self.config["take_profit_pct"])
            sim = LiveSimulation(book, profile=self.config["profile"], ladder=stakes,
                                 min_hands=self.config["min_hands"],
                                 max_hands=self.config["max_hands"],
                                 max_streak=self.config["max_streak"],
                                 live_exit_streak=self.config["live_exit_streak"],
                                 min_lead=self.config["min_lead"],
                                 block_mode=self.config["block_mode"],
                                 max_lead=self.config["max_lead"],
                                 tie_side=self.config["tie_side"])
            sim.on_arm = self._place_armed_bet
            self.sim = sim
            self.tables_done = 0
            self.cycle_waiting = False
            self.cycle_retry_at = 0.0
            self.cycle_table_key = None
            self.auto_start_key = None
            self.chip_live = None
        self._save_state()
        self.log("Đã reset phiên Baccarat (vốn %s VN2)." % M.fmt_money(self.config["capital"]))
        self._push_state()
        return {"ok": True, "note": "Đã reset phiên."}

    def session_new(self, params):
        """Tạo phiên mới từ vốn/chốt lời/thang/phương pháp mới (lưu bản sao state cũ)."""
        params = params or {}
        with self.lock:
            config = dict(self.config)
            for key in ("capital", "take_profit_pct", "profile", "ladder", "ladder_mode",
                        "ladder_base", "block_mode", "min_hands", "max_hands", "max_streak",
                        "min_lead", "max_lead", "tie_side", "live_exit_streak"):
                if key in params:
                    config[key] = params[key]
            if config.get("ladder_mode") == "multiply" and ("ladder" not in params):
                config["ladder"] = ME.multiply_ladder(config.get("ladder_base", 100),
                                                      config.get("profile", ME.DEFAULT_PROFILE))
            config = self._normalize_config(config)
            if config["capital"] < float(config["ladder"][0]):
                return {"ok": False,
                        "error": "Vốn phải ≥ bậc 1 (%s VN2)." % M.fmt_money(config["ladder"][0])}
            # lưu bản sao phiên cũ trước khi tạo mới
            try:
                if self.state_path.exists():
                    archive = self.state_path.with_name(
                        "bcr-session-%s-%d.json" % (time.strftime("%Y%m%d-%H%M%S"),
                                                    time.time_ns()))
                    archive.write_bytes(self.state_path.read_bytes())
            except OSError:
                pass
            self.config = config
            stakes = [float(s) for s in config["ladder"]]
            book = M.MoneyBook(capital=config["capital"], unit=stakes[0],
                               take_profit_pct=config["take_profit_pct"])
            sim = LiveSimulation(book, profile=config["profile"], ladder=stakes,
                                 min_hands=config["min_hands"], max_hands=config["max_hands"],
                                 max_streak=config["max_streak"],
                                 live_exit_streak=config["live_exit_streak"],
                                 min_lead=config["min_lead"], block_mode=config["block_mode"],
                                 max_lead=config["max_lead"], tie_side=config["tie_side"])
            sim.on_arm = self._place_armed_bet
            self.sim = sim
            self.tables_done = 0
            self.cycle_waiting = False
            self.cycle_retry_at = 0.0
            self.chip_live = None
        self._save_state()
        self.log("Đã tạo phiên mới: vốn %s VN2 · chốt lời %.0f%% · %s." % (
            M.fmt_money(self.config["capital"]), self.config["take_profit_pct"],
            self.config["profile"]))
        self._push_state()
        return {"ok": True, "note": "Đã tạo phiên mới."}

    def session_recover(self):
        if self.recover_busy:
            return {"ok": False, "error": "Đang phục hồi phiên."}
        if not self.config["auto_recover"]:
            return {"ok": False, "error": "Đang tắt tự phục hồi phiên."}
        self._try_recover_session(force=True)
        return {"ok": True, "note": "Đang mở lại EVO…"}

    def _try_recover_session(self, force=False):
        if not force and (self.recover_busy or not self.config["auto_recover"]):
            return
        if self.recover_attempts >= 3:
            self.sweep_note = ("Đã tự mở lại EVO 3 lần nhưng phiên vẫn bị văng. Hãy đăng nhập lại "
                               "trang gốc rồi bấm “Nhận tab sảnh”.")
            self._push_state()
            return
        self.recover_busy = True
        self.recover_attempts += 1
        self.sweep_note = "Phát hiện văng phiên · đang tự mở lại EVO (lần %d/3)…" % self.recover_attempts

        def work():
            try:
                ok, note = self.stream.open_lobby_from_portal()
                if not ok:
                    reloaded, reload_note = self.stream.reload_portal()
                    if reloaded:
                        ok, note = self.stream.open_lobby_from_portal()
                    else:
                        note = reload_note or note
            except Exception as exc:
                ok, note = False, "Không mở lại được EVO (%s)." % type(exc).__name__
            self.events.put(("recovered", (ok, note)))

        threading.Thread(target=work, daemon=True, name="ba-recover").start()
        self._push_state()

    # ----------------------------------------------------------------- vòng
    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ba-loop")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.tool_running = False

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.log("Lỗi vòng đọc: %s" % type(exc).__name__, "error")
            self._stop.wait(self.interval)

    def _drain(self):
        while True:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                return
            self._handle(kind, data)

    def _handle(self, kind, data):
        if kind == "scan":
            self._handle_scan(*data)
        elif kind == "opened":
            self.chrome_busy = False
            self.connection_text = str(data)
            self.next_scan = 0.0
            self.sweep_note = str(data)
            self._on_chrome_ready()
            self.log("Chrome: " + str(data))
        elif kind == "open_error":
            self.chrome_busy = False
            self.connection_text = str(data)
            with self.lock:
                self.sim.pause("Chưa kết nối Chrome. Phiên theo dõi đã dừng.")
            self.tables.clear()
            self.log(str(data), "error")
        elif kind == "stopped":
            self.chrome_busy = False
            self.connection_text = str(data)
            self.tables.clear()
            self.chip_live = None
        elif kind == "lobby_entered":
            self.sweep_busy = False
            ok, note = data
            self.sweep_note = note
            if ok:
                self.session_lost = False
                self.next_scan = 0.0
                self.log("Đã nhận tab sảnh Baccarat.")
            else:
                self.log(note, "error")
        elif kind == "sweep":
            self.sweep_busy = False
            rows, error, observed_at = data
            self._on_sweep(rows, error, observed_at)
        elif kind == "table_opened":
            self.open_table_busy = False
            ok, note, key = data
            self.sweep_note = note
            if ok and key in self.tables:
                self.session_lost = False
                with self.lock:
                    self.sim.bind(self.tables[key])
                self.auto_start_key = key
                self.auto_start_deadline = time.time() + ENTRY_WAIT
                self.sweep_note = "Đã vào bàn · đang xác nhận dữ liệu…"
                self.next_scan = 0.0
                self.chip_live = None
                self.chip_read_at = 0.0
                self.log("Đã mở bàn %s." % self.tables[key]["label"])
            elif not ok:
                if self._auth_blame():
                    self._session_lost(note)
                else:
                    self._schedule_cycle_retry(note, exclude_key=key)
                self.log(note, "error")
        elif kind == "cycle_back":
            self.sweep_busy = False
            self._on_cycle_back(*data)
        elif kind == "chips":
            self.chip_busy = False
            self.chip_live = data
        elif kind == "placed":
            self.place_busy = False
            ok, note, plan = data
            with self.lock:
                self.sim.mark_placed(ok, note, plan)
            self.place_status = {"ok": bool(ok), "note": note}
            self.log(("Đặt chip OK: " + note) if ok else ("Chưa đặt được chip: " + note),
                     "win" if ok else "error")
        elif kind == "recovered":
            self.recover_busy = False
            ok, note = data
            if ok:
                self.session_lost = False
                self.recover_attempts = 0
                self.sweep_note = "Đã mở lại EVO · quét sảnh để tìm bàn…"
                self.lobby_sweep()
            else:
                self.sweep_note = "Tự phục hồi chưa xong: " + note
                self.cycle_waiting = True
                self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
                self.log(self.sweep_note, "error")
        elif kind == "window_applied":
            self.chrome_busy = False
            ok, note = data
            self.sweep_note = note if ok else ("Đã lưu cấu hình. " + note)

    def _on_sweep(self, rows, error, observed_at):
        if error and self._auth_blame():
            self._session_lost("Quét sảnh không thành công: " + error)
            return
        if error:
            self.sweep_note = "Quét sảnh không thành công: " + error
            self.log(self.sweep_note, "error")
            self._schedule_cycle_retry(self.sweep_note)
            return
        if not rows:
            self.sweep_note = ("Không có bàn nào trong mục Ưa thích đọc được lịch sử. Thêm bàn vào "
                               "Ưa thích trên sảnh rồi quét lại."
                               if self.config["favorites_only"] else
                               "Không đọc được lịch sử bàn ở sảnh. Mở tab sảnh Baccarat rồi bấm "
                               "“Nhận tab sảnh” để app tự quét tiếp.")
            if not self.cycle_waiting and self.config["auto_hunt"] and not self.session_lost:
                self.cycle_waiting = True
                self.cycle_attempts = 0
                self.cycle_table_key = None
            self._schedule_cycle_retry(self.sweep_note)
            return
        self.session_lost = False
        self._ingest(rows, observed_at)
        if self.config["favorites_only"]:
            allowed = {"id:" + row["table_id"] for row in rows if row.get("table_id")}
            self.tables = {k: v for k, v in self.tables.items()
                           if k in allowed or k == self.sim.table_key}
        matched = sum(1 for row in rows if row.get("road_match"))
        self.sweep_note = "Đã quét %d bàn%s · %d bàn khớp lịch sử." % (
            len(rows), " trong mục Ưa thích" if self.config["favorites_only"] else "", matched)
        self.log(self.sweep_note)
        # tự mở bàn nếu đang săn và chưa vào bàn nào
        if not self.sim.running:
            self.auto_open_table(allow_sweep=self.cycle_waiting,
                                 exclude_key=self.cycle_table_key if self.cycle_waiting else None)

    def _on_cycle_back(self, ok, note):
        self.sweep_note = note
        take_profit = bool(getattr(self.sim, "take_profit_done", False))
        if take_profit:
            if ok:
                self.cycle_waiting = False
                self.cycle_retry_at = 0.0
                self.cycle_table_key = None
                self.tables_done += 1
                with self.lock:
                    self.sim.table_key = None
                    self.sim.table_name = "Chưa chọn bàn"
                    self.sim.letters = ""
                    self.sim.take_profit_done = False
                if self._session_complete():
                    self.sweep_note = ("Đã đạt mục tiêu lãi %.0f%% vốn (%s) · đã đóng bàn và dừng "
                                       "phiên (xong %d/%d bàn)." % (
                                           self.sim.money.take_profit_pct,
                                           M.fmt_money(self.sim.money.profit),
                                           self.tables_done, self._tables_target()))
                    self.log(self.sweep_note, "win")
                else:
                    self._start_next_table()
            elif self._auth_blame():
                self._session_lost(note)
            else:
                self.cycle_waiting = True
                self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
                self.sweep_note = "%s · sẽ thử đóng bàn lại sau %d giây." % (note, CYCLE_RETRY_SECONDS)
            return
        if ok:
            self.lobby_sweep()
        elif self._auth_blame():
            self._session_lost(note)
        else:
            self._schedule_cycle_retry(note)

    def _start_next_table(self):
        book = self.sim.money
        balance = book.rebase_to_balance()
        with self.lock:
            self.config["capital"] = float(balance)
        self.sim.take_profit_done = False
        self.sim.note = "Bàn trước đã chốt lời · vốn mới %s · đang tìm bàn kế tiếp." % \
                        M.fmt_money(balance)
        self.cycle_waiting = True
        self.cycle_attempts = 0
        self.cycle_retry_at = 0.0
        self.cycle_table_key = None
        self.sweep_note = "Bàn %d/%d đã chốt lời · vốn mới %s · đang quét tìm bàn kế tiếp…" % (
            self.tables_done, self._tables_target(), M.fmt_money(balance))
        self._save_state()
        self.lobby_sweep()

    def _session_lost(self, note):
        self.session_lost = True
        self.cycle_waiting = False
        self.cycle_attempts = 0
        self.cycle_retry_at = 0.0
        self.cycle_table_key = None
        self.auto_start_key = None
        with self.lock:
            self.sim.pause("Sảnh báo phiên Evolution không còn hợp lệ. Đăng nhập lại trang gốc, "
                           "bấm thẻ Evolution một lần rồi bấm “Nhận tab sảnh”.")
        self.sweep_note = note
        self.log("Mất phiên: " + note, "error")
        self._try_recover_session()

    def _schedule_cycle_retry(self, reason, exclude_key=None):
        if self.session_lost or self.tool_running is False:
            return False
        if not (self.cycle_waiting and self.config["auto_hunt"]):
            return False
        if exclude_key:
            self.cycle_table_key = exclude_key
        hunting = not (getattr(self.sim, "take_profit_done", False)
                       or getattr(self.sim, "filter_exit_done", False))
        first_attempt = hunting and self.cycle_attempts == 0
        self.cycle_attempts += 1
        wait = 0 if first_attempt else CYCLE_RETRY_SECONDS
        self.cycle_retry_at = time.time() + wait
        self.sweep_note = ("%s · quét lại ngay…" % reason if first_attempt else
                          "%s · tự quét lại sau %d giây (đã thử %d lượt)." % (
                              reason, CYCLE_RETRY_SECONDS, self.cycle_attempts))
        return True

    def _ingest(self, rows, now=None):
        """Gộp kết quả quét/scan vào bảng bàn, giữ độ "ổn định" của cầu."""
        now = time.time() if now is None else now
        incoming = {}
        for raw in rows:
            letters = raw.get("letters", "")
            label = " ".join(str(raw.get("label") or raw.get("title") or "").split())[:80]
            if not label or not isinstance(letters, str):
                continue
            if any(ch not in "BPT" for ch in letters) or len(letters) > 400:
                continue
            table_id = raw.get("table_id") or raw.get("id") or ""
            if not isinstance(table_id, str) or not table_id or len(table_id) > 80:
                table_id = ""
            key = "id:" + table_id if table_id else "".join(label.split()).casefold()
            previous = incoming.get(key)
            old = self.tables.get(key)
            conflict = bool(previous and previous["letters"] != letters)
            incoming[key] = {
                "key": key, "label": label, "letters": letters, "ts": now,
                "table_id": table_id, "game_type": raw.get("game_type", ""),
                "read_error": raw.get("read_error", ""),
                "source": raw.get("source", ""), "conflict": conflict,
            }
            if raw.get("road_match") is True:
                incoming[key]["ws_confirmed_letters"] = letters
            elif old:
                confirmed = old.get("ws_confirmed_letters", "")
                incoming[key]["ws_confirmed_letters"] = confirmed if (
                    letters.startswith(confirmed) or confirmed.startswith(letters)) else ""
        for key, row in incoming.items():
            old = self.tables.get(key)
            row["stable"] = (old["stable"] + 1
                             if old and old["letters"] == row["letters"]
                             and not (old["conflict"] or row["conflict"]
                                      or now - old["ts"] > LIVE_STALE) else 1)
            row["screen_stable"] = (old.get("screen_stable", 0) + 1
                                    if row["source"] == "evo-bead" and old
                                    and old.get("source") == "evo-bead"
                                    and old["letters"] == row["letters"]
                                    else (1 if row["source"] == "evo-bead" else 0))
            self.tables[key] = row
        self.tables = {k: r for k, r in self.tables.items()
                       if now - r["ts"] < 300 or k == self.sim.table_key}

    # ------------------------------------------------------------ tự săn bàn
    def auto_open_table(self, allow_sweep=False, exclude_key=None):
        if self._busy() or not self.tool_running:
            return
        if self.sim.running or getattr(self.sim, "take_profit_done", False):
            return
        candidates = []
        skipped_fast = 0
        for row in list(self.tables.values()):
            if not row.get("table_id") or row["key"] == exclude_key or row_problem(row):
                continue
            if not road_is_current(row):
                continue
            if is_fast_table(row.get("label")):
                skipped_fast += 1
                continue
            info = ME.check_table(list(row["letters"]), self.sim.min_hands, self.sim.max_hands,
                                  self.sim.max_streak, self.sim.min_lead,
                                  max_lead=self.sim.max_lead, tie_side=self.sim.tie_side)
            if info["eligible"]:
                candidates.append((info["hands"], row, info))
        if not candidates:
            if not self.cycle_waiting and self.config["auto_hunt"]:
                self.cycle_waiting = True
                self.cycle_attempts = 0
                self.cycle_table_key = exclude_key
            reason = "Chưa có bàn đạt 4 điều kiện"
            if skipped_fast:
                reason += " · bỏ qua %d bàn Siêu Tốc Độ" % skipped_fast
            if self._schedule_cycle_retry(reason, exclude_key=exclude_key):
                return
            if self.cycle_waiting or not allow_sweep:
                self.sweep_note = ("Chưa có bàn đạt điều kiện · bấm “Quét sảnh” để thử lại, hoặc bật "
                                   "“Tự săn bàn” để app tự lặp tới khi vào được.")
                return
            self.lobby_sweep()
            return
        ideal = (self.sim.min_hands + self.sim.max_hands) / 2.0
        candidates.sort(key=lambda item: (abs(item[0] - ideal), item[1]["label"].casefold()))
        hands, row, info = candidates[0]
        self.sweep_note = "Đang mở %s (%d tay, bên mạnh %s)…" % (
            row["label"], hands, E.SIDE_NAME.get(info["strong"], "—"))
        self.open_table(row["key"])

    def _auto_start_verified_table(self):
        key = self.auto_start_key
        if not key:
            return
        row = self.tables.get(key)
        if not row or row.get("source") != "evo-bead" or row.get("screen_stable", 0) < 2:
            if time.time() > self.auto_start_deadline:
                self.sweep_note = ("Vào bàn rồi nhưng %d giây chưa đọc được cầu bàn hai lần giống "
                                   "nhau. Đang tìm bàn khác…" % ENTRY_WAIT)
                self.auto_start_key = None
                self._cycle_search(key)
            return
        problem = row_problem(row)
        if problem or not same_shoe(self.auto_start_letters, row["letters"]):
            self.sweep_note = ("Chưa tự bắt đầu: " + problem) if problem else (
                "Cầu trong bàn (%d tay) không cùng shoe với cầu đã lọc ở sảnh (%d tay)." % (
                    len(row["letters"]), len(self.auto_start_letters)))
            self.auto_start_key = None
            self._cycle_search(key)
            return
        with self.lock:
            started = self.sim.arm_at_entry(row)
            note = self.sim.note
        if started:
            self.sweep_note = "Đã vào bàn và bắt đầu theo dõi · %s" % note
            self.auto_start_key = None
            self.cycle_waiting = False
            self.cycle_attempts = 0
            self.cycle_retry_at = 0.0
            self.cycle_table_key = None
            self.log("Bắt đầu theo dõi %s." % row["label"])
        else:
            self.sweep_note = "Chưa thể bắt đầu: " + note
            self.auto_start_key = None
            self._cycle_search(key)

    def _cycle_search(self, key):
        if self.cycle_waiting:
            self.auto_open_table(allow_sweep=True, exclude_key=key)

    def _resume_cycle_hunt(self, now):
        take_profit = bool(getattr(self.sim, "take_profit_done", False))
        filter_exit = bool(getattr(self.sim, "filter_exit_done", False))
        if not (self.cycle_waiting and (self.config["auto_hunt"] or take_profit or filter_exit)
                and self.cycle_retry_at and now >= self.cycle_retry_at):
            return
        self.cycle_retry_at = 0.0
        if self.sim.running or self.auto_start_key:
            return
        if take_profit or filter_exit:
            self.leave_table(take_profit=take_profit)
            return
        if self.session_lost:
            self._try_recover_session()
            if self.session_lost:
                self.cycle_retry_at = now + CYCLE_RETRY_SECONDS
            return
        if self._busy() or not self._cdp_available():
            self.cycle_retry_at = now + CYCLE_RETRY_SECONDS
            return
        self.sweep_note = "Đang tự quét tìm bàn đạt chuẩn (lượt %d)…" % (self.cycle_attempts + 1)
        self.lobby_sweep()

    # --------------------------------------------------------------- nhịp làm việc
    def _scan(self):
        """Đọc cầu bàn đang mở (CDP) để tiến trình cược chạy tiếp."""
        if self.scan_busy:
            return
        self.scan_busy = True

        def work():
            try:
                reports = self.browser.scan()
                self.events.put(("scan", (reports, self.browser.last_error, time.time())))
            except Exception as exc:
                self.events.put(("scan", ([], str(exc).splitlines()[0][:180], time.time())))

        threading.Thread(target=work, daemon=True, name="ba-scan").start()

    def _handle_scan(self, reports, error, observed_at):
        self.scan_busy = False
        if error:
            self.connection_text = "Đọc cầu chưa thành công: " + error
            if self._auth_blame():
                self._session_lost(error)
                return
            with self.lock:
                self.sim.pause("Mất kết nối/đọc cầu lỗi. Hãy đối chiếu lại trước khi tiếp tục.")
            return
        self.session_lost = False
        self._sync_wallet(reports)
        rows = []
        for report in reports:
            for table in report.get("tables", []):
                rows.append(table)
        if rows:
            self._ingest(rows, observed_at)
        count = len(rows)
        self.connection_text = ("Đang đọc %d bàn." % count if count
                                else "Chưa thấy bàn Baccarat.")
        with self.lock:
            changed = self.sim.process(self.tables.get(self.sim.table_key))
        if changed:
            self._save_state()
            if self.sim.cycle_done:
                self.sim.cycle_done = False
                self.leave_table(take_profit=self.sim.take_profit_done)

    def _sync_wallet(self, reports):
        text = next((str((report.get("page") or {}).get("wallet_balance") or "").strip()
                     for report in reports
                     if (report.get("page") or {}).get("wallet_balance")), "")
        if not text:
            return
        try:
            value = M.parse_money(text)
        except (TypeError, ValueError):
            self.wallet_text = "Số dư ví trên web: không đọc được giá trị"
            return
        self.wallet_text = "Số dư ví trên web: %s VN2" % M.fmt_amount(value)
        book = self.sim.money
        if not self.sim.running and not book.hands_played:
            ok, _note = book.sync_capital(value)
            if ok:
                self.config["capital"] = float(value)
                self.wallet_text += " · vốn phiên đã đồng bộ"

    def _tick(self):
        self._drain()
        if not self.browser.connected:
            self._on_chrome_lost()
        else:
            self._on_chrome_ready()
            if self.tool_running:
                now = time.time()
                if not self.session_lost and now >= self.next_scan and not self._busy():
                    self.next_scan = now + (FAST_SCAN if (self.auto_start_key or self.sim.running)
                                            else SCAN_SECONDS)
                    self._scan()
                if (self.sim.table_key and not self.chip_busy
                        and now - self.chip_read_at >= CHIP_REFRESH):
                    self.chip_read_at = now
                    self.chips_read()      # đọc chip ở thread riêng, không chặn vòng chính
                self._auto_start_verified_table()
                self._resume_cycle_hunt(now)
        self._push_state()

    def _on_chrome_lost(self):
        """Chrome bị đóng (người dùng tắt cửa sổ): dọn danh sách bàn, tạm dừng phiên.

        Nhờ vậy UI thấy ``chrome_connected = false`` ngay và nút “Mở Chrome” bấm lại được;
        chỉ ghi log một lần cho tới khi kết nối lại.
        """
        if self.offline_noted:
            return
        self.offline_noted = True
        had_session = bool(self.tables) or bool(self.sim.running) or bool(self.sim.table_key)
        self.tables.clear()
        self.chip_live = None
        self.chip_read_at = 0.0
        self.auto_start_key = None
        with self.lock:
            if self.sim.running:
                self.sim.pause("Chrome đã đóng · phiên tạm dừng. Bấm “Mở Chrome” để mở lại.")
        self.sweep_note = getattr(self.browser, "note", "") or \
            "Chrome đã đóng · bấm “Mở Chrome” để mở lại."
        self.connection_text = self.sweep_note
        if had_session:
            self.log(self.sweep_note, "error")

    def _on_chrome_ready(self):
        if self.offline_noted:
            self.offline_noted = False
            self.next_scan = 0.0

    # ------------------------------------------------------------- dispatch
    def dispatch(self, method, params):
        params = params or {}
        table = {
            "ping": lambda p: {"ok": True, "pong": True, "game": "baccarat"},
            "state": lambda p: self.snapshot(),
            "chrome.open": lambda p: self.chrome_open(),
            "chrome.attach": lambda p: self.chrome_attach(),
            "chrome.close": lambda p: self.chrome_close(),
            "chrome.window": self.apply_window,
            "chrome.status": lambda p: {"connected": bool(self.browser.connected),
                                        "mode": self.browser.mode,
                                        "note": self.browser.note},
            "lobby.enter": lambda p: self.lobby_enter(),
            "lobby.sweep": self.lobby_sweep,
            "lobby.back": lambda p: self.lobby_back(),
            "tables": lambda p: {"tables": self.table_rows(),
                                 "note": self.sweep_note},
            "table.open": lambda p: self.open_table(p.get("key") or ""),
            "table.select": lambda p: self.select_table(p.get("key") or ""),
            "table.leave": lambda p: self.leave_table(take_profit=bool(p.get("take_profit"))),
            "tool.start": lambda p: self.tool_start(),
            "tool.stop": lambda p: self.tool_stop(),
            "scan": lambda p: self.scan_now(),
            "chips.read": lambda p: self.chips_read(),
            "config.get": lambda p: dict(self.config),
            "config.set": self.config_set,
            "session.reset": lambda p: self.session_reset(),
            "session.new": self.session_new,
            "session.recover": lambda p: self.session_recover(),
            "log.clear": lambda p: {"ok": True},
            "export": lambda p: {"log": list(self.sim.log)},
        }
        handler = table.get(method)
        if handler is None:
            return None, "Method không hỗ trợ: %s" % method
        try:
            return handler(params), None
        except Exception as exc:
            return None, "%s: %s" % (type(exc).__name__, exc)

    def shutdown(self):
        self.stop()
        try:
            self._save_state()
        except Exception:
            pass
        try:
            self.browser.stop()
        except Exception:
            pass
