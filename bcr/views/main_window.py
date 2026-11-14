# -*- coding: utf-8 -*-
"""Tầng View: dựng toàn bộ widget (nav, bảng bàn, lịch sử, cài đặt) và chọn trò.

View chỉ tạo widget và giữ tham chiếu; không quyết định cược, không gọi CDP.
"""
import re
import tkinter as tk
from tkinter import messagebox, ttk

from bcr.config import *
from bcr.models import method as ME
from bcr.models import money as M
from bcr.models.live_sim import group_money_input


class MainWindowMixin:
    """Dựng widget: thanh chọn trò, trang tổng quan, lịch sử, cài đặt."""

    def _label(self, parent, text="", size=10, color=TEXT, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kw)

    def _button(self, parent, text, command, primary=False, bg=None, fg=None):
        background = bg or (ACCENT if primary else PANEL)
        foreground = fg or ("white" if primary else TEXT)
        return tk.Button(parent, text=text, command=command, bg=background,
                         fg=foreground, activebackground=PALE,
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
        # Menu tổng chọn trò: mỗi trò có khung UI riêng xếp chồng trong game_host,
        # chỉ khung của trò đang chọn được tkraise(). Nhờ vậy Baccarat và LONG-LONG
        # không dùng chung widget/state và không giẫm lên nhau.
        game_bar = tk.Frame(self.root, bg="#0f1722")
        game_bar.pack(side="top", fill="x")
        self.game_buttons = {}
        for key, title in (("baccarat", "Baccarat"), ("longlong", "LONG-LONG")):
            button = tk.Button(game_bar, text=title, relief="flat", bd=0,
                               bg="#0f1722", fg="#8aa0ba", activebackground="#25364e",
                               activeforeground="white", font=("Segoe UI", 11, "bold"),
                               padx=18, pady=9, cursor="hand2",
                               command=lambda k=key: self.select_game(k))
            button.pack(side="left", padx=(4 if key != "baccarat" else 8, 0), pady=4)
            self.game_buttons[key] = button
        # Điều khiển Chrome ngay trên thanh trên cùng: đang ở tab nào (Baccarat hay
        # LONG-LONG) cũng thấy, mở Chrome rồi tự vào bàn bằng tay trên cửa sổ đó.
        self.chrome_status = self._label(game_bar, "Chưa mở Chrome", 9, "#8aa0ba")
        self.chrome_status.pack(side="right", padx=(8, 12))
        # Số dư thật của bàn (đọc từ iframe game) luôn hiện ở thanh trên cùng.
        self.top_balance = self._label(game_bar, "Số dư: —", 10, "#63d6bc", True)
        self.top_balance.pack(side="right", padx=(8, 8))
        self.top_stop_button = self._button(game_bar, "Đóng", self.stop_browser,
                                            bg="#1d2b3d", fg="#c9d6e5")
        self.top_stop_button.pack(side="right", padx=(0, 6), pady=6)
        self.top_attach_button = self._button(game_bar, "Gắn Chrome", self.attach_browser,
                                              bg="#1d2b3d", fg="#c9d6e5")
        self.top_attach_button.pack(side="right", padx=(0, 6), pady=6)
        self.top_chrome_button = self._button(game_bar, "Mở Chrome", self.start_browser, True)
        self.top_chrome_button.pack(side="right", padx=(0, 6), pady=6)
        self.game_host = tk.Frame(self.root, bg=BG)
        self.game_host.pack(side="top", fill="both", expand=True)
        self.game_host.rowconfigure(0, weight=1)
        self.game_host.columnconfigure(0, weight=1)
        self.game_frames = {}
        self.baccarat_frame = tk.Frame(self.game_host, bg=BG)
        self.baccarat_frame.grid(row=0, column=0, sticky="nsew")
        self.game_frames["baccarat"] = self.baccarat_frame
        self.longlong_frame = tk.Frame(self.game_host, bg=BG)
        self.longlong_frame.grid(row=0, column=0, sticky="nsew")
        self.game_frames["longlong"] = self.longlong_frame
        # Thanh điều hướng ngang thay cho cột trái 170px: cửa sổ app chỉ còn ~600px
        # bề ngang để đặt cạnh Chrome trên cùng màn hình.
        nav = tk.Frame(self.baccarat_frame, bg=NAV)
        nav.pack(side="top", fill="x")
        self._label(nav, "BCR", 15, "white", True).pack(side="left", padx=(12, 6), pady=6)
        self.brand_method = self._label(nav, "", 15, "#63d6bc", True)
        self.brand_method.pack(side="left", padx=(0, 10))
        self.nav_buttons = {}
        for key, title in (("overview", "Tổng quan"), ("history", "Lịch sử"), ("settings", "Cài đặt")):
            button = tk.Button(nav, text=title, relief="flat", bd=0,
                               bg=NAV, fg="#b8c6d8", activebackground="#25364e", activeforeground="white",
                               font=("Segoe UI", 10), padx=12, pady=8, cursor="hand2",
                               command=lambda k=key: self.show_page(k))
            button.pack(side="left", padx=2, pady=4)
            self.nav_buttons[key] = button
        main = tk.Frame(self.baccarat_frame, bg=BG)
        main.pack(side="top", fill="both", expand=True, padx=10, pady=10)
        header = tk.Frame(main, bg=BG)
        header.pack(fill="x", pady=(0, 8))
        self.page_title = self._label(header, "Tổng quan", 15, bold=True)
        self.page_title.pack(side="left")
        self.hunt_banner = tk.Frame(main, bg=PALE, highlightbackground=ACCENT, highlightthickness=1)
        self.hunt_spinner = self._label(self.hunt_banner, "●", 13, ACCENT, True)
        self.hunt_spinner.pack(side="left", padx=(14, 8), pady=8)
        self.hunt_text = self._label(self.hunt_banner, "", 10, ACCENT, True, anchor="w", justify="left")
        self.hunt_text.pack(side="left", fill="x", expand=True, pady=8)
        self.hunt_cancel = self._button(self.hunt_banner, "Dừng tự quét", self.cancel_auto_hunt)
        self.hunt_cancel.pack(side="right", padx=10, pady=6)
        self._hunt_frame = 0
        connect = self._card(main)
        connect.pack(fill="x", pady=(0, 12))
        self.hunt_banner.pack(fill="x", pady=(0, 12), before=connect)
        self.hunt_banner.pack_forget()
        self._hunt_anchor = connect
        url_row = tk.Frame(connect, bg=PANEL)
        url_row.pack(fill="x", padx=10, pady=(8, 0))
        self._label(url_row, "Link", 9, MUTED).pack(side="left", padx=(0, 6))
        url_entry = tk.Entry(url_row, textvariable=self.start_url_var, font=("Segoe UI", 10),
                             relief="solid", bd=1)
        url_entry.pack(side="left", fill="x", expand=True, ipady=3)
        url_entry.bind("<FocusOut>", lambda _e: self._save_state())
        self.connection_label = self._label(connect, self._window_text(self._window_config()),
                                            9, bold=True, anchor="w")
        self.connection_label.pack(fill="x", padx=10, pady=(6, 0))
        row = tk.Frame(connect, bg=PANEL)
        row.pack(fill="x", padx=10, pady=(6, 2))
        self.open_button = self._button(row, "Mở Chrome", self.start_browser, True)
        self.open_button.pack(side="left")
        self.attach_button = self._button(row, "Gắn Chrome", self.attach_browser)
        self.attach_button.pack(side="left", padx=4)
        self._button(row, "Đọc lại", self.request_scan).pack(side="left", padx=4)
        self.disconnect_button = self._button(row, "Đóng", self.stop_browser)
        self.disconnect_button.pack(side="left", padx=4)
        self.connection_note = self._label(connect, size=9, color=MUTED, anchor="w", justify="left", wraplength=500)
        self.connection_note.pack(fill="x", padx=10, pady=(0, 6))
        wallet_row = tk.Frame(connect, bg=PANEL)
        wallet_row.pack(fill="x", padx=10, pady=(0, 6))
        self.wallet_balance_label = self._label(wallet_row, self.wallet_balance_text, 10, bold=True, anchor="w")
        self.wallet_balance_label.pack(side="left", fill="x", expand=True)
        self.wallet_sync_button = self._button(wallet_row, "Cập nhật số dư", self.request_wallet_sync)
        self.wallet_sync_button.pack(side="right", padx=(8, 0))
        sweep_row = tk.Frame(connect, bg=PALE, highlightbackground=ACCENT, highlightthickness=1)
        sweep_row.pack(fill="x", padx=10, pady=(0, 8), ipady=4)
        self.enter_lobby_button = self._button(sweep_row, "Nhận tab sảnh", self.enter_lobby)
        self.enter_lobby_button.pack(side="left")
        self.sweep_button = self._button(sweep_row, "Quét sảnh", self.sweep_lobby, primary=True)
        self.sweep_button.pack(side="left", padx=4)
        self.auto_open_button = self._button(sweep_row, "Tự mở bàn", lambda: self.auto_open_table(True),
                                             bg=BLUE, fg="white")
        self.auto_open_button.config(disabledforeground="#d7e3fb")
        self.auto_open_button.pack(side="left", padx=4)
        tk.Checkbutton(sweep_row, text="Ưa thích", variable=self.favorites_only,
                       command=self._save_state, bg=PALE, fg=TEXT, selectcolor=PANEL,
                       activebackground=PALE, activeforeground=ACCENT,
                       font=("Segoe UI", 9)).pack(side="left", padx=(4, 0))
        self.sweep_note = self._label(connect, "Bạn tự mở tab sảnh Baccarat trong Chrome rồi bấm “Nhận tab sảnh”. "
                                              "Vào bàn là app tự đánh, lặp khối tới khi đủ lãi mới đóng bàn.",
                                      9, MUTED, anchor="w", justify="left", wraplength=500)
        self.sweep_note.pack(fill="x", padx=10, pady=(0, 8))
        self.step_label = self._label(main, "", 9, MUTED, anchor="w", wraplength=520)
        self.step_label.pack(fill="x", pady=(0, 8))

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
        self._build_longlong_panel(self.longlong_frame)
        self.root.bind("<MouseWheel>", self._scroll_page, add="+")
        self.show_page("overview")
        self.select_game(getattr(self, "game_mode", "baccarat"))

    def _build_overview(self, parent):
        kpis = tk.Frame(parent, bg=BG)
        kpis.pack(fill="x", pady=(0, 10))
        self.kpi_values, self.kpi_notes = [], []
        for i, title in enumerate(("VỐN", "LÃI / LỖ", "ROI", "THẮNG / THUA")):
            kpis.columnconfigure(i, weight=1, uniform="kpi")
            card = self._card(kpis)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            self._label(card, title, 8, MUTED, True).pack(anchor="w", padx=8, pady=(8, 2))
            value = self._label(card, "—", 14, bold=True)
            value.pack(anchor="w", padx=8)
            note = self._label(card, "", 8, MUTED)
            note.pack(anchor="w", padx=8, pady=(2, 8))
            self.kpi_values.append(value)
            self.kpi_notes.append(note)
        # Cầu và đường vốn không hiển thị ở app — xem trực tiếp trong Chrome.
        plan = self._card(parent)
        plan.pack(fill="x", pady=(0, 10))
        self._label(plan, "KẾ HOẠCH TAY TIẾP THEO", 8, MUTED, True).pack(anchor="w", padx=10, pady=(10, 2))
        self.plan_side = self._label(plan, "CHỜ BÀN", 18, bold=True)
        self.plan_side.pack(anchor="w", padx=10)
        self.plan_stake = self._label(plan, "", 10, MUTED)
        self.plan_stake.pack(anchor="w", padx=10, pady=(0, 5))
        self.cycle_strip = tk.Frame(plan, bg=PANEL)
        self.cycle_strip.pack(fill="x", padx=10)
        self.cycle_labels = []
        self._sync_cycle_strip()
        self.checks_label = self._label(plan, "", 9, MUTED, anchor="w", justify="left")
        self.checks_label.pack(fill="x", padx=10, pady=6)
        self.chip_title = self._label(plan, "CHIP TRÊN BÀN", 8, MUTED, True, anchor="w")
        self.chip_title.pack(fill="x", padx=10, pady=(2, 0))
        self.chip_status = self._label(plan, "Chưa đọc chip.", 9, MUTED, True, anchor="w", justify="left",
                                       wraplength=500)
        self.chip_status.pack(fill="x", padx=10)
        self.chip_detail = self._label(plan, "", 9, MUTED, anchor="w", justify="left", wraplength=500)
        self.chip_detail.pack(fill="x", padx=10, pady=(0, 4))
        self.place_status = self._label(plan, "", 9, MUTED, anchor="w", justify="left", wraplength=500)
        self.place_status.pack(fill="x", padx=10, pady=(0, 4))
        self.sim_note = self._label(plan, "", 9, MUTED, anchor="w", justify="left", wraplength=500)
        self.sim_note.pack(fill="x", padx=10, pady=(0, 10))
        table_card = self._card(parent)
        table_card.pack(fill="both", expand=True)
        toolbar = tk.Frame(table_card, bg=PANEL)
        toolbar.pack(fill="x", padx=10, pady=8)
        self.table_title = self._label(toolbar, "Bàn từ site · 0", 11, bold=True)
        self.table_title.pack(side="left")
        self._button(toolbar, "Chọn bàn", self.load_live_selected).pack(side="right")
        tk.Checkbutton(toolbar, text="Chỉ bàn đạt", variable=self.only_eligible, bg=PANEL,
                       activebackground=PANEL, font=("Segoe UI", 9), command=self._render_tables).pack(side="right", padx=8)
        entry = tk.Entry(toolbar, textvariable=self.search, relief="solid", bd=1,
                         font=("Segoe UI", 10), width=10)
        entry.pack(side="right", ipady=4)
        self.search.trace_add("write", lambda *_: self._render_tables())
        self.live_tree = self._tree(table_card, (("table", "TÊN BÀN", 140), ("hands", "TAY", 40),
                                    ("strong", "BÊN MẠNH", 70), ("rules", "ĐIỀU KIỆN", 160),
                                    ("data", "DỮ LIỆU", 90)), height=4)
        self.live_tree.tag_configure("ready", foreground=ACCENT)
        self.live_tree.tag_configure("wait", foreground=MUTED)
        self.live_tree.bind("<Double-1>", lambda _e: self.load_live_selected())
        self.empty_label = self._label(table_card, "Chưa đọc được bàn. Đăng nhập trong Chrome và mở sảnh Baccarat.",
                                       9, MUTED, anchor="w", wraplength=500)
        self.empty_label.pack(fill="x", padx=10, pady=8)

    def _build_history(self, parent):
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        bar = tk.Frame(card, bg=PANEL)
        bar.pack(fill="x", padx=10, pady=10)
        self._label(bar, "Kết quả bàn", 12, bold=True).pack(side="left")
        self._button(bar, "Xuất CSV", self.export_csv).pack(side="right")
        self.history_note = self._label(card, "", 9, MUTED, anchor="w", justify="left", wraplength=500)
        self.history_note.pack(fill="x", padx=10, pady=(0, 10))
        self.history_tree = self._tree(card, (("time", "GIỜ", 55), ("table", "BÀN", 90),
                        ("hand", "TAY", 40), ("bet", "CƯỢC", 45), ("result", "KQ", 40),
                        ("stake", "TIỀN", 70), ("delta", "LÃI/LỖ", 70), ("balance", "VỐN", 85)))
        self.history_tree.configure(show="tree headings")
        self.history_tree.heading("#0", text="")
        self.history_tree.column("#0", width=20, minwidth=20, stretch=False)
        self.history_tree.tag_configure("table_group", font=("Segoe UI", 9, "bold"), background="#edf2f8")
        self.history_tree.tag_configure("win", foreground=GREEN)
        self.history_tree.tag_configure("loss", foreground=RED)
        self.history_size = -1

    def _build_settings(self, parent):
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        self._label(card, "Vốn & chốt lời", 13, bold=True).pack(anchor="w", padx=12, pady=(12, 4))
        self._label(card, "Đổi vốn sẽ lưu phiên hiện tại.", 9, MUTED).pack(anchor="w", padx=12)
        grid = tk.Frame(card, bg=PANEL)
        grid.pack(fill="x", padx=12, pady=10)
        self.entries = {}
        self.money_inputs = []
        fields = (("capital", "Vốn ban đầu (VN2)"),
                  ("take_profit_pct", "Chốt lời (% vốn)"))
        for i, (key, title) in enumerate(fields):
            r, c = divmod(i, 2)
            grid.columnconfigure(c, weight=1)
            field = tk.Frame(grid, bg=PANEL)
            field.grid(row=r, column=c, sticky="ew", padx=(0, 12), pady=(0, 10))
            self._label(field, title, 9, MUTED).pack(anchor="w", pady=(0, 4))
            value = self.sim.money.start_capital if key == "capital" else getattr(self.sim.money, key)
            is_money = key == "capital"
            display_value = M.fmt_input_money(value) if is_money else str(value)
            var = tk.StringVar(value=display_value)
            entry = tk.Entry(field, textvariable=var, font=("Segoe UI", 11), relief="solid", bd=1)
            entry.pack(fill="x", ipady=4)
            if is_money:
                self._bind_money_input(entry, var)
            self.entries[key] = var
        method = tk.Frame(card, bg=PANEL)
        method.pack(fill="x", padx=12, pady=(4, 0))
        self._label(method, "Phương pháp vào cầu", 12, bold=True).pack(anchor="w")
        self.profile_var = tk.StringVar(value=self.sim.profile)
        for name in ME.PROFILES:
            tk.Radiobutton(method, text=ME.PROFILE_LABEL[name], variable=self.profile_var, value=name,
                           command=self._rebuild_ladder_inputs, bg=PANEL, fg=TEXT, selectcolor=PALE,
                           activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w")
        self._label(method, "Hướng khối 3–3 tại cùng bàn", 10, MUTED, True).pack(anchor="w", pady=(8, 0))
        for value, title in (
                (ME.BLOCK_MODE_RECALCULATE, "Tính lại bên mạnh sau mỗi khối 6 tay (hiện tại)"),
                (ME.BLOCK_MODE_REPEAT, "Lặp lại hướng của khối đầu tiên (3P–3B rồi tiếp tục 3P–3B)")):
            tk.Radiobutton(method, text=title, variable=self.block_mode_var, value=value,
                           command=self.apply_block_mode, bg=PANEL, fg=TEXT, selectcolor=PALE,
                           activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w")
        self.ladder_mode_frame = tk.Frame(method, bg=PANEL)
        self.ladder_mode_frame.pack(fill="x", pady=(10, 0))
        self._label(self.ladder_mode_frame, "Cơ chế điền mức cược", 10, MUTED, True).pack(anchor="w")
        self.ladder_mode_buttons = {}
        for key, title in (("manual", "Tự config — điền từng bậc"),
                           ("multiply", "Tự nhân — theo cấu trúc 1-3-7-15-32-64")):
            tk.Radiobutton(self.ladder_mode_frame, text=title, variable=self.ladder_mode_var,
                           value=key, command=self._on_ladder_mode_change, bg=PANEL, fg=TEXT,
                           selectcolor=PALE, activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w")
        self.ladder_base_row = tk.Frame(method, bg=PANEL)
        self.ladder_base_row.pack(fill="x", pady=(6, 0))
        self._label(self.ladder_base_row, "Bậc 1 (VN2)", 10, MUTED).pack(side="left", padx=(0, 8))
        self.ladder_base_entry = tk.Entry(self.ladder_base_row, textvariable=self.ladder_base_var,
                                          width=14, font=("Segoe UI", 11), relief="solid", bd=1)
        self.ladder_base_entry.pack(side="left", ipady=4)
        self._bind_money_input(self.ladder_base_entry, self.ladder_base_var)
        self._label(self.ladder_base_row, "×3 ×7 ×15 ×32 ×64", 9, MUTED).pack(side="left", padx=8)
        self.ladder_frame = tk.Frame(method, bg=PANEL)
        self.ladder_frame.pack(fill="x", pady=(10, 0))
        self.ladder_vars = []
        self._rebuild_ladder_inputs()
        self._label(method, "Thắng về bậc 1 · hoà giữ bậc, hoàn tiền và không tính tay · mỗi ô là một tay trong khối.",
                    9, MUTED, justify="left", wraplength=500).pack(anchor="w", pady=(6, 0))
        hands_row = tk.Frame(method, bg=PANEL)
        hands_row.pack(fill="x", pady=(12, 0))
        self._label(hands_row, "Bộ lọc cầu", 12, bold=True).pack(anchor="w")
        hand_inputs = tk.Frame(hands_row, bg=PANEL)
        hand_inputs.pack(anchor="w", pady=(6, 0))
        self._label(hand_inputs, "Tay đã đi", 9, MUTED).pack(side="left", padx=(0, 6))
        tk.Entry(hand_inputs, textvariable=self.hands_min_var, width=4, font=("Segoe UI", 11),
                 relief="solid", bd=1).pack(side="left", ipady=3)
        self._label(hand_inputs, "–", 9, MUTED).pack(side="left", padx=4)
        tk.Entry(hand_inputs, textvariable=self.hands_max_var, width=4, font=("Segoe UI", 11),
                 relief="solid", bd=1).pack(side="left", ipady=3)
        self._label(hand_inputs, "\u00b7 b\u1ec7t t\u1ed1i \u0111a \u1edf s\u1ea3nh", 9, MUTED).pack(side="left", padx=(10, 6))
        tk.Entry(hand_inputs, textvariable=self.streak_var, width=3, font=("Segoe UI", 11),
                 relief="solid", bd=1).pack(side="left", ipady=3)
        live_exit_row = tk.Frame(hands_row, bg=PANEL)
        live_exit_row.pack(anchor="w", pady=(6, 0))
        self._label(live_exit_row, "Thoát bàn khi vệt bệt đạt", 9, MUTED).pack(side="left", padx=(0, 6))
        tk.Entry(live_exit_row, textvariable=self.live_exit_streak_var, width=4,
                 font=("Segoe UI", 11), relief="solid", bd=1).pack(side="left", ipady=3)
        self._label(live_exit_row, "tay (ví dụ 7: thoát ở tay bệt thứ 7)", 9, MUTED).pack(side="left", padx=(4, 0))
        lead_row = tk.Frame(hands_row, bg=PANEL)
        lead_row.pack(anchor="w", pady=(6, 0))
        self._label(lead_row, "Chênh lệch tay B/P tối thiểu", 9, MUTED).pack(side="left", padx=(0, 6))
        tk.Entry(lead_row, textvariable=self.min_lead_var, width=4,
                 font=("Segoe UI", 11), relief="solid", bd=1).pack(side="left", ipady=3)
        self._label(lead_row, "tay (3: P 10 vs B 13 OK · 12 vs 13 thì chờ)", 9, MUTED).pack(side="left", padx=(4, 0))
        self._label(hand_inputs, "tay", 9, MUTED).pack(side="left", padx=(4, 0))
        self._button(hand_inputs, "Lưu bộ lọc", self.apply_road_filter).pack(side="left", padx=(10, 0))
        self.hands_note = self._label(hands_row, "", 9, MUTED, justify="left", wraplength=500)
        self.hands_note.pack(anchor="w", pady=(6, 0))
        self._button(card, "Lưu & áp dụng phương pháp", self.apply_settings, True).pack(anchor="w", padx=12, pady=(14, 0))
        cycle = tk.Frame(card, bg=PANEL)
        cycle.pack(fill="x", padx=12, pady=(16, 0))
        self._label(cycle, "Chu trình bàn", 12, bold=True).pack(anchor="w")
        self.cycle_check = tk.Checkbutton(cycle, text="", variable=self.auto_cycle_var,
                                          command=self._save_state, bg=PANEL, fg=TEXT, selectcolor=PALE,
                                          activebackground=PANEL, font=("Segoe UI", 10))
        self.cycle_check.pack(anchor="w")
        self.hunt_check = tk.Checkbutton(cycle, text="Tự quét tìm bàn tới khi vào được (nghỉ %d giây)" % CYCLE_RETRY_SECONDS,
                                         variable=self.auto_hunt_var, command=self._on_hunt_toggle,
                                         bg=PANEL, fg=TEXT, selectcolor=PALE, activebackground=PANEL,
                                         font=("Segoe UI", 10))
        self.hunt_check.pack(anchor="w")
        tables_row = tk.Frame(cycle, bg=PANEL)
        tables_row.pack(anchor="w", pady=(6, 0))
        self._label(tables_row, "Số bàn muốn chơi", 9, MUTED).pack(side="left", padx=(0, 6))
        tk.Entry(tables_row, textvariable=self.tables_var, width=4, font=("Segoe UI", 11),
                 relief="solid", bd=1).pack(side="left", ipady=3)
        self._button(tables_row, "Lưu số bàn", self.apply_tables_target).pack(side="left", padx=(8, 0))
        self.tables_note = self._label(cycle, "", 9, MUTED, justify="left", wraplength=500)
        self.tables_note.pack(anchor="w", pady=(2, 0))
        self.cycle_rule = self._label(cycle, "", 9, MUTED, justify="left", wraplength=500)
        self.cycle_rule.pack(anchor="w", pady=(4, 0))
        self._sync_method_texts()
        display = tk.Frame(card, bg=PANEL)
        display.pack(fill="x", padx=12, pady=(16, 0))
        self._label(display, "Hiển thị", 12, bold=True).pack(anchor="w", pady=(0, 6))
        scale_row = tk.Frame(display, bg=PANEL)
        scale_row.pack(anchor="w")
        self._label(scale_row, "Tỉ lệ giao diện (%)", 9, MUTED).pack(side="left", padx=(0, 8))
        self.ui_scale_var = tk.StringVar(value=str(int(self.ui_scale)))
        tk.Entry(scale_row, textvariable=self.ui_scale_var, width=6,
                 font=("Segoe UI", 11), relief="solid", bd=1).pack(side="left", ipady=3)
        self._button(scale_row, "Lưu tỉ lệ", self.apply_ui_scale).pack(side="left", padx=8)
        self._label(display, "Cửa sổ app 600 × 900 · tỉ lệ áp dụng khi mở lại app.",
                    9, MUTED).pack(anchor="w", pady=(6, 0))
        self._label(card, "Kết nối Chrome", 12, bold=True).pack(anchor="w", padx=12, pady=(18, 6))
        for var, text in ((self.chrome_startup_var, "Tự mở Chrome khi khởi động app"),
                          (self.cdp_var, "Chrome app mở có cổng 9222 (để quét sảnh)")):
            tk.Checkbutton(card, text=text, variable=var, command=self._save_state, bg=PANEL,
                           activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w", padx=10)
        tk.Checkbutton(card, text="Tự phục hồi phiên khi bị văng “Bạn đã đăng xuất” (EV.7)",
                       variable=self.auto_recover_var, command=self._save_state, bg=PANEL,
                       fg=TEXT, selectcolor=PALE, activebackground=PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=10)
        tk.Checkbutton(card, text="Chuột người thật khi bấm sảnh/ô bàn",
                       variable=self.human_mouse_var, command=self.apply_mouse_setting, bg=PANEL,
                       fg=TEXT, selectcolor=PALE, activebackground=PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=10)
        tk.Checkbutton(card, text="Tự đặt chip vào ô cược khi chốt kế hoạch",
                       variable=self.auto_place_var, command=self._save_state, bg=PANEL,
                       fg=TEXT, selectcolor=PALE, activebackground=PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=10)
        window = tk.Frame(card, bg=PANEL)
        window.pack(fill="x", padx=10, pady=(10, 0))
        tk.Checkbutton(window, text="Chrome toàn màn hình",
                       variable=self.chrome_fullscreen_var,
                       command=self.apply_window_settings, bg=PANEL, fg=TEXT, selectcolor=PALE,
                       activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w")
        size_row = tk.Frame(window, bg=PANEL)
        size_row.pack(anchor="w", pady=(8, 0))
        inputs = (("Ngang", self.chrome_w_var, 6), ("Dọc", self.chrome_h_var, 6),
                  ("Tỉ lệ %", self.chrome_scale_var, 5))
        for title, var, width in inputs:
            self._label(size_row, title, 9, MUTED).pack(side="left", padx=(0, 4))
            tk.Entry(size_row, textvariable=var, width=width, font=("Segoe UI", 11),
                     relief="solid", bd=1).pack(side="left", padx=(0, 10), ipady=3)
        self._button(size_row, "Lưu cửa sổ", self.apply_window_settings).pack(side="left")
        self._label(window, "Cỡ 800×600 – 7680×4320, tỉ lệ 50–200%. Đổi tỉ lệ render cần mở lại Chrome.",
                    9, MUTED, justify="left", wraplength=500).pack(anchor="w", pady=(6, 0))
        chips = tk.Frame(card, bg=PANEL)
        chips.pack(fill="x", padx=12, pady=(14, 12))
        self._label(chips, "Chip trên bàn", 12, bold=True).pack(anchor="w")
        self._label(chips, "Số trên chip là tiền VN2 của sàn (chip 20 = 20). App đọc khay thật để gợi ý chip.",
                    9, MUTED, justify="left", wraplength=500).pack(anchor="w")

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

    def _bind_money_input(self, entry, var):
        """Ô nhập tiền chỉ nhận chữ số và tự chèn dấu phân cách hàng nghìn khi gõ."""
        entry.config(validate="key", validatecommand=(
            entry.register(lambda proposed: re.fullmatch(r"[\d,.]*", proposed) is not None), "%P"))

        def regroup(_event=None):
            try:
                caret = entry.index("insert")
            except tk.TclError:
                caret = len(var.get())
            grouped, target = group_money_input(var.get(), caret)
            if grouped == var.get():
                return
            var.set(grouped)
            entry.icursor(target)

        self.money_inputs.append((entry, regroup))
        for event in ("<KeyRelease>", "<FocusOut>"):
            entry.bind(event, regroup)

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

    def _rebuild_ladder_inputs(self):
        """Số ô nhập bậc cược đi theo khối của phương pháp; giữ mức đã gõ riêng cho từng phương pháp."""
        if getattr(self, "ladder_vars", None):
            self.ladder_store[self.ladder_shown_profile] = [var.get() for var in self.ladder_vars]
        profile = self.profile_var.get()
        raw = self.ladder_store.get(profile) or [M.fmt_input_money(s) for s in ME.DEFAULT_LADDER[profile]]
        for child in self.ladder_frame.winfo_children():
            child.destroy()
        self.ladder_vars = []
        multiply = self.ladder_mode_var.get() == "multiply"
        for i, text in enumerate(raw):
            field = tk.Frame(self.ladder_frame, bg=PANEL)
            field.grid(row=i // 3, column=i % 3, padx=(0, 12), pady=(0, 8))
            self._label(field, "Tay %d · bậc %d (VN2)" % (i + 1, i + 1), 9, MUTED).pack(anchor="w")
            var = tk.StringVar(value=text)
            entry = tk.Entry(field, textvariable=var, width=13, font=("Segoe UI", 11),
                             relief="solid", bd=1)
            entry.pack(ipady=4)
            self._bind_money_input(entry, var)
            if multiply:
                entry.config(state="readonly", readonlybackground=PALE)
            self.ladder_vars.append(var)
        self.ladder_shown_profile = profile
        self._sync_method_texts(profile)
        self._sync_ladder_mode_state()

    def _sync_ladder_mode_state(self):
        """Đồng bộ trạng thái ô bậc 1 và các ô bậc theo cơ chế đang chọn."""
        if not hasattr(self, "ladder_base_entry"):
            return
        multiply = self.ladder_mode_var.get() == "multiply"
        self.ladder_base_entry.config(state="normal" if multiply else "readonly",
                                      readonlybackground=PALE if not multiply else "white")
        if multiply:
            try:
                base = M.parse_money(self.ladder_base_var.get())
            except ValueError:
                base = 0
            if base > 0:
                stakes = ME.multiply_ladder(base, self.profile_var.get())
                for var, value in zip(self.ladder_vars, stakes):
                    var.set(M.fmt_input_money(value))
                self.ladder_store[self.ladder_shown_profile] = [M.fmt_input_money(s) for s in stakes]

    def _sync_method_texts(self, profile=None):
        """Câu chữ trong app luôn nói đúng số tay của khối đang chọn."""
        profile = profile or self.sim.profile
        hands = ME.block_hands(profile)
        strong = ME.strong_hands(profile)
        if hasattr(self, "brand_method"):
            applied = ME.strong_hands(self.sim.profile)
            self.brand_method.config(text="%d — %d" % (applied, self.sim.cycle_hands - applied))
        target = self.sim.money.take_profit_pct
        if hasattr(self, "cycle_check"):
            self.cycle_check.config(text="Lặp khối %s tại bàn tới khi lãi đủ %.0f%% vốn" % (
                profile, target))
            direction_rule = ("t\u00ednh l\u1ea1i b\u00ean m\u1ea1nh m\u1ed7i kh\u1ed1i" if self.sim.block_mode == ME.BLOCK_MODE_RECALCULATE
                              else "l\u1eb7p l\u1ea1i h\u01b0\u1edbng c\u1ee7a kh\u1ed1i \u0111\u1ea7u ti\u00ean")
            self.cycle_rule.config(
                text="Kh\u1ed1i %s = %d tay b\u00ean m\u1ea1nh + %d tay b\u00ean \u0111\u1ed5i; %s. "
                     "\u0110\u1ee7 %.0f%% v\u1ed1n th\u00ec r\u1eddi b\u00e0n, t\u1ef1 qu\u00e9t v\u00e0o b\u00e0n k\u1ebf ti\u1ebfp t\u1edbi khi xong %d b\u00e0n; "
                     "v\u1ed1n ban \u0111\u1ea7u t\u1ef1 \u0111\u1ed3ng b\u1ed9 theo s\u1ed1 d\u01b0 sau m\u1ed7i b\u00e0n." % (
                         profile, strong, hands - strong, direction_rule, target, self._tables_target()))
        if hasattr(self, "step_label"):
            self.step_label.config(text="Vào bàn là tự đánh · khối %s lặp tới khi lãi đủ %.0f%% vốn · tự đóng bàn"
                                   % (profile, target))

    def select_game(self, mode):
        """Đổi trò đang hiển thị. Chỉ raise đúng khung, không chạy chéo logic."""
        if mode not in self.game_frames:
            mode = "baccarat"
        self.game_mode = mode
        self.game_frames[mode].tkraise()
        # Tab LONG-LONG: bật/tắt vòng đọc số dư thật theo tab (Controller quyết định).
        if hasattr(self, "longlong_live_autostart"):
            self.longlong_live_autostart()
        for name, button in self.game_buttons.items():
            active = name == mode
            button.config(bg="#1d2f45" if active else "#0f1722",
                          fg="white" if active else "#8aa0ba")
