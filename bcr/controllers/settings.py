# -*- coding: utf-8 -*-
"""Tầng Controller: áp cấu hình người dùng nhập trên View vào Model + Adapter.

Gồm bộ lọc cầu, chế độ chặn, thang cược, cỡ chữ, cấu hình cửa sổ và xuất CSV.
"""
import csv
import threading
import time

from tkinter import filedialog, messagebox

from bcr.adapters import browser as BR
from bcr.config import *
from bcr.models import method as ME
from bcr.models import money as M
from bcr.models.live_sim import LiveSimulation


class SettingsMixin:
    """Áp cấu hình từ View xuống Model/Adapter và xuất báo cáo CSV."""

    def apply_settings(self):
        try:
            values = {k: (M.parse_money(v.get()) if k == "capital" else float(v.get().strip()))
                      for k, v in self.entries.items()}
            if not all(math.isfinite(v) for v in values.values()):
                raise ValueError("Vốn và chốt lời phải là số hợp lệ.\n"
                                 "Vốn nhập kiểu 20,000,000; chốt lời ≥ 1%.")
            if values["take_profit_pct"] < 1:
                raise ValueError("Chốt lời phải ≥ 1%.")
            profile = self.profile_var.get()
            stakes = ME.ladder_stakes(profile, self._ladder_from_inputs())
            if stakes[0] > values["capital"] or values["capital"] <= 0:
                raise ValueError("Vốn (%s) phải lớn hơn 0 và không nhỏ hơn bậc 1 (%s)." % (
                    M.fmt_money(values["capital"]), M.fmt_money(stakes[0])))
        except ValueError as exc:
            messagebox.showerror("Thông số chưa hợp lệ", str(exc), parent=self.root)
            return
        book = M.MoneyBook(unit=stakes[0], **values)
        for var, stake in zip(self.ladder_vars, stakes):
            var.set(M.fmt_input_money(stake))
        self.ladder_store[profile] = [var.get() for var in self.ladder_vars]
        self.entries["capital"].set(M.fmt_input_money(values["capital"]))
        if not messagebox.askyesno("Tạo phiên mới", "Lưu bản sao phiên hiện tại, dừng theo dõi và bắt đầu với vốn mới?", parent=self.root):
            return
        if not self._save_state():
            return
        try:
            self._archive_state()
        except OSError as exc:
            messagebox.showerror("Không lưu được bản sao", str(exc), parent=self.root)
            return
        self.sim = LiveSimulation(book, profile=profile, ladder=stakes,
                                  min_hands=self.sim.min_hands, max_hands=self.sim.max_hands,
                                  max_streak=self.sim.max_streak,
                                  live_exit_streak=self.sim.live_exit_streak,
                                  block_mode=self.block_mode_var.get())
        self.sim.on_arm = self.place_armed_bet
        self.tables_done = 0
        self._sync_cycle_strip()
        self._sync_method_texts()
        self.history_size = -1
        self._save_state()
        self.refresh()

    def apply_block_mode(self):
        mode = self.block_mode_var.get()
        if mode == "alternate":
            mode = ME.BLOCK_MODE_REPEAT
            self.block_mode_var.set(mode)
        if mode not in (ME.BLOCK_MODE_RECALCULATE, ME.BLOCK_MODE_REPEAT):
            mode = ME.BLOCK_MODE_RECALCULATE
            self.block_mode_var.set(mode)
        self.sim.block_mode = mode
        self._sync_method_texts()
        self._save_state()
        self.refresh()

    def apply_road_filter(self):
        """Lưu bộ lọc cầu: khoảng tay đã đi (10–50) và số tay bệt tối đa còn nhận."""
        try:
            low = int(self.hands_min_var.get().strip())
            high = int(self.hands_max_var.get().strip())
            streak = int(self.streak_var.get().strip())
            live_exit_streak = int(self.live_exit_streak_var.get().strip())
            min_lead = int(self.min_lead_var.get().strip())
        except (ValueError, TypeError):
            messagebox.showerror("Bộ lọc cầu chưa hợp lệ",
                                 "Nhập số tay đã đi tối thiểu/tối đa (ví dụ 10 và 50), "
                                 "số tay bệt tối đa (ví dụ 3) và chênh lệch B/P tối thiểu (ví dụ 3).",
                                 parent=self.root)
            return
        low, high, streak, min_lead = self.sim.set_road_filter(low, high, streak, min_lead)
        live_exit_streak = self.sim.set_live_exit_streak(live_exit_streak)
        self.hands_min_var.set(str(low))
        self.hands_max_var.set(str(high))
        self.streak_var.set(str(streak))
        self.live_exit_streak_var.set(str(live_exit_streak))
        self.min_lead_var.set(str(min_lead))
        self._save_state()
        if hasattr(self, "hands_note"):
            self.hands_note.config(
                text="\u0110ang l\u1ecdc s\u1ea3nh %d?%d tay; b\u1ec7t t\u1ed1i \u0111a %d tay; ch\u00eanh B/P \u2265 %d tay. Trong b\u00e0n: tho\u00e1t khi b\u1ec7t \u0111\u1ea1t %d tay." % (low, high, streak, min_lead, live_exit_streak), fg=GREEN)
        self.refresh()

    def apply_mouse_setting(self):
        """Bật/tắt chuột người thật ngay cho lượt bấm sảnh kế tiếp."""
        enabled = bool(self.human_mouse_var.get())
        self.stream.human_mouse = enabled
        self.sweep_note_text = ("Đã bật chuột người thật (WindMouse + ghost-cursor) cho các cú bấm sảnh."
                                if enabled else "Đã tắt chuột người thật; dùng cú bấm 3 sự kiện như cũ.")
        self._save_state()
        self.refresh()

    def apply_window_settings(self):
        window = self._window_config()
        # Ô nhập được viết lại đúng giá trị đã kẹp để người dùng thấy con số app thật dùng.
        self.chrome_w_var.set(str(window["width"]))
        self.chrome_h_var.set(str(window["height"]))
        self.chrome_scale_var.set(str(window["percent"]))
        self._save_state()
        pos = self._window_pos(window)
        self.connection_text = self._window_text(window)
        if not self.browser.connected:
            self.sweep_note_text = ("Đã lưu cấu hình cửa sổ Chrome (%s). Mở Chrome là app áp dụng "
                                    "luôn; đổi tỉ lệ thì cần mở lại Chrome." % self._window_text(
                                        window).replace("CHROME · ", ""))
            self.refresh()
            return
        self.chrome_busy = True
        def work():
            try:
                sized, note = self.browser.resize_window(window, pos=pos)
                self.events.put(("window_applied", (sized, note)))
            except Exception as exc:
                self.events.put(("window_applied", (False, "Không đổi được cửa sổ (%s)." % type(exc).__name__)))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def _window_config(self):
        return BR.normalize_window((self.chrome_w_var.get(), self.chrome_h_var.get()),
                                   self.chrome_scale_var.get(), self.chrome_fullscreen_var.get())

    def _window_pos(self, window):
        if window["fullscreen"]:
            return None
        return (max(20, self.root.winfo_screenwidth() - window["width"] - 20), 30)

    def _window_text(self, window):
        return ("CHROME · toàn màn hình · tỉ lệ %d%%" % window["percent"] if window["fullscreen"]
                else "CHROME · %d × %d · tỉ lệ %d%%" % (window["width"], window["height"],
                                                         window["percent"]))

    def _on_ladder_mode_change(self):
        """Đổi cơ chế: bảo lưu bậc 1 và tự điền/nhả các ô bậc cược cho khớp."""
        multiply = self.ladder_mode_var.get() == "multiply"
        if multiply:
            # Lấy bậc 1 hiện đang hiển thị làm gốc cho cơ chế tự nhân.
            try:
                base = M.parse_money(self.ladder_vars[0].get())
            except (ValueError, IndexError):
                base = ME.DEFAULT_LADDER[self.profile_var.get()][0]
            self.ladder_base_var.set(M.fmt_input_money(base))
        self._rebuild_ladder_inputs()

    def _ladder_from_inputs(self):
        """Đọc các ô bậc cược thành số tiền; báo rõ ô nào nhập sai."""
        if self.ladder_mode_var.get() == "multiply":
            try:
                base = M.parse_money(self.ladder_base_var.get())
            except ValueError:
                raise ValueError("Bậc 1 (%r) chưa hợp lệ — nhập số tiền, ví dụ 20,000." % self.ladder_base_var.get())
            if base <= 0:
                raise ValueError("Bậc 1 phải lớn hơn 0.")
            stakes = ME.multiply_ladder(base, self.profile_var.get())
            self.ladder_store[self.profile_var.get()] = [M.fmt_input_money(s) for s in stakes]
            return stakes
        stakes = []
        for i, var in enumerate(self.ladder_vars):
            try:
                stakes.append(M.parse_money(var.get()))
            except ValueError:
                raise ValueError("Bậc %d (%r) chưa hợp lệ — nhập số tiền, ví dụ 160,000." % (
                    i + 1, var.get()))
        return stakes

    def export_csv(self):
        if not self.sim.log:
            messagebox.showinfo("Chưa có dữ liệu", "Chưa có dữ liệu đối chiếu để xuất.", parent=self.root)
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
