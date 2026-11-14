# -*- coding: utf-8 -*-
"""Panel LONG-LONG (thay hẳn panel Đua ngựa).

View chỉ dựng widget và hiển thị trạng thái do Model trả về; mọi quyết định cược nằm
trong ``bcr.models.longlong``. Không giữ state của Baccarat.

Chiến thuật của bàn này chỉ có MỘT ô đánh: "1 vàng 1 đỏ" (trả 0.95), nên panel không
có bộ chọn cửa — chỉ có thang cược, nút ghi tay / đọc kết quả và sổ vốn.
"""

import time
import tkinter as tk
from tkinter import messagebox

from bcr.config import *
from bcr.models import longlong
from bcr.models.longlong import (
    DEFAULT_BASE, DEFAULT_CAPITAL, DEFAULT_LADDER, DOOR_LABEL, DOOR_NAME, PAYOUT,
    LongLongSimulation, raw_label,
)


def fmt(value):
    """Số theo đơn vị sảnh: 4920 -> '4.920'."""
    return "{:,}".format(int(round(value))).replace(",", ".")


def fmt_vn2(value):
    """Số VN2 theo kiểu Việt Nam: 3114.69 -> '3.114,69'; None -> '—'."""
    if value is None:
        return "—"
    text = "{:,.2f}".format(float(value))
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


