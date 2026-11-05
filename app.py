# -*- coding: utf-8 -*-
import csv
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import browser as BR
import engine as E
import lobby_stream as LS
import method as ME
import money as M

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "bcr_state.json"
SCAN_SECONDS = 4
FAST_SCAN = 1.5
LIVE_STALE = 30
ENTRY_WAIT = 75
BG, PANEL, NAV = "#f3f6fa", "#ffffff", "#111e32"
TEXT, MUTED, LINE = "#172b45", "#718198", "#e5ebf2"
ACCENT, PALE = "#087f78", "#e8f5f2"
RED, BLUE, GREEN = "#d94d62", "#3f7edf", "#16866b"
COLORS = {"B": RED, "P": BLUE, "T": GREEN}
MONEY_FIELDS = ("balance", "hands_played", "wins", "losses", "pushes", "ties_bet",
                "total_staked", "total_returned", "peak", "max_drawdown", "loss_streak", "blocked")
DEFAULT_UI_SCALE = 80


def fresh_method():
    return {"started": False, "ladder_step": 0, "hands_in_block": 0, "strong": None}


def signed(value):
    return ("+" if value > 0 else "") + M.fmt_money(value)


def road_is_current(row):
    """Cầu đủ tin để chọn bàn: sảnh - hai nguồn (cầu đang vẽ + lịch sử WebSocket) cùng cho một chuỗi."""
    if not row or row.get("conflict") or row.get("read_error") or not row.get("letters"):
        return False
    if row.get("source") == "evo-bead":
        return row.get("screen_stable", 0) >= 2
    return row.get("ws_confirmed_letters") == row["letters"] and row.get("stable", 0) >= 2


def same_shoe(entry_letters, screen_letters):
    """Cầu đã lọc ở sảnh và cầu trong bàn là một shoe: phần trùng nhau phải khớp đầu hoặc khớp cuối."""
    if not entry_letters or not screen_letters:
        return False
    shared = min(len(entry_letters), len(screen_letters))
    return (entry_letters[:shared] == screen_letters[:shared]
            or entry_letters[-shared:] == screen_letters[-shared:])


def row_problem(row, now=None, check_fresh=True):
    now = time.time() if now is None else now
    if not row:
        return "Chưa có dữ liệu bàn. Mở Chrome, đăng nhập và chọn một bàn."
    if check_fresh and now - row["ts"] > LIVE_STALE:
        return "Dữ liệu quá %d giây chưa cập nhật. Đã dừng để tránh tính sai." % LIVE_STALE
    if row.get("conflict"):
        return "Có nhiều cầu khác nhau cùng tên bàn. Cần đối chiếu lại."
    if row.get("read_error"):
        return row["read_error"]
    if row.get("game_type") and row["game_type"] != "baccarat":
        return "Bàn trả thưởng đặc biệt: chưa hỗ trợ tính lãi/lỗ theo bàn chuẩn."
    if row.get("stable", 0) < 2:
        return "Chờ hai lần đọc giống nhau để xác nhận dữ liệu."
    if not row.get("table_id") and (row["label"].lower().startswith("ban ") or not re.search(r"\d", row["label"])):
        return "Chưa nhận diện rõ mã bàn. Không thể bắt đầu giả lập."
    return ""


class LiveSimulation:
    def __init__(self, book=None):
        self.money = book or M.MoneyBook()
        self.results = []
        self.log = []
        self.method_state = fresh_method()
        self.table_key = None
        self.table_name = "Chưa chọn bàn"
        self.letters = ""
        self.running = False
        self.pending = None
        self.note = "Chọn bàn để theo dõi."

    def plan(self):
        s = self.method_state
        return ME.plan(self.results, self.money.unit, s["ladder_step"], s["hands_in_block"],
                       s["started"], s["strong"])

    def bind(self, row):
        self.pause()
        self.table_key, self.table_name = row["key"], row["label"]
        self.letters = row["letters"]
        self.results = list(self.letters)
        self.method_state = fresh_method()
        self.note = "Đã chọn bàn."

    def start(self, row, verified=False, now=None):
        problem = row_problem(row, now)
        if problem:
            self.note = problem
            return False
        if row["key"] != self.table_key:
            self.note = "Hãy chọn bàn trước."
            return False
        if row["letters"] != self.letters:
            self.bind(row)
            self.note = "Cầu đã thay đổi trong lúc dừng. Đối chiếu và xác nhận lại."
            return False
        if not verified:
            self.note = "Cần xác nhận cầu và loại bàn Baccarat chuẩn trước khi giả lập."
            return False
        if self.money.blocked:
            self.note = self.money.blocked[1]
            return False
        self.running = True
        self._arm()
        return self.running

    def pause(self, reason="Đã tạm dừng. Không ghi thêm kết quả vào vốn."):
        self.running = False
        self.pending = None
        self.note = reason

    def arm_at_entry(self, row):
        """Vừa vào bàn đạt chuẩn: chốt khối 3-3 ngay trên cầu đang đọc, chỉ tính tay đi sau đó."""
        problem = row_problem(row)
        if problem:
            self.note = problem
            return False
        if self.money.blocked:
            self.note = self.money.blocked[1]
            return False
        self.bind(row)
        self.method_state["started"] = True
        self.running = True
        self._arm()
        return self.running

    def _arm(self):
        plan = self.plan()
        if plan["action"] not in ("start", "bet"):
            self.pause(plan["reason"])
            return
        if plan["stake"] > self.money.balance:
            self.pause("Vốn không đủ cho bậc tiếp theo. Không tự giảm tiền cược.")
            return
        self.pending = dict(plan, armed_at=time.time(), baseline=self.letters)
        self.note = "Chờ %s · %s đ · ván kế tiếp." % (
            E.SIDE_NAME[plan["side"]], M.fmt_money(plan["stake"]))

    def process(self, row, now=None):
        if not self.running:
            return False
        if not row or row["key"] != self.table_key:
            self.pause("Không còn đọc được bàn đang theo dõi. Hãy chọn lại bàn.")
            return False
        letters = row["letters"]
        problem = row_problem(row, now, check_fresh=False)
        if row.get("conflict") or row.get("read_error") or row.get("game_type", "baccarat") not in ("", "baccarat"):
            self.pause(problem)
            return False
        current_time = time.time() if now is None else now
        if current_time - row["ts"] > LIVE_STALE:
            if letters == self.letters:
                return False
            self.pause(row_problem(row, now))
            return False
        # Tay mới phải được đọc giống nhau hai lần mới tính; trong lúc chờ vẫn giữ cược đã chốt.
        if row.get("stable", 0) < 2:
            return False
        if problem:
            self.pause(problem)
            return False
        if letters == self.letters:
            return False
        # Không tính bù các tay đã biết kết quả bằng kế hoạch lập sau đó.
        if not letters.startswith(self.letters) or len(letters) != len(self.letters) + 1:
            self.pause("Cầu nhảy tay, đổi shoe hoặc không khớp. Đã dừng, không tính bù lãi/lỗ.")
            return False
        plan = self.pending
        if not plan or plan["baseline"] != self.letters:
            self.pause("Không có kế hoạch được chốt trước kết quả. Hãy xác nhận lại bàn.")
            return False
        result = letters[-1]
        settled = self.money.settle(plan["side"], result, plan["stake"], E.PAYOUT)
        self.method_state, _ = ME.advance(self.method_state, result, plan["side"], plan["stake"])
        self.method_state["strong"] = plan["strong"]
        self.letters = letters
        self.results = list(letters)
        self.log.append(dict(time=time.time(), armed_at=plan["armed_at"], table=self.table_name,
                             hand=len(letters), result=result, bet=plan["side"], stake=plan["stake"],
                             delta=settled["delta"], outcome=settled["outcome"],
                             balance_after=self.money.balance, block_pos=plan["block_pos"],
                             ladder=plan["ladder_step"], source="live-driver"))
        if self.money.blocked:
            self.pause(self.money.blocked[1])
        else:
            self._arm()
        return True


