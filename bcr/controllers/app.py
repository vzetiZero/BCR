# -*- coding: utf-8 -*-
import queue
import threading
import time

import tkinter as tk
from pathlib import Path

from bcr.adapters import browser as BR
from bcr.adapters import lobby_stream as LS
from bcr.config import *
from bcr.models import method as ME
from bcr.models import money as M
from bcr.models.live_sim import LiveSimulation
from bcr.views.main_window import MainWindowMixin
from bcr.views.render import RenderMixin
from bcr.views.longlong_panel import LongLongPanelMixin
from bcr.controllers.state_store import StateStoreMixin
from bcr.controllers.hunt import HuntMixin
from bcr.controllers.browsers import BrowserMixin
from bcr.controllers.betting import BettingMixin
from bcr.controllers.session import SessionMixin
from bcr.controllers.settings import SettingsMixin
from bcr.controllers.longlong_live import LongLongLiveMixin


class BCRApp(MainWindowMixin, LongLongPanelMixin, RenderMixin, StateStoreMixin, HuntMixin,
                 BrowserMixin, BettingMixin, SessionMixin, SettingsMixin, LongLongLiveMixin):
    """Bo dieu khien ung dung: ghep View + Model + Adapter lai va giu vong poll."""
    def __init__(self, root, state_file=STATE_FILE, auto_connect=True):
        self.root, self.state_file = root, Path(state_file)
        self.sim = LiveSimulation()
        self.sim.on_arm = self.place_armed_bet
        self.ladder_store = {}
        self.ladder_shown_profile = None
        self.browser = BR.BrowserSession()
        self.tables = {}
        self.events = queue.Queue()
        self.chrome_busy = self.scan_busy = self.closing = self.exit_requested = False
        self.sweep_busy = self.open_table_busy = False
        self.auto_start_table_key = None
        self.auto_start_letters = ""
        self.auto_start_deadline = 0.0
        self.cycle_waiting = False
        self.cycle_attempts = 0
        self.cycle_retry_at = 0.0
        self.cycle_table_key = None
        # Số bàn đã chốt lời xong trong phiên này; đủ ``tables_var`` bàn thì dừng hẳn.
        self.tables_done = 0
        self.session_lost = False
        self.recover_busy = False
        self.recover_attempts = 0
        self.trace_file = self.state_file.with_name("bcr_trace.jsonl")
        self.chip_live = None
        self.chip_check = None
        self.chip_read_at = 0.0
        self.chip_busy = False
        self.place_busy = False
        self.stream = LS.LobbyStream(self.browser)
        self.sweep_note_text = "Quét sảnh để tìm bàn đạt 3 điều kiện."
        self.next_scan = 0
        self.poll_id = None
        self.state_read_error = False
        self.ui_scale = DEFAULT_UI_SCALE
        self.chrome_startup_var = tk.BooleanVar(value=False)
        # Mặc định BẬT: app tự mở Chrome bằng chromedriver kèm cổng 9222, nên mọi
        # đường đọc (WebSocket sảnh, bấm ô bàn) chạy được mà người dùng không phải
        # giữ hay gắn cửa sổ Chrome nào khác.
        self.cdp_var = tk.BooleanVar(value=True)
        self.start_url_var = tk.StringVar(value=BR.START_URL)
        self.auto_cycle_var = tk.BooleanVar(value=True)
        self.auto_hunt_var = tk.BooleanVar(value=True)
        self.tables_var = tk.StringVar(value="1")
        self.favorites_only = tk.BooleanVar(value=True)
        self.human_mouse_var = tk.BooleanVar(value=True)
        self.game_mode = "baccarat"
        self._longlong_saved = None
        self.longlong_table = None
        self.longlong_live_state = None
        self.auto_place_var = tk.BooleanVar(value=True)
        self.auto_recover_var = tk.BooleanVar(value=True)
        self.ladder_mode_var = tk.StringVar(value="manual")
        self.ladder_base_var = tk.StringVar(value=M.fmt_input_money(ME.DEFAULT_LADDER[ME.DEFAULT_PROFILE][0]))
        self.hands_min_var = tk.StringVar(value=str(ME.MIN_HANDS))
        self.hands_max_var = tk.StringVar(value=str(ME.MAX_HANDS))
        self.streak_var = tk.StringVar(value=str(ME.MAX_STREAK_ALLOWED))
        self.live_exit_streak_var = tk.StringVar(value=str(ME.DEFAULT_LIVE_EXIT_STREAK))
        self.min_lead_var = tk.StringVar(value=str(ME.DEFAULT_MIN_LEAD))
        self.block_mode_var = tk.StringVar(value=ME.BLOCK_MODE_RECALCULATE)
        self._last_sweep_favorites = False
        self.chrome_fullscreen_var = tk.BooleanVar(value=BR.WIN_FULLSCREEN)
        self.chrome_w_var = tk.StringVar(value=str(BR.WIN_W))
        self.chrome_h_var = tk.StringVar(value=str(BR.WIN_H))
        self.chrome_scale_var = tk.StringVar(value=str(int(BR.WIN_SCALE * 100)))
        self.only_eligible = tk.BooleanVar(value=False)
        self.search = tk.StringVar()
        self.connection_text = "Chưa kết nối Chrome."
        self.wallet_balance_text = "Số dư ví trên web: chưa đọc được"
        self._wallet_startup_sync_pending = True
        self._wallet_manual_sync_pending = False
        self._load_state()
        self.stream.human_mouse = self.human_mouse_var.get()
        self.base_tk_scaling = float(root.tk.call("tk", "scaling"))
        root.tk.call("tk", "scaling", self.base_tk_scaling * self.ui_scale / 100)
        root.title("BCR · Theo dõi 3-3 / 3-2")
        root.configure(bg=BG)
        root.geometry("600x900+20+20")
        root.minsize(560, 620)
        self._build_ui()
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(500, self.poll)
        if auto_connect and self.chrome_startup_var.get():
            root.after(700, self.start_browser)

    def poll(self):
        if self.closing:
            return
        while not self.events.empty():
            kind, data = self.events.get_nowait()
            if kind in ("opened", "open_error", "stopped"):
                self.chrome_busy = False
                self.connection_text = data
                self.next_scan = 0
                if kind != "opened":
                    self.sim.pause("Chưa kết nối Chrome. Phiên theo dõi đã dừng.")
                    self.tables.clear()
            elif kind == "scan":
                self.scan_busy = False
                reports, error, observed_at = data
                if error:
                    self.connection_text = "Đọc cầu chưa thành công: " + error
                    self.tables.clear()
                    if self._auth_blame():
                        self._session_lost_step(error)
                        continue
                    self.sim.pause("Mất kết nối/đọc cầu lỗi. Hãy đối chiếu lại trước khi tiếp tục.")
                else:
                    self.session_lost = False
                    self._sync_wallet_from_reports(reports)
                    self._ingest_reports(reports, observed_at)
                    n = sum(len(rep.get("tables", [])) for rep in reports)
                    self.connection_text = ("Đang đọc %d bàn." % n if n else "Chưa thấy bàn Baccarat.")
            elif kind == "sweep":
                self.sweep_busy = False
                rows, error, diag, observed_at = data
                self._trace("sweep", dict(rows=len(rows or []), error=bool(error), diag=diag or {}))
                if error and self._auth_blame():
                    self._session_lost_step("Quét sảnh không thành công: " + error)
                elif error:
                    self.sweep_note_text = "Quét sảnh không thành công: " + error
                    self._schedule_cycle_retry(self.sweep_note_text)
                elif not rows:
                    # Lượt quét không ra bàn nào (kể cả lần quét đầu tiên, trước khi từng
                    # vào bàn): nếu "Tự quét tìm bàn" đang bật, tự bật vòng hunt ngay từ
                    # đây để có banner đếm ngược + tự quét lại, không chỉ dòng chữ tĩnh
                    # bắt người dùng tự bấm lại "Quét toàn sảnh".
                    was_hunting = self.cycle_waiting
                    if not was_hunting and self.auto_hunt_var.get() and not self.session_lost:
                        self.cycle_waiting = True
                        self.cycle_attempts = 0
                        self.cycle_table_key = None
                    self.sweep_note_text = (
                        "Chưa có bàn nào trong mục Ưa thích đọc được lịch sử. Hãy thêm bàn vào Ưa thích "
                        "trên sảnh rồi quét lại."
                        if self._last_sweep_favorites else
                        ("Chưa đọc được lịch sử bàn ở sảnh. Mở tab sảnh Baccarat rồi bấm "
                         "“Nhận tab sảnh” để app tự quét tiếp."
                         if was_hunting else
                         "Chưa nhận được lịch sử. Mở sảnh Baccarat rồi quét lại."))
                    self._schedule_cycle_retry(self.sweep_note_text)
                else:
                    self.session_lost = False
                    self._ingest_reports([{"tables": rows, "page": {}, "diag": {}}], observed_at)
                    for row in rows:
                        stored = self.tables.get("id:" + row["table_id"])
                        # WebSocket và cầu đang vẽ là hai nguồn đọc độc lập cùng cho một chuỗi.
                        if stored and row["road_match"] and stored["stable"] < 2:
                            stored["stable"] = 2
                    # Quét Ưa thích chỉ tin danh sách vừa lọc: bỏ bàn cũ ngoài danh sách
                    # để vòng tự mở bàn không chọn nhầm bàn không còn là ưa thích.
                    if self._last_sweep_favorites:
                        allowed = {"id:" + row["table_id"] for row in rows if row.get("table_id")}
                        self.tables = {key: value for key, value in self.tables.items()
                                       if key in allowed or key == self.sim.table_key}
                    self.sweep_note_text = "Đã quét %d bàn%s · %d bàn khớp lịch sử." % (
                        len(rows), " trong mục Ưa thích" if self._last_sweep_favorites else "",
                        sum(1 for r in rows if r["road_match"]))
                    # Bàn được mở bằng đúng ô bàn của sảnh nên phiên đăng nhập không bị động vào.
                    if not self.sim.running:
                        self.auto_open_table(allow_sweep=self.cycle_waiting,
                                             exclude_key=self.cycle_table_key if self.cycle_waiting else None)
            elif kind == "lobby_entered":
                self.sweep_busy = False
                ok, note = data
                self.sweep_note_text = note
                self._trace("lobby_tab", dict(ok=ok))
                if ok:
                    self.session_lost = False
                    self.next_scan = 0
            elif kind == "window_applied":
                self.chrome_busy = False
                ok, note = data
                self.sweep_note_text = note if ok else "Đã lưu cấu hình. " + note
            elif kind == "cycle_back":
                self.sweep_busy = False
                self._on_cycle_back(*data)
            elif kind == "table_opened":
                self.open_table_busy = False
                ok, note, key = data
                self.sweep_note_text = note
                if ok and key in self.tables:
                    self.session_lost = False
                    self.sim.bind(self.tables[key])
                    self.auto_start_table_key = key
                    self.auto_start_deadline = time.time() + ENTRY_WAIT
                    self.sweep_note_text = "Đã vào bàn · đang xác nhận dữ liệu…"
                    self.next_scan = 0
                    self.chip_live = None
                    self.chip_read_at = 0.0
                elif not ok:
                    self._trace("table_open_failed", dict(key=key))
                    if self._auth_blame():
                        self._session_lost_step(note)
                    else:
                        self._schedule_cycle_retry(note, exclude_key=key or self.cycle_table_key)
            elif kind == "chips":
                self.chip_busy = False
                self.chip_live = data
            elif kind == "placed":
                self._on_placed(*data)
            elif kind == "recovered":
                ok, note = data
                self._on_recovered(ok, note)
        self._auto_start_verified_table()
        if self.sim.process(self.tables.get(self.sim.table_key)):
            self._save_state()
        if self.sim.cycle_done:
            self.sim.cycle_done = False
            # Đủ lãi mới có cờ take_profit_done: rời bàn xong thì dừng hẳn,
            # không quét hay mở bàn mới.
            self.leave_table_for_lobby(take_profit=self.sim.take_profit_done)
        now = time.time()
        # Mất phiên thì dừng hẳn việc đọc/đặt chip: quét tiếp chỉ bấm lên trang đã bị site
        # đá (EV.7) và làm phiên bị đá lặp lại. Chờ người dùng/tự phục hồi mở lại sảnh.
        if not self.session_lost:
            self.request_chip_read()
        if (not self.exit_requested and not self.session_lost and now >= self.next_scan
                and not self.scan_busy and not self.chrome_busy and not self.sweep_busy
                and self.browser.connected):
            self.next_scan = now + (FAST_SCAN if (self.auto_start_table_key or self.sim.running) else SCAN_SECONDS)
            self._scan()
        self._resume_cycle_hunt(now)
        # Tab LONG-LONG: tự bật/tắt vòng đọc số dư thật theo tab + trạng thái Chrome.
        self.longlong_live_autostart()
        self.refresh()
        self.poll_id = self.root.after(500, self.poll)

    def close(self):
        if self.closing:
            return
        self.exit_requested = True
        self.longlong_live_stop()
        self.sim.pause()
        if self.chrome_busy or self.scan_busy or self.sweep_busy or self.open_table_busy:
            self.connection_note.config(text="Đang chờ tác vụ Chrome/Sảnh kết thúc để đóng an toàn…")
            self.root.after(100, self.close)
            return
        self._save_state()
        self.closing = True
        if self.poll_id:
            self.root.after_cancel(self.poll_id)
        self.connection_note.config(text="Đang ngắt kết nối và lưu phiên…" if self.browser.cdp_attached
                                    else "Đang đóng Chrome và lưu phiên…")
        def work():
            try:
                self.browser.stop()
            finally:
                self.events.put(("closed", None))
        threading.Thread(target=work, daemon=True).start()
        def finish():
            while not self.events.empty():
                kind, _ = self.events.get_nowait()
                if kind == "closed":
                    self.root.destroy()
                    return
            self.root.after(100, finish)
        self.root.after(100, finish)

# --- Bề mặt công khai để ``import app`` giữ nguyên API cũ ---------------------------
# Các tên dưới đây không dùng bên trong module nhưng là API cũ mà test_live_app,
# test_longlong và verify_live_ui vẫn đọc qua ``app.<tên>``; import lại (không phải
# định nghĩa mới) nên vẫn là đúng một đối tượng của tầng Model.
from bcr.models.live_sim import (  # noqa: E402,F401
    LiveSimulation,
    fresh_method,
    group_money_input,
    is_fast_table,
    road_is_current,
    row_problem,
    same_shoe,
    signed,
    table_is_clear,
)
# Test cũ patch ``app.messagebox`` để chặn hộp thoại lỗi: module tkinter.messagebox là
# cùng một đối tượng ở mọi nơi, nên chỉ cần tên này tồn tại trên module ``app``.
from tkinter import messagebox  # noqa: E402,F401