class LongLongPanelMixin:
    """Khung LONG-LONG: thang gấp thếp, nhập/đọc kết quả và sổ vốn riêng."""

    def _build_longlong_panel(self, parent):
        bar = tk.Frame(parent, bg=NAV)
        bar.pack(side="top", fill="x")
        self._label(bar, "LONG-LONG", 15, "white", True).pack(side="left", padx=(12, 6), pady=6)
        self._label(bar, "2 bát súc xắc · vàng / đỏ", 12, "#63d6bc", True).pack(
            side="left", padx=(0, 10))
        body = tk.Frame(parent, bg=BG)
        body.pack(side="top", fill="both", expand=True, padx=10, pady=10)

        # Số dư thật đọc từ bàn (Controller đẩy xuống), cập nhật realtime.
        live = self._card(body)
        live.pack(fill="x")
        live_head = tk.Frame(live, bg=PANEL)
        live_head.pack(fill="x", padx=12, pady=(10, 0))
        self._label(live_head, "SỐ DƯ THỰC TẾ TRÊN BÀN", 12, bold=True).pack(side="left")
        self.longlong_live_button = self._button(live_head, "Bật cập nhật",
                                                 self.longlong_live_toggle)
        self.longlong_live_button.pack(side="right")
        self._button(live_head, "Đọc ngay", self.longlong_read_now).pack(side="right", padx=6)
        self.longlong_balance = self._label(live, "—", 22, TEXT, True, anchor="w")
        self.longlong_balance.pack(fill="x", padx=12, pady=(4, 0))
        self.longlong_balance_note = self._label(
            live, "Chưa đọc — mở bàn trong Chrome rồi bấm “Đọc ngay” (hoặc bật cập nhật).",
            9, MUTED, anchor="w", justify="left", wraplength=560)
        self.longlong_balance_note.pack(fill="x", padx=12, pady=(0, 2))
        self.longlong_board = self._label(live, "", 9, TEXT, anchor="w", justify="left",
                                          wraplength=560)
        self.longlong_board.pack(fill="x", padx=12, pady=(0, 2))
        self.longlong_live_status = self._label(live, "", 9, MUTED, anchor="w")
        self.longlong_live_status.pack(fill="x", padx=12, pady=(0, 4))

        auto_row = tk.Frame(live, bg=PANEL)
        auto_row.pack(fill="x", padx=12, pady=(0, 4))
        self.longlong_auto_var = tk.BooleanVar(value=True)
        tk.Checkbutton(auto_row, text="Tự chốt kết quả ván (mỗi ván đúng 1 lần)",
                       variable=self.longlong_auto_var, bg=PANEL, activebackground=PANEL,
                       font=("Segoe UI", 9), bd=0, anchor="w",
                       command=self.longlong_toggle_options).pack(side="left")
        self.longlong_place_var = tk.BooleanVar(value=False)
        tk.Checkbutton(auto_row, text="Đặt chip thật (tiền thật)",
                       variable=self.longlong_place_var, bg=PANEL, activebackground=PANEL,
                       font=("Segoe UI", 9, "bold"), fg=RED, bd=0, anchor="w",
                       command=self.longlong_toggle_options).pack(side="left", padx=(14, 0))
        self.longlong_auto_log = self._label(live, "Chưa có ván nào.", 9, TEXT, anchor="nw",
                                             justify="left", wraplength=560)
        self.longlong_auto_log.pack(fill="x", padx=12, pady=(2, 10))

        strategy = self._card(body)
        strategy.pack(fill="x")
        self._label(strategy, "Nguyên tắc: chờ bộ vàng-đỏ (VD/DV) rồi đặt ô 1 vàng 1 đỏ",
                    13, bold=True, anchor="w").pack(fill="x", padx=12, pady=(12, 2))
        self._label(strategy,
                    "Tay trước ra VV hoặc DD thì BỎ. Tay trước ra VD hoặc DV thì tay sau đặt ô "
                    "\"1 vàng 1 đỏ\" (ăn 0.95). Thắng thì đánh tiếp ở vốn 1; thua thì chờ bộ "
                    "vàng-đỏ kế tiếp và gấp thếp lên bậc mới để bù số đã thua.",
                    9, MUTED, anchor="w", justify="left", wraplength=560).pack(
                        fill="x", padx=12, pady=(0, 10))

        door_row = tk.Frame(strategy, bg=PANEL)
        door_row.pack(fill="x", padx=12, pady=(0, 12))
        self._label(door_row, "Ô đánh:", 10, MUTED).pack(side="left", padx=(0, 8))
        self._label(door_row, "%s (%s) · ăn %.2f : 1" % (DOOR_NAME, DOOR_LABEL, PAYOUT["M"]),
                    10, ACCENT, True).pack(side="left")

        ladder_card = self._card(body)
        ladder_card.pack(fill="x", pady=(10, 0))
        self._label(ladder_card, "Thang cược (%d bậc · đơn vị sảnh: 1 = 1.000đ)"
                    % len(DEFAULT_LADDER), 12, bold=True, anchor="w").pack(
                        fill="x", padx=12, pady=(10, 4))
        auto_row = tk.Frame(ladder_card, bg=PANEL)
        auto_row.pack(fill="x", padx=12, pady=(0, 6))
        self._label(auto_row, "Mức gốc", 9, MUTED).pack(side="left", padx=(0, 6))
        self.longlong_base_var = tk.StringVar(value=str(DEFAULT_BASE))
        tk.Entry(auto_row, textvariable=self.longlong_base_var, width=7,
                 font=("Segoe UI", 11), relief="solid", bd=1).pack(side="left", ipady=3)
        self._label(auto_row, "Vốn phiên", 9, MUTED).pack(side="left", padx=(12, 6))
        self.longlong_capital_var = tk.StringVar(value=str(DEFAULT_CAPITAL))
        tk.Entry(auto_row, textvariable=self.longlong_capital_var, width=9,
                 font=("Segoe UI", 11), relief="solid", bd=1).pack(side="left", ipady=3)
        self._button(auto_row, "Chia thang", self.longlong_autofill, True).pack(
            side="left", padx=8)
        self._label(auto_row, "gốc 40 + vốn 10.000 → 40, 80 … 4.920", 9, MUTED).pack(side="left")
        grid = tk.Frame(ladder_card, bg=PANEL)
        grid.pack(fill="x", padx=12)
        self.longlong_ladder_vars = []
        for i, stake in enumerate(DEFAULT_LADDER):
            row, col = divmod(i, 3)
            cell = tk.Frame(grid, bg=PANEL)
            cell.grid(row=row, column=col, sticky="we", padx=(0, 8), pady=2)
            grid.columnconfigure(col, weight=1)
            self._label(cell, "Tay %d" % (i + 1), 8, MUTED).pack(side="left")
            var = tk.StringVar(value=str(stake))
            self.longlong_ladder_vars.append(var)
            tk.Entry(cell, textvariable=var, width=9, font=("Segoe UI", 10),
                     relief="solid", bd=1).pack(side="right", ipady=2)
        self.longlong_ladder_note = self._label(ladder_card, "", 9, MUTED, anchor="w")
        self.longlong_ladder_note.pack(fill="x", padx=12, pady=(6, 10))
        self._apply_saved_longlong_ladder()

        play = self._card(body)
        play.pack(fill="x", pady=(10, 0))
        self._label(play, "Nhập kết quả tay: VV · DD · VD · DV", 12, bold=True,
                    anchor="w").pack(fill="x", padx=12, pady=(10, 4))
        entry_row = tk.Frame(play, bg=PANEL)
        entry_row.pack(fill="x", padx=12)
        self.longlong_result_var = tk.StringVar(value="")
        tk.Entry(entry_row, textvariable=self.longlong_result_var, width=4,
                 font=("Segoe UI", 12), relief="solid", bd=1).pack(side="left", ipady=4)
        self._button(entry_row, "Ghi tay", self.longlong_observe, True).pack(side="left", padx=6)
        self._button(entry_row, "Đặt theo kế hoạch", self.longlong_arm).pack(side="left", padx=4)
        self._button(entry_row, "Reset phiên", self.longlong_reset).pack(side="left", padx=4)
        self._button(entry_row, "Đọc kết quả từ trang",
                     self.longlong_read_live).pack(side="left", padx=4)
        self.longlong_next = self._label(play, "", 11, ACCENT, True, anchor="w",
                                          justify="left", wraplength=560)
        self.longlong_next.pack(fill="x", padx=12, pady=(8, 0))
        self.longlong_state = self._label(play, "", 10, TEXT, anchor="w", justify="left",
                                           wraplength=560)
        self.longlong_state.pack(fill="x", padx=12, pady=(2, 0))
        self.longlong_profit = self._label(play, "", 10, MUTED, True, anchor="w")
        self.longlong_profit.pack(fill="x", padx=12, pady=(2, 10))

        history = self._card(body)
        history.pack(fill="both", expand=True, pady=(10, 0))
        self._label(history, "Lịch sử cược", 12, bold=True, anchor="w").pack(
            fill="x", padx=12, pady=(10, 4))
        self.longlong_log = self._label(history, "Chưa có tay nào.", 9, MUTED, anchor="nw",
                                         justify="left", wraplength=560)
        self.longlong_log.pack(fill="x", padx=12, pady=(0, 10))
        self._longlong_refresh()
        self._longlong_render_live()

    # ------------------------------------------------------------------ state
    def _apply_saved_longlong_ladder(self):
        """Nạp thang đã lưu; giá trị cũ tính theo đồng (>=1000) tự chia 1000 về đơn vị sảnh."""
        saved = getattr(self, "_longlong_saved", None) or {}
        ladder = saved.get("ladder")
        base = saved.get("base")
        if isinstance(base, str) and base.strip():
            self.longlong_base_var.set(base.strip())
        if not isinstance(ladder, list) or not ladder:
            return
        values = []
        for stake in ladder:
            try:
                number = float(str(stake).replace(".", "").replace(",", "").strip())
            except (TypeError, ValueError):
                values = []
                break
            # Thang cũ ghi bằng đồng (2.000, 4.000...): hạ 1000 lần cho khớp chip sảnh.
            if number >= 1000:
                number = number / 1000.0
            values.append(int(round(number)))
        if values and all(v > 0 for v in values):
            for var, stake in zip(self.longlong_ladder_vars, values):
                var.set(str(stake))

    def _longlong_build(self):
        """Dựng lại chiến thuật + sổ vốn LONG-LONG từ thang cược đang nhập."""
        stakes = [str(var.get()).replace(".", "").replace(",", "").strip()
                  for var in self.longlong_ladder_vars]
        self.longlong_sim = LongLongSimulation(stakes)
        self.longlong = self.longlong_sim.strategy
        self.longlong.note = "Đã nạp thang cược; chờ tay ra bộ vàng-đỏ (VD/DV)."
        return self.longlong

    def _longlong_strategy(self):
        if not getattr(self, "longlong", None):
            self._longlong_build()
        return self.longlong

    def _longlong_simulation(self):
        if not getattr(self, "longlong_sim", None):
            self._longlong_build()
        return self.longlong_sim

    # ------------------------------------------------------------------ actions
    def longlong_autofill(self):
        """Chia vốn thành thang gấp đôi: các bậc ×2, bậc cuối bù phần lẻ cho đủ vốn.

        Ví dụ gốc 40 + vốn 10.000 đv → 40, 80, 160, 320, 640, 1.280, 2.560, 4.920
        (tổng đúng 10.000 đv = 10.000.000đ).
        """
        capital = self.longlong_capital_var.get() if hasattr(self, "longlong_capital_var") else ""
        stakes = longlong.ladder_for_capital(capital, self.longlong_base_var.get(),
                                             len(self.longlong_ladder_vars))
        for var, stake in zip(self.longlong_ladder_vars, stakes):
            var.set(str(stake))
        self.longlong_sim = LongLongSimulation(stakes)
        self.longlong = self.longlong_sim.strategy
        self.longlong.note = "Đã chia thang %s … theo đơn vị sảnh." % fmt(stakes[0])
        self._longlong_refresh()

    def longlong_observe(self):
        """Ghi một tay kết quả (VV/DD/VD/DV) để chạy chiến thuật."""
        value = str(self.longlong_result_var.get()).strip()
        if longlong.normalize_result(value) is None:
            messagebox.showerror(
                "Kết quả chưa hợp lệ",
                "Nhập bộ hai mặt của tay: VV (2 vàng), DD (2 đỏ), VD hoặc DV (vàng-đỏ).",
                parent=self.root)
            return
        self._longlong_simulation().append(value)
        self.longlong_result_var.set("")
        self._longlong_refresh()

    def longlong_read_live(self):
        """Nhờ Controller đọc kết quả LONG-LONG thật từ tab Chrome (không bấm cược).

        View không tự gọi Adapter: nó chỉ hiển thị kết quả Controller trả về.
        """
        self.longlong_state.config(text="Đang đọc lịch sử LONG-LONG từ trang…")

        def apply(results, error):
            if error or not results:
                self.longlong_state.config(
                    text="Chưa đọc được kết quả: %s" % (error or "trống"))
                return
            sim = self._longlong_simulation()
            added = sim.ingest(results)
            self._longlong_refresh()
            self.longlong_state.config(
                text="Đã nạp %d tay mới từ trang · %s" % (added, sim.note))

        self.read_longlong_from_page(apply)

    def longlong_arm(self):
        """Chốt lệnh cược cho tay kế tiếp theo kế hoạch của Model."""
        self._longlong_strategy().arm()
        self._longlong_refresh()

    def longlong_toggle_options(self):
        """Bật/tắt tự chốt kết quả và đặt chip thật (ghi rõ vào nhật ký)."""
        if self.longlong_place_real_on():
            self._longlong_log("ĐÃ BẬT đặt chip thật: app sẽ bấm chip vào ô %s theo thang."
                               % DOOR_NAME)
        else:
            self._longlong_log("Đặt chip thật đang TẮT: chỉ ghi kế hoạch, không bấm chip.")
        self._longlong_log("Tự chốt kết quả: %s." % ("BẬT" if self.longlong_auto_result_on()
                                                     else "TẮT"))
        self._longlong_render_live()

    def longlong_reset(self):
        sim = self._longlong_simulation()
        sim.reset()
        if getattr(self, "longlong_tracker", None):
            self.longlong_tracker.reset()
        self.longlong_pending_place = None
        self.longlong_place_attempts = 0
        self.longlong_arm_hand = None
        self._longlong_log("Đã reset phiên và bộ đếm ván.")
        self._longlong_refresh()

    # ------------------------------------------------------------------ render
    def _longlong_refresh(self):
        if not hasattr(self, "longlong_next"):
            return
        strategy = self._longlong_strategy()
        plan = strategy.plan()
        if plan["action"] == "bet":
            self.longlong_next.config(
                text="Tay kế tiếp: ĐẶT ô %s · %s đv (bậc %d/%d, ăn %.2f)" % (
                    DOOR_NAME, fmt(plan["stake"]), plan.get("step", 0) + 1,
                    len(strategy.ladder), PAYOUT["M"]), fg=ACCENT)
        elif plan["action"] == "stop":
            self.longlong_next.config(
                text="PHIÊN ĐÃ DỪNG · chạm đáy thang (bậc %d) · bấm Reset phiên để chạy lại"
                % (len(strategy.ladder)), fg=RED)
        else:
            self.longlong_next.config(text="Tay kế tiếp: CHỜ · %s" % plan["reason"], fg=MUTED)
        self.longlong_state.config(text=strategy.note)
        sim = self._longlong_simulation()
        total = longlong.ladder_total(strategy.ladder)
        self.longlong_ladder_note.config(
            text="Tổng thang: %s đv = %sđ · %d bậc · đang ở bậc %d" % (
                fmt(total), fmt(total * 1000), len(strategy.ladder), strategy.step + 1))
        profit = sim.profit
        self.longlong_profit.config(
            text="Lãi/lỗ phiên: %s%s đv · %d tay đã cược" % (
                "+" if profit >= 0 else "-",
                fmt(abs(profit) / 1000.0), len(sim.log)),
            fg=GREEN if profit >= 0 else RED)
        if not strategy.log:
            self.longlong_log.config(text="Chưa có tay nào.")
            return
        start = max(1, len(strategy.log) - 7)
        lines = ["Tay %d: ra %s · cược %s vào ô %s · %s" % (
            index, raw_label(row["result"]), fmt(row["stake"]), DOOR_NAME,
            (("THẮNG +" if row["won"] else "THUA -") + fmt(abs(row["delta"]))))
            for index, row in enumerate(strategy.log[-8:], start=start)]
        self.longlong_log.config(text="\n".join(lines))

    def _longlong_render_live(self):
        """Vẽ số dư thật + tổng đặt từng ô do Controller đọc được từ bàn."""
        if not hasattr(self, "longlong_balance"):
            return
        running = bool(getattr(self, "longlong_live_running", False))
        if hasattr(self, "longlong_live_button"):
            self.longlong_live_button.config(text="Tắt cập nhật" if running else "Bật cập nhật")
        state = getattr(self, "longlong_live_state", None)
        if not isinstance(state, dict) or not state.get("ok"):
            error = (state or {}).get("error") or "chưa đọc"
            self.longlong_balance.config(text="—", fg=MUTED)
            self.longlong_balance_note.config(text="Chưa đọc được số dư: %s" % error, fg=MUTED)
            self.longlong_board.config(text="")
            self.longlong_live_status.config(text="")
            if hasattr(self, "top_balance"):
                self.top_balance.config(text="Số dư: —", fg="#8aa0ba")
            return
        balance = state.get("balance_vn2")
        if balance is None:
            self.longlong_balance.config(text="—", fg=MUTED)
        else:
            self.longlong_balance.config(text="%s VN2" % fmt_vn2(balance),
                                         fg=GREEN if balance > 0 else RED)
        stamp = time.strftime("%H:%M:%S", time.localtime(state.get("read_at") or time.time()))
        self.longlong_balance_note.config(
            text="%s · tổng đặt trên bàn %s VN2 · cập nhật %s · %s" % (
                ("≈ %sđ" % fmt(balance * 1000)) if balance is not None else "chưa có số",
                fmt_vn2(state.get("total_bet_vn2")), stamp,
                "đọc 2 giây/lần" if running else "đọc một lần"),
            fg=MUTED)
        doors = state.get("doors") or {}
        parts = []
        for role, label in (("JNS_B", "B vàng"), ("JNS_A", "A đỏ"), ("JNS_C", "C mix")):
            door = doors.get(role) or {}
            pieces = "%s %s" % (label, fmt_vn2(door.get("amount")))
            if door.get("my_chips"):
                pieces += " (%d chip của bạn)" % door["my_chips"]
            parts.append(pieces)
        self.longlong_board.config(text="Bàn đang đặt: " + " · ".join(parts))
        self.longlong_live_status.config(text=state.get("status_note") or "")
        if hasattr(self, "top_balance"):
            self.top_balance.config(
                text="Số dư: " + ("%s VN2" % fmt_vn2(balance) if balance is not None else "—"),
                fg=GREEN if (balance or 0) > 0 else RED)