class BCRApp:
    def __init__(self, root, state_file=STATE_FILE, auto_connect=True):
        self.root, self.state_file = root, Path(state_file)
        self.sim = LiveSimulation()
        self.browser = BR.BrowserSession()
        self.tables = {}
        self.events = queue.Queue()
        self.chrome_busy = self.scan_busy = self.closing = self.exit_requested = False
        self.sweep_busy = self.open_table_busy = False
        self.auto_start_table_key = None
        self.auto_start_letters = ""
        self.auto_start_deadline = 0.0
        self.stream = LS.LobbyStream(self.browser)
        self.sweep_note_text = "Quét sảnh để tìm bàn đạt 3 điều kiện."
        self.next_scan = 0
        self.poll_id = None
        self.state_read_error = False
        self.ui_scale = DEFAULT_UI_SCALE
        self.chrome_startup_var = tk.BooleanVar(value=False)
        self.cdp_var = tk.BooleanVar(value=False)
        self.verified = tk.BooleanVar(value=False)
        self.only_eligible = tk.BooleanVar(value=False)
        self.search = tk.StringVar()
        self.connection_text = "Chưa kết nối Chrome."
        self._load_state()
        self.base_tk_scaling = float(root.tk.call("tk", "scaling"))
        root.tk.call("tk", "scaling", self.base_tk_scaling * self.ui_scale / 100)
        root.title("BCR · Live Method Lab — Giả lập 3-3")
        root.configure(bg=BG)
        root.geometry("1200x900+20+20")
        root.minsize(960, 720)
        self._build_ui()
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(500, self.poll)
        if auto_connect and self.chrome_startup_var.get():
            root.after(700, self.start_browser)

    def _label(self, parent, text="", size=10, color=TEXT, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kw)

    def _button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, bg=ACCENT if primary else PANEL,
                         fg="white" if primary else TEXT, activebackground=PALE,
                         activeforeground=ACCENT, relief="flat", bd=0, cursor="hand2",
                         font=("Segoe UI", 10, "bold"), padx=14, pady=8)

    def _card(self, parent):
        return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)

    def _build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=32, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=BG, foreground=MUTED,
                        relief="flat", font=("Segoe UI", 9, "bold"), padding=(8, 7))
        style.map("Treeview", background=[("selected", PALE)], foreground=[("selected", ACCENT)])
        style.configure("TNotebook", background=PANEL, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 7), font=("Segoe UI", 9))
        style.map("TNotebook.Tab", background=[("selected", PALE)], foreground=[("selected", ACCENT)])
        sidebar = tk.Frame(self.root, bg=NAV, width=170)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._label(sidebar, "BCR", 29, "white", True).pack(anchor="w", padx=24, pady=(28, 0))
        self._label(sidebar, "LIVE METHOD LAB", 9, "#8b9bb2").pack(anchor="w", padx=24)
        self._label(sidebar, "3 — 3", 24, "#63d6bc", True).pack(anchor="w", padx=24, pady=(18, 32))
        self.nav_buttons = {}
        for key, title in (("overview", "Tổng quan"), ("history", "Lịch sử"), ("settings", "Cài đặt")):
            button = tk.Button(sidebar, text=title, anchor="w", relief="flat", bd=0,
                               bg=NAV, fg="#b8c6d8", activebackground="#25364e", activeforeground="white",
                               font=("Segoe UI", 11), padx=20, pady=12, cursor="hand2",
                               command=lambda k=key: self.show_page(k))
            button.pack(fill="x", padx=10, pady=3)
            self.nav_buttons[key] = button
        self._label(sidebar, "CHỈ THEO DÕI\nKhông đặt tiền", 9, "#9badc5", justify="left").pack(
            side="bottom", anchor="w", padx=20, pady=24)
        main = tk.Frame(self.root, bg=BG)
        main.pack(side="left", fill="both", expand=True, padx=22, pady=18)
        header = tk.Frame(main, bg=BG)
        header.pack(fill="x", pady=(0, 12))
        self.page_title = self._label(header, "Tổng quan trực tiếp", 22, bold=True)
        self.page_title.pack(side="left")
        self._label(header, " DỮ LIỆU TRỰC TIẾP  /  VỐN ", 9, ACCENT, True).pack(side="right")
        connect = self._card(main)
        connect.pack(fill="x", pady=(0, 12))
        row = tk.Frame(connect, bg=PANEL)
        row.pack(fill="x", padx=14, pady=(10, 2))
        self.connection_label = self._label(row, "CHROME · 600 × 800", 10, bold=True, anchor="w")
        self.connection_label.pack(side="left", fill="x", expand=True)
        self.open_button = self._button(row, "Mở Chrome", self.start_browser, True)
        self.open_button.pack(side="right")
        self.attach_button = self._button(row, "Gắn Chrome đang mở", self.attach_browser)
        self.attach_button.pack(side="right", padx=6)
        self._button(row, "Đọc lại", self.request_scan).pack(side="right", padx=6)
        self.disconnect_button = self._button(row, "Đóng", self.stop_browser)
        self.disconnect_button.pack(side="right")
        self.connection_note = self._label(connect, size=9, color=MUTED, anchor="w", justify="left", wraplength=980)
        self.connection_note.pack(fill="x", padx=14, pady=(0, 8))
        sweep_row = tk.Frame(connect, bg=PANEL)
        sweep_row.pack(fill="x", padx=14, pady=(0, 10))
        self.sweep_button = self._button(sweep_row, "Quét toàn sảnh", self.sweep_lobby)
        self.sweep_button.pack(side="left")
        self.auto_open_button = self._button(sweep_row, "Tự mở bàn đạt chuẩn", lambda: self.auto_open_table(True))
        self.auto_open_button.pack(side="left", padx=6)
        self.sweep_note = self._label(sweep_row, "Một lần bấm: quét sảnh lấy lịch sử mọi bàn, đối chiếu hai nguồn, "
                                                 "mở bàn đạt 3 điều kiện rồi giả lập từ ván kế tiếp.",
                                      9, MUTED, anchor="w", justify="left", wraplength=680)
        self.sweep_note.pack(side="left", fill="x", expand=True, padx=10)
        self._label(main, "01  Mở Chrome     /     02  Chọn bàn     /     03  Đối chiếu kết quả", 9,
                    MUTED, anchor="w").pack(fill="x", pady=(0, 12))
        self._label(main, "Chỉ mô phỏng, không đặt tiền.", 9, MUTED, anchor="w").pack(
            side="bottom", fill="x", pady=(10, 0))
        self.stack = tk.Frame(main, bg=BG)
        self.stack.pack(fill="both", expand=True)
        self.stack.rowconfigure(0, weight=1)
        self.stack.columnconfigure(0, weight=1)
        self.pages, self.page_hosts, self.page_scrolls = {}, {}, {}
        for key in self.nav_buttons:
            host = tk.Frame(self.stack, bg=BG)
            host.grid(row=0, column=0, sticky="nsew")
            self.page_hosts[key] = host
            if key == "history":
                self.pages[key] = host
                continue
            canvas = tk.Canvas(host, bg=BG, highlightthickness=0)
            scrollbar = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            page = tk.Frame(canvas, bg=BG)
            item = canvas.create_window(0, 0, anchor="nw", window=page)
            canvas.bind("<Configure>", lambda e, c=canvas, i=item: c.itemconfigure(i, width=e.width))
            page.bind("<Configure>", lambda e, c=canvas: c.configure(scrollregion=c.bbox("all")))
            self.pages[key] = page
            self.page_scrolls[key] = canvas
        self._build_overview(self.pages["overview"])
        self._build_history(self.pages["history"])
        self._build_settings(self.pages["settings"])
        self.root.bind("<MouseWheel>", self._scroll_page, add="+")
        self.show_page("overview")

    def _build_overview(self, parent):
        kpis = tk.Frame(parent, bg=BG)
        kpis.pack(fill="x", pady=(0, 12))
        self.kpi_values, self.kpi_notes = [], []
        for i, title in enumerate(("VỐN", "LÃI / LỖ", "ROI", "THẮNG / THUA")):
            kpis.columnconfigure(i, weight=1, uniform="kpi")
            card = self._card(kpis)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 9, 0))
            self._label(card, title, 9, MUTED, True).pack(anchor="w", padx=14, pady=(12, 3))
            value = self._label(card, "—", 21, bold=True)
            value.pack(anchor="w", padx=14)
            note = self._label(card, "", 9, MUTED)
            note.pack(anchor="w", padx=14, pady=(2, 12))
            self.kpi_values.append(value)
            self.kpi_notes.append(note)
        middle = tk.Frame(parent, bg=BG)
        middle.pack(fill="x", pady=(0, 12))
        middle.columnconfigure(0, weight=3)
        middle.columnconfigure(1, weight=2, minsize=345)
        road = self._card(middle)
        road.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.road_title = self._label(road, "Chưa chọn bàn", 13, bold=True, anchor="w")
        self.road_title.pack(fill="x", padx=14, pady=(12, 2))
        self.road_count = self._label(road, "", 9, MUTED, anchor="w")
        self.road_count.pack(fill="x", padx=14, pady=(0, 8))
        tabs = ttk.Notebook(road)
        tabs.pack(fill="both", expand=True, padx=12)
        self.canvases = {}
        for key, title in (("bead", "Cầu B/P/T"), ("big", "Big Road"), ("equity", "Đường vốn")):
            canvas = tk.Canvas(tabs, bg=PANEL, height=164, width=200, highlightthickness=0)
            tabs.add(canvas, text=title)
            self.canvases[key] = canvas
            canvas.bind("<Configure>", lambda _e, k=key: self._draw_canvas(k))
        self.road_letters = self._label(road, "Chưa có kết quả từ site", 9, MUTED, anchor="w", wraplength=470)
        self.road_letters.pack(fill="x", padx=14, pady=(6, 12))
        plan = self._card(middle)
        plan.grid(row=0, column=1, sticky="nsew")
        self._label(plan, "KẾ HOẠCH TAY TIẾP THEO", 9, MUTED, True).pack(anchor="w", padx=14, pady=(12, 2))
        self.plan_side = self._label(plan, "CHỜ BÀN", 20, bold=True)
        self.plan_side.pack(anchor="w", padx=14)
        self.plan_stake = self._label(plan, "", 10, MUTED)
        self.plan_stake.pack(anchor="w", padx=14, pady=(0, 5))
        cycle = tk.Frame(plan, bg=PANEL)
        cycle.pack(fill="x", padx=14)
        self.cycle_labels = []
        for i in range(6):
            label = self._label(cycle, str(i + 1), 10, MUTED, True, width=3, pady=3)
            label.pack(side="left", padx=(0, 5))
            self.cycle_labels.append(label)
        self.checks_label = self._label(plan, "", 9, MUTED, anchor="w", justify="left")
        self.checks_label.pack(fill="x", padx=14, pady=6)
        self.verify_check = tk.Checkbutton(plan, text="Cầu khớp site · bàn chuẩn B 0,95 / P 1:1",
                                          variable=self.verified, bg=PANEL, fg=TEXT, selectcolor=PALE,
                                          activebackground=PANEL, font=("Segoe UI", 9), anchor="w")
        self.verify_check.pack(fill="x", padx=10)
        self.start_button = self._button(plan, "Bắt đầu", self.toggle_simulation, True)
        self.start_button.pack(fill="x", padx=14, pady=(5, 6))
        self.sim_note = self._label(plan, "", 9, MUTED, anchor="w", justify="left", wraplength=350)
        self.sim_note.pack(fill="x", padx=14, pady=(0, 12))
        table_card = self._card(parent)
        table_card.pack(fill="both", expand=True)
        toolbar = tk.Frame(table_card, bg=PANEL)
        toolbar.pack(fill="x", padx=14, pady=10)
        self.table_title = self._label(toolbar, "Bàn từ site · 0", 12, bold=True)
        self.table_title.pack(side="left")
        self._button(toolbar, "Chọn bàn", self.load_live_selected).pack(side="right")
        tk.Checkbutton(toolbar, text="Chỉ bàn đạt", variable=self.only_eligible, bg=PANEL,
                       activebackground=PANEL, font=("Segoe UI", 9), command=self._render_tables).pack(side="right", padx=10)
        entry = tk.Entry(toolbar, textvariable=self.search, relief="solid", bd=1,
                         font=("Segoe UI", 10), width=15)
        entry.pack(side="right", ipady=5)
        self.search.trace_add("write", lambda *_: self._render_tables())
        self.live_tree = self._tree(table_card, (("table", "TÊN BÀN", 220), ("hands", "TAY", 55),
                                    ("strong", "BÊN MẠNH", 100), ("rules", "ĐIỀU KIỆN 3-3", 240),
                                    ("data", "DỮ LIỆU", 165)), height=4)
        self.live_tree.tag_configure("ready", foreground=ACCENT)
        self.live_tree.tag_configure("wait", foreground=MUTED)
        self.live_tree.bind("<Double-1>", lambda _e: self.load_live_selected())
        self.empty_label = self._label(table_card, "Chưa đọc được bàn. Đăng nhập trong Chrome và mở sảnh Baccarat.",
                                       9, MUTED, anchor="w")
        self.empty_label.pack(fill="x", padx=14, pady=8)

    def _tree(self, parent, columns, height=10):
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height,
                            selectmode="browse")
        for key, title, width in columns:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=width, minwidth=40, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return tree

    def _build_history(self, parent):
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        bar = tk.Frame(card, bg=PANEL)
        bar.pack(fill="x", padx=14, pady=14)
        self._label(bar, "Kết quả bàn", 14, bold=True).pack(side="left")
        self._button(bar, "Xuất CSV", self.export_csv).pack(side="right")
        self.history_note = self._label(card, "", 10, MUTED, anchor="w", justify="left")
        self.history_note.pack(fill="x", padx=14, pady=(0, 12))
        self.history_tree = self._tree(card, (("time", "GIỜ", 75), ("table", "BÀN", 170),
                        ("hand", "TAY", 45), ("bet", "GIẢ LẬP", 80), ("result", "KẾT QUẢ", 80),
                        ("stake", "CƯỢC", 110), ("delta", "LÃI / LỖ", 110), ("balance", "VỐN", 125)))
        self.history_tree.tag_configure("win", foreground=GREEN)
        self.history_tree.tag_configure("loss", foreground=RED)
        self.history_size = -1

    def _build_settings(self, parent):
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        self._label(card, "Vốn & giới hạn phiên", 15, bold=True).pack(anchor="w", padx=20, pady=(18, 5))
        self._label(card, "Đổi vốn sẽ lưu phiên hiện tại.", 10, MUTED).pack(anchor="w", padx=20)
        grid = tk.Frame(card, bg=PANEL)
        grid.pack(fill="x", padx=20, pady=18)
        self.entries = {}
        fields = (("capital", "Vốn ban đầu (đ)"), ("unit", "Cược gốc (đ)"),
                  ("stop_loss_pct", "Cắt lỗ (% vốn)"), ("take_profit_pct", "Chốt lời (% vốn)"),
                  ("max_hands", "Giới hạn tay (0 = không giới hạn)"))
        for i, (key, title) in enumerate(fields):
            r, c = divmod(i, 2)
            grid.columnconfigure(c, weight=1)
            field = tk.Frame(grid, bg=PANEL)
            field.grid(row=r, column=c, sticky="ew", padx=(0, 24), pady=(0, 12))
            self._label(field, title, 10, MUTED).pack(anchor="w", pady=(0, 4))
            value = self.sim.money.start_capital if key == "capital" else getattr(self.sim.money, key)
            display_value = M.fmt_money(value) if key in ("capital", "unit") else str(value)
            var = tk.StringVar(value=display_value)
            tk.Entry(field, textvariable=var, font=("Segoe UI", 12), relief="solid", bd=1).pack(fill="x", ipady=6)
            self.entries[key] = var
        self._label(card, "Bậc cược: 1 → 3 → 7 → 15 → 31 → 64. Thắng về bậc 1.\n"
                    "Hòa giữ bậc. Chỉ theo dõi bàn Baccarat chuẩn.",
                    10, MUTED, justify="left").pack(anchor="w", padx=20, pady=(0, 14))
        self._button(card, "Lưu phiên cũ & áp dụng vốn mới", self.apply_settings, True).pack(anchor="w", padx=20)
        display = tk.Frame(card, bg=PANEL)
        display.pack(fill="x", padx=20, pady=(20, 0))
        self._label(display, "Hiển thị", 13, bold=True).pack(anchor="w", pady=(0, 8))
        scale_row = tk.Frame(display, bg=PANEL)
        scale_row.pack(anchor="w")
        self._label(scale_row, "Tỉ lệ giao diện (%)", 10, MUTED).pack(side="left", padx=(0, 10))
        self.ui_scale_var = tk.StringVar(value=str(int(self.ui_scale)))
        tk.Entry(scale_row, textvariable=self.ui_scale_var, width=7,
                 font=("Segoe UI", 11), relief="solid", bd=1).pack(side="left", ipady=4)
        self._button(scale_row, "Lưu tỉ lệ", self.apply_ui_scale).pack(side="left", padx=8)
        self._label(display, "Cửa sổ 1200 × 900 · mặc định 80% · áp dụng khi mở lại ứng dụng.",
                    9, MUTED).pack(anchor="w", pady=(6, 0))
        self._label(card, "Kết nối Chrome", 13, bold=True).pack(anchor="w", padx=20, pady=(24, 8))
        for var, text in ((self.chrome_startup_var, "Tự mở Chrome khi khởi động ứng dụng"),
                          (self.cdp_var, "Mở CDP localhost 9222 (cần mở lại Chrome)")):
            tk.Checkbutton(card, text=text, variable=var, command=self._save_state, bg=PANEL,
                           activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w", padx=16)
        self._label(card, "Đăng nhập trong Chrome để bắt đầu đọc bàn.",
                    9, MUTED, justify="left").pack(anchor="w", padx=20, pady=10)

    def _scroll_page(self, event):
        canvas = self.page_scrolls.get(self.current_page)
        if canvas and not isinstance(event.widget, ttk.Treeview):
            canvas.yview_scroll(-int(event.delta / 120), "units")

    def show_page(self, key):
        self.current_page = key
        self.page_hosts[key].tkraise()
        self.page_title.config(text={"overview": "Tổng quan", "history": "Lịch sử",
                                     "settings": "Cài đặt"}[key])
        for name, button in self.nav_buttons.items():
            button.config(bg="#25364e" if name == key else NAV, fg="white" if name == key else "#b8c6d8")

    def start_browser(self):
        if self.chrome_busy or self.exit_requested:
            return
        self.chrome_busy = True
        self.connection_text = "Đang mở Chrome và ghép driver tương thích…"
        cdp = bool(self.cdp_var.get())
        pos = (max(20, self.root.winfo_screenwidth() - BR.WIN_W - 20), 30)
        def work():
            try:
                if not self.browser.alive() and not self.browser.start(BR.START_URL, pos=pos, cdp=cdp):
                    self.events.put(("open_error", self.browser.last_error))
                    return
                self.events.put(("opened", self.browser.driver_info + " · Đăng nhập thủ công rồi mở sảnh Baccarat."))
            except Exception as exc:
                self.events.put(("open_error", str(exc).splitlines()[0][:240]))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def attach_browser(self):
        if self.chrome_busy or self.exit_requested:
            return
        self.chrome_busy = True
        self.connection_text = "Đang kết nối Chrome hiện có qua CDP 9222; không mở lại trang…"
        def work():
            try:
                ok = self.browser.attach()
                self.events.put(("opened" if ok else "open_error", self.browser.note))
            except Exception as exc:
                self.events.put(("open_error", "Không kết nối được Chrome (%s)." % type(exc).__name__))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def _cdp_available(self):
        if self.browser.cdp_attached:
            return True
        if self.browser.driver is None or not self.cdp_var.get():
            return False
        try:
            return self.browser._cdp_page() is not None
        except Exception:
            return False

    def sweep_lobby(self):
        if self.chrome_busy or self.sweep_busy or self.exit_requested:
            return
        if not self._cdp_available():
            self.sweep_note_text = "Gắn Chrome đang mở trước."
            self.refresh()
            return
        self.sweep_busy = True
        self.sweep_note_text = "Đang quét sảnh…"
        def work():
            try:
                rows = self.stream.sweep()
                self.events.put(("sweep", (rows, self.stream.last_error, self.stream.diag, time.time())))
            except Exception as exc:
                self.events.put(("sweep", ([], type(exc).__name__, {}, time.time())))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def auto_open_table(self, allow_sweep=False):
        if self.chrome_busy or self.sweep_busy or self.open_table_busy or self.exit_requested:
            return
        if not self._cdp_available():
            self.sweep_note_text = "Gắn Chrome đang mở trước."
            self.refresh()
            return
        if self.sim.running:
            self.sweep_note_text = "Tạm dừng trước khi đổi bàn."
            self.refresh()
            return
        candidates = []
        for row in self.tables.values():
            if not row.get("table_id") or row_problem(row):
                continue
            if not road_is_current(row):
                continue
            info = ME.check_table(list(row["letters"]))
            if info["eligible"]:
                candidates.append((info["hands"], row, info))
        if not candidates:
            if allow_sweep:
                self.sweep_note_text = "Chưa có bàn vừa được đối chiếu hai nguồn. Đang quét lại sảnh…"
                self.sweep_lobby()
            else:
                self.sweep_note_text = "Chưa có bàn đạt 3 điều kiện. Quét lại sảnh."
                self.refresh()
            return
        candidates.sort(key=lambda item: (abs(item[0] - ME.MIN_HANDS), item[1]["label"].casefold()))
        hands, row, info = candidates[0]
        self.open_table_busy = True
        self.auto_start_letters = row["letters"]
        self.sweep_note_text = "Đang mở %s (%d tay, bên mạnh %s)…" % (
            row["label"], hands, E.SIDE_NAME.get(info["strong"], "—"))
        table_id = row["table_id"]
        def work():
            try:
                ok, note = self.stream.open_table(table_id)
                self.events.put(("table_opened", (ok, note, row["key"])))
            except Exception as exc:
                self.events.put(("table_opened", (False, "Không mở được bàn (%s)." % type(exc).__name__, "")))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def stop_browser(self):
        if self.chrome_busy:
            return
        self.sim.pause("Đã ngắt kết nối.")
        self.chrome_busy = True
        self.connection_text = "Đang ngắt kết nối…"
        def work():
            self.events.put(("stopped", self.browser.stop()))
        threading.Thread(target=work, daemon=True).start()

    def request_scan(self):
        self.next_scan = 0
        if not self.browser.connected:
            self.connection_text = "Hãy mở Chrome trước khi đọc dữ liệu."
        self.refresh()

    def _scan(self):
        self.scan_busy = True
        def work():
            try:
                reports = self.browser.scan()
                self.events.put(("scan", (reports, self.browser.last_error, time.time())))
            except Exception as exc:
                self.events.put(("scan", ([], str(exc).splitlines()[0][:180], time.time())))
        threading.Thread(target=work, daemon=True).start()

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
                    self.sim.pause("Chưa kết nối Chrome. Giả lập đã dừng.")
                    self.tables.clear()
            elif kind == "scan":
                self.scan_busy = False
                reports, error, observed_at = data
                if error:
                    self.connection_text = "Đọc cầu chưa thành công: " + error
                    self.tables.clear()
                    self.verified.set(False)
                    self.sim.pause("Mất kết nối/đọc cầu lỗi. Hãy đối chiếu lại trước khi tiếp tục.")
                else:
                    self._ingest_reports(reports, observed_at)
                    n = sum(len(rep.get("tables", [])) for rep in reports)
                    self.connection_text = ("Đang đọc %d bàn." % n if n else "Chưa thấy bàn Baccarat.")
            elif kind == "sweep":
                self.sweep_busy = False
                rows, error, diag, observed_at = data
                if error:
                    self.sweep_note_text = "Quét toàn sảnh không thành công: " + error
                elif not rows:
                    self.sweep_note_text = "Chưa nhận được lịch sử. Mở sảnh Baccarat rồi quét lại."
                else:
                    self._ingest_reports([{"tables": rows, "page": {}, "diag": {}}], observed_at)
                    for row in rows:
                        stored = self.tables.get("id:" + row["table_id"])
                        # WebSocket và cầu đang vẽ là hai nguồn đọc độc lập cùng cho một chuỗi.
                        if stored and row["road_match"] and stored["stable"] < 2:
                            stored["stable"] = 2
                    self.sweep_note_text = "Đã quét %d bàn · %d bàn khớp lịch sử." % (
                        len(rows), sum(1 for r in rows if r["road_match"]))
                    # Bàn được mở bằng đúng ô bàn của sảnh nên phiên đăng nhập không bị động vào.
                    if not self.sim.running:
                        self.auto_open_table()
            elif kind == "table_opened":
                self.open_table_busy = False
                ok, note, key = data
                self.sweep_note_text = note
                if ok and key in self.tables:
                    self.sim.bind(self.tables[key])
                    self.verified.set(False)
                    self.auto_start_table_key = key
                    self.auto_start_deadline = time.time() + ENTRY_WAIT
                    self.sweep_note_text = "Đã vào bàn · đang xác nhận dữ liệu…"
                    self.next_scan = 0
        self._auto_start_verified_table()
        if self.sim.process(self.tables.get(self.sim.table_key)):
            self._save_state()
        now = time.time()
        if (not self.exit_requested and now >= self.next_scan and not self.scan_busy
                and not self.chrome_busy and not self.sweep_busy and self.browser.connected):
            self.next_scan = now + (FAST_SCAN if (self.auto_start_table_key or self.sim.running) else SCAN_SECONDS)
            self._scan()
        self.refresh()
        self.poll_id = self.root.after(500, self.poll)

    def _ingest_reports(self, reports, now=None):
        now = time.time() if now is None else now
        incoming = {}
        for report in reports:
            for raw in report.get("tables", []):
                letters, label = raw.get("letters", ""), " ".join(raw.get("label", "").split())[:80]
                if not label or not re.fullmatch(r"[BPT]{0,400}", letters):
                    continue
                table_id = raw.get("table_id", "")
                if not isinstance(table_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", table_id):
                    table_id = ""
                key = "id:" + table_id if table_id else re.sub(r"\s+", "", label).casefold()
                previous = incoming.get(key)
                old = self.tables.get(key)
                # Màn hình bàn không có tên trong DOM: giữ tên đẹp đã đọc từ sảnh cho cùng mã bàn.
                if old and (not label or label == table_id) and old["label"] != table_id:
                    label = old["label"]
                conflict = bool(previous and (previous["letters"] != letters or previous["conflict"]))
                incoming[key] = dict(key=key, label=label, letters=letters, ts=now, table_id=table_id,
                                     game_type=raw.get("game_type", ""), read_error=raw.get("read_error", ""),
                                     source=raw.get("source", ""), conflict=conflict)
                if raw.get("road_match") is True:
                    incoming[key]["ws_confirmed_letters"] = letters
                elif old:
                    confirmed = old.get("ws_confirmed_letters", "")
                    # Đổi shoe hoặc cầu bị viết lại: mất bằng chứng hai nguồn cùng khớp.
                    incoming[key]["ws_confirmed_letters"] = confirmed if (
                        letters.startswith(confirmed) or confirmed.startswith(letters)) else ""
        for key, row in incoming.items():
            old = self.tables.get(key)
            row["stable"] = old["stable"] + 1 if old and old["letters"] == row["letters"] and not (
                old["conflict"] or row["conflict"] or now - old["ts"] > LIVE_STALE) else 1
            row["screen_stable"] = (old.get("screen_stable", 0) + 1
                                    if row["source"] == "evo-bead" and old
                                    and old.get("source") == "evo-bead"
                                    and old["letters"] == row["letters"] else
                                    1 if row["source"] == "evo-bead" else 0)
            self.tables[key] = row
            if key == self.sim.table_key and not self.sim.running and row["letters"] != self.sim.letters:
                self.verified.set(False)
        self.tables = {k: r for k, r in self.tables.items() if now - r["ts"] < 300 or k == self.sim.table_key}
        # Lobby có thể tạm không trả bàn đang mở khi chuyển màn hình: giữ cược đã chốt, chờ lượt đọc sau.

    def _auto_start_verified_table(self):
        key = self.auto_start_table_key
        if not key:
            return
        row = self.tables.get(key)
        if not row or row.get("source") != "evo-bead" or row.get("screen_stable", 0) < 2:
            if time.time() > self.auto_start_deadline:
                self.sweep_note_text = ("Vào bàn rồi nhưng %d giây chưa đọc được cầu bàn đã mở hai lần giống nhau. "
                                        "Hãy quét lại sảnh hoặc bấm “Chọn bàn”." % ENTRY_WAIT)
                self.auto_start_table_key = None
            return
        problem = row_problem(row)
        if problem or not same_shoe(self.auto_start_letters, row["letters"]):
            self.sweep_note_text = ("Chưa tự bắt đầu: " + problem) if problem else (
                "Cầu trong bàn (%d tay) không cùng shoe với cầu đã lọc ở sảnh (%d tay)." % (
                    len(row["letters"]), len(self.auto_start_letters)))
            self.auto_start_table_key = None
            self.verified.set(False)
            return
        self.verified.set(True)
        if self.sim.arm_at_entry(row):
            self.sweep_note_text = "Đã vào bàn và bắt đầu giả lập · %s" % self.sim.note
            self.auto_start_table_key = None
        else:
            self.sweep_note_text = "Chưa thể bắt đầu: " + self.sim.note
            self.auto_start_table_key = None

    def _render_tables(self):
        if not hasattr(self, "live_tree"):
            return
        rows = []
        for row in self.tables.values():
            if time.time() - row["ts"] > LIVE_STALE:
                continue
            info = ME.check_table(list(row["letters"]))
            ready = info["eligible"] and not row_problem(row) and road_is_current(row)
            if self.search.get().casefold() not in row["label"].casefold() or (self.only_eligible.get() and not ready):
                continue
            rows.append((row, info, ready))
        rows.sort(key=lambda item: (not item[2], item[0]["label"].casefold()))
        keep = {row["key"] for row, _, _ in rows}
        for key in self.live_tree.get_children():
            if key not in keep:
                self.live_tree.delete(key)
        for i, (row, info, ready) in enumerate(rows):
            passed = sum(c["ok"] for c in info["checks"])
            verdict = "Đạt 3/3 · cần đối chiếu" if info["eligible"] else "%d/3 · %s" % (
                passed, next(c["detail"] for c in info["checks"] if not c["ok"]))
            problem = row_problem(row)
            status = "Xung đột" if row["conflict"] else "Chờ xác nhận đọc" if row["stable"] < 2 else (
                "Dữ liệu cũ" if time.time() - row["ts"] > LIVE_STALE else "Mới · %ds" % (time.time() - row["ts"]))
            if row.get("read_error"):
                verdict, status = row["read_error"], "Cần đối chiếu"
            elif row.get("game_type") and row["game_type"] != "baccarat":
                verdict, status = "Bàn đặc biệt · không giả lập", "Đọc được Canvas"
            elif not road_is_current(row):
                status = "Chưa đối chiếu 2 nguồn"
            elif problem and not row["conflict"] and row["stable"] >= 2 and "mã bàn" in problem:
                status = "Chưa rõ mã bàn"
            values = (row["label"], len(row["letters"]), E.SIDE_NAME.get(info["strong"], "—"), verdict, status)
            if self.live_tree.exists(row["key"]):
                self.live_tree.item(row["key"], values=values, tags=("ready" if ready else "wait",))
                self.live_tree.move(row["key"], "", i)
            else:
                self.live_tree.insert("", "end", iid=row["key"], values=values, tags=("ready" if ready else "wait",))
        self.table_title.config(text="Bàn khớp cầu · %d" % len(rows))
        self.empty_label.config(text=("%d bàn hiển thị · Bấm đúp để đối chiếu. Bàn đặc biệt không hỗ trợ." % len(rows))
                                if self.tables else "Chưa đọc được bàn. Đăng nhập trong Chrome và mở sảnh Baccarat.")

    def load_live_selected(self):
        if self.sim.running:
            self.sim.note = "Hãy tạm dừng giả lập trước khi đổi bàn."
        else:
            selected = self.live_tree.selection()
            row = self.tables.get(selected[0]) if selected else None
            if row:
                self.sim.bind(row)
                self.verified.set(False)
            else:
                self.sim.note = "Chọn một dòng bàn trong danh sách trước."
        self.refresh()

    def toggle_simulation(self):
        if self.sim.running:
            self.sim.pause()
        elif self.chrome_busy or not self.browser.connected:
            self.sim.note = "Mở Chrome và chờ đọc được bàn thật trước khi bắt đầu."
        else:
            before = self.sim.letters
            self.sim.start(self.tables.get(self.sim.table_key), self.verified.get())
            if self.sim.letters != before:
                self.verified.set(False)
        self._save_state()
        self.refresh()

    def refresh(self):
        book = self.sim.money
        decisive = book.wins + book.losses
        values = (M.fmt_money(book.balance), signed(book.profit), "%+.2f%%" % (book.profit / book.start_capital * 100)
                  if book.start_capital else "—", "%.0f%% / %.0f%%" % (book.wins / decisive * 100, book.losses / decisive * 100)
                  if decisive else "— / —")
        notes = ("Vốn đầu: " + M.fmt_money(book.start_capital), "%d tay đã ghi · %d hòa" % (book.hands_played, book.pushes),
                 "Lãi/lỗ ÷ vốn ban đầu", "Tỷ lệ tay, không tính hòa")
        for i, (value, note) in enumerate(zip(values, notes)):
            self.kpi_values[i].config(text=value, fg=(GREEN if book.profit >= 0 else RED) if i in (1, 2) else TEXT)
            self.kpi_notes[i].config(text=note)
        self.connection_note.config(text=self.connection_text)
        self.open_button.config(state="disabled" if self.chrome_busy else "normal")
        self.attach_button.config(state="disabled" if self.chrome_busy else "normal")
        self.disconnect_button.config(text="Ngắt kết nối" if self.browser.mode == "cdp" else "Đóng")
        self.sweep_button.config(state="disabled" if self.chrome_busy or self.sweep_busy else "normal")
        self.auto_open_button.config(state="disabled" if self.chrome_busy or self.sweep_busy
                                     or self.open_table_busy else "normal")
        self.sweep_note.config(text=self.sweep_note_text)
        self.road_title.config(text=self.sim.table_name)
        road = self.sim.results
        self.road_count.config(text="%d tay · Banker %d  /  Player %d  /  Hòa %d" % (
            len(road), road.count("B"), road.count("P"), road.count("T")))
        self.road_letters.config(text=" ".join(road[-40:]) if road else "Cầu thật sẽ xuất hiện sau khi bạn chọn bàn.")
        plan = self.sim.pending or self.sim.plan()
        side = plan["side"]
        self.plan_side.config(text=E.SIDE_NAME.get(side, "CHỜ BÀN").upper(), fg=COLORS.get(side, MUTED))
        self.plan_stake.config(text=("%s đ · bậc %d/6 · %s" % (M.fmt_money(plan["stake"]),
                                   min(plan["ladder_step"] + 1, 6), "đã chốt" if self.sim.running else "dự kiến")) if side else "Chưa chốt cược giả lập")
        strong = plan["strong"]
        for i, label in enumerate(self.cycle_labels):
            cell_side = strong if i < 3 else ("P" if strong == "B" else "B") if strong else None
            active = i == plan["block_pos"] and side
            label.config(text=cell_side or str(i + 1), bg=COLORS.get(cell_side, BG) if active else BG,
                         fg="white" if active else COLORS.get(cell_side, MUTED))
        self.checks_label.config(text="\n".join(("Đạt · " if c["ok"] else "Chờ · ") + c["name"] for c in plan["checks"]))
        self.verify_check.config(state="disabled" if self.sim.running else "normal")
        self.start_button.config(text="Tạm dừng" if self.sim.running else "Bắt đầu")
        self.sim_note.config(text=self.sim.note, fg=ACCENT if self.sim.running else MUTED)
        for key in self.canvases:
            self._draw_canvas(key)
        self._render_tables()
        self.history_note.config(text="%d tay · %d thắng / %d thua / %d hòa · Lãi/lỗ %s đ\nSụt vốn lớn nhất: %s đ" % (
            book.hands_played, book.wins, book.losses, book.pushes, signed(book.profit), M.fmt_money(book.max_drawdown)))
        if self.history_size != len(self.sim.log):
            for item in self.history_tree.get_children():
                self.history_tree.delete(item)
            for row in self.sim.log:
                self.history_tree.insert("", "end", tags=(row["outcome"],), values=(
                    time.strftime("%H:%M:%S", time.localtime(row["time"])), row["table"], row["hand"],
                    row["bet"], row["result"], M.fmt_money(row["stake"]), signed(row["delta"]), M.fmt_money(row["balance_after"])))
            self.history_size = len(self.sim.log)

    def _draw_canvas(self, key):
        canvas = self.canvases[key]
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 20 or h < 20:
            return
        canvas.delete("all")
        if key == "equity":
            if not self.sim.log:
                canvas.create_text(w / 2, h / 2, text="Chưa có tay giả lập từ site", fill=MUTED, font=("Segoe UI", 11))
                return
            values = [self.sim.money.start_capital] + [r["balance_after"] for r in self.sim.log]
            low, high = min(values), max(values)
            span = max(high - low, self.sim.money.unit)
            coords = []
            for i, v in enumerate(values):
                coords.extend((18 + i / (len(values) - 1) * (w - 36), h - 30 - (v - low) / span * (h - 52)))
            canvas.create_line(*coords, fill=ACCENT, width=3)
            canvas.create_text(18, 12, anchor="w", text="Vốn · " + M.fmt_money(values[-1]) + " đ", fill=MUTED)
            return
        cell = max(18, min(25, (h - 12) // 6))
        cols = max(1, (w - 12) // cell)
        for c in range(cols + 1):
            canvas.create_line(6 + c * cell, 6, 6 + c * cell, 6 + 6 * cell, fill=LINE)
        for r in range(7):
            canvas.create_line(6, 6 + r * cell, 6 + cols * cell, 6 + r * cell, fill=LINE)
        if key == "bead":
            total = (len(self.sim.results) + 5) // 6
            cells = [dict(col=i // 6, row=i % 6, side=s, ties=0) for i, s in enumerate(self.sim.results)]
        else:
            road = E.build_big_road(self.sim.results)
            total, cells = road["cols"], road["cells"]
        start = max(0, total - cols)
        for item in cells:
            if item["col"] < start:
                continue
            x, y = 6 + (item["col"] - start) * cell, 6 + item["row"] * cell
            color = COLORS[item["side"]]
            canvas.create_oval(x + 3, y + 3, x + cell - 3, y + cell - 3,
                               fill=color if key == "bead" else PANEL, outline=color, width=2)
            if key == "bead":
                canvas.create_text(x + cell / 2, y + cell / 2, text=item["side"], fill="white", font=("Segoe UI", 8, "bold"))
            elif item["ties"]:
                canvas.create_text(x + cell / 2, y + cell / 2, text=str(item["ties"]), fill=GREEN, font=("Segoe UI", 8))

    def export_csv(self):
        if not self.sim.log:
            messagebox.showinfo("Chưa có dữ liệu", "Chưa có tay giả lập để xuất.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                          initialfile="bcr-live-%s.csv" % time.strftime("%Y%m%d-%H%M%S"))
        if not path:
            return
        fields = ("time", "armed_at", "table", "hand", "bet", "result", "stake", "delta", "outcome", "balance_after", "source")
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                for item in self.sim.log:
                    row = {k: item[k] for k in fields}
                    if row["table"].startswith(("=", "+", "-", "@", "\t", "\r")):
                        row["table"] = "'" + row["table"]
                    writer.writerow(row)
        except OSError as exc:
            messagebox.showerror("Không xuất được", str(exc), parent=self.root)

    def apply_ui_scale(self):
        try:
            scale = int(self.ui_scale_var.get().strip())
            if not 50 <= scale <= 150:
                raise ValueError()
        except ValueError:
            messagebox.showerror("Tỉ lệ chưa hợp lệ", "Nhập tỉ lệ từ 50 đến 150.", parent=self.root)
            return
        self.ui_scale = scale
        self._save_state()
        messagebox.showinfo("Đã lưu tỉ lệ", "Mở lại ứng dụng để áp dụng tỉ lệ %d%%." % scale,
                            parent=self.root)

    def apply_settings(self):
        try:
            values = {k: float(v.get().strip().replace(",", "").replace(".", "")
                                if k in ("capital", "unit") else v.get().strip())
                      for k, v in self.entries.items()}
            if not all(math.isfinite(v) for v in values.values()):
                raise ValueError()
            if not (0 < values["unit"] <= values["capital"] and 1 <= values["stop_loss_pct"] <= 100
                    and values["take_profit_pct"] >= 1 and values["max_hands"] >= 0
                    and values["max_hands"].is_integer()):
                raise ValueError()
            book = M.MoneyBook(**values)
        except (ValueError, OverflowError):
            messagebox.showerror("Thông số chưa hợp lệ", "Vốn và cược gốc nhập bằng số (20.000.000 hoặc 20000000 đều được).\n"
                                 "Vốn ≥ cược gốc > 0; cắt lỗ 1–100%; chốt lời ≥ 1%; giới hạn tay là số nguyên ≥ 0.",
                                 parent=self.root)
            return
        if not messagebox.askyesno("Tạo phiên mới", "Lưu bản sao phiên hiện tại, dừng giả lập và bắt đầu với vốn mới?", parent=self.root):
            return
        if not self._save_state():
            return
        try:
            self._archive_state()
        except OSError as exc:
            messagebox.showerror("Không lưu được bản sao", str(exc), parent=self.root)
            return
        self.sim = LiveSimulation(book)
        self.verified.set(False)
        self.history_size = -1
        self._save_state()
        self.refresh()

    def _archive_state(self):
        if self.state_file.exists():
            archive = self.state_file.with_name("bcr-session-%s-%d.json" % (time.strftime("%Y%m%d-%H%M%S"), time.time_ns()))
            archive.write_bytes(self.state_file.read_bytes())

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            settings = data.get("settings", {})
            self.sim.money = M.MoneyBook(**{k: settings[k] for k in (
                "capital", "unit", "stop_loss_pct", "take_profit_pct", "max_hands") if k in settings})
            self.chrome_startup_var.set(bool(settings.get("chrome_startup", False)))
            self.cdp_var.set(bool(settings.get("chrome_cdp", False)))
            scale = settings.get("ui_scale", DEFAULT_UI_SCALE)
            if isinstance(scale, (int, float)) and not isinstance(scale, bool) and 50 <= scale <= 150:
                self.ui_scale = int(scale)
            if data.get("format") != "live-v2":
                self._archive_state()
                if data.get("log") or data.get("results"):
                    self.sim.note = "Phiên cũ đã được lưu riêng; không trộn dữ liệu cũ/thử nghiệm vào thống kê trực tiếp."
                return
            self.sim.log = data.get("log", [])
            for key in MONEY_FIELDS:
                if key in data.get("money", {}):
                    setattr(self.sim.money, key, data["money"][key])
            self.sim.note = "Đã khôi phục sổ vốn. Chọn và xác nhận lại bàn để chạy; không tự tính các tay lúc tắt app."
        except (OSError, ValueError, TypeError, AttributeError):
            self.sim = LiveSimulation()
            self.state_read_error = True
            self.connection_text = "Không đọc được file phiên. Giữ nguyên file gốc; không tự ghi đè."

    def _save_state(self):
        if self.state_read_error:
            self.sim.pause("Không thể lưu phiên: file cũ chưa đọc được. Dữ liệu gốc được giữ nguyên.")
            return False
        book = self.sim.money
        data = dict(format="live-v2", log=self.sim.log,
                    settings=dict(capital=book.start_capital, unit=book.unit, stop_loss_pct=book.stop_loss_pct,
                                  take_profit_pct=book.take_profit_pct, max_hands=book.max_hands,
                                  chrome_startup=self.chrome_startup_var.get(), chrome_cdp=self.cdp_var.get(),
                                  chrome_url=BR.START_URL, ui_scale=self.ui_scale),
                    money={key: getattr(book, key) for key in MONEY_FIELDS})
        try:
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.state_file)
            return True
        except OSError:
            self.sim.pause("Không lưu được phiên xuống ổ đĩa. Đã tạm dừng; dữ liệu vẫn còn trong ứng dụng.")
            return False

    def close(self):
        if self.closing:
            return
        self.exit_requested = True
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


def main():
    root = tk.Tk()
    BCRApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
