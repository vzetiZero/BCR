# -*- coding: utf-8 -*-
"""Tầng View: vẽ lại trạng thái Model lên widget (bảng bàn, banner săn bàn, chip).

Mọi hàm ở đây chỉ đọc state của Controller rồi đặt text/màu, không đổi nghiệp vụ.
"""

from bcr.config import *
from bcr.models import engine as E
from bcr.models import method as ME
from bcr.models import chips as CHIP
from bcr.models import money as M
from bcr.models.live_sim import signed, road_is_current, row_problem


class RenderMixin:
    """Vẽ lại số liệu phiên, bảng bàn và trạng thái chip lên widget."""

    def refresh(self):
        book = self.sim.money
        decisive = book.wins + book.losses
        values = (M.fmt_money(book.balance), signed(book.profit), "%+.2f%%" % (book.profit / book.start_capital * 100)
                  if book.start_capital else "—", "%.0f%% / %.0f%%" % (book.wins / decisive * 100, book.losses / decisive * 100)
                  if decisive else "— / —")
        notes = ("Hiện tại", "%d tay · %d hòa" % (book.hands_played, book.pushes),
                 "÷ vốn đầu", "không tính hòa")
        for i, (value, note) in enumerate(zip(values, notes)):
            self.kpi_values[i].config(text=value, fg=(GREEN if book.profit >= 0 else RED) if i in (1, 2) else TEXT)
            self.kpi_notes[i].config(text=note)
        if self.browser.connected:
            self.connection_text = self.browser.note or "Đã nối Chrome (driver)."
        elif not self.chrome_busy:
            self.connection_text = "Chế độ driver: bấm “Mở Chrome” để app mở Chrome và tự vào sảnh."
        self.connection_note.config(text=self.connection_text)
        if hasattr(self, "top_chrome_button"):
            # Thanh trên cùng luôn hiện: đồng bộ trạng thái để người dùng biết Chrome đã
            # mở chưa và bấm mở lại được ngay khi nút bị khoá trong lúc đang khởi động.
            busy = self.chrome_busy
            self.top_chrome_button.config(state="disabled" if busy else "normal")
            self.top_attach_button.config(state="disabled" if busy else "normal")
            self.top_stop_button.config(state="disabled" if busy else "normal")
            connected = bool(getattr(self.browser, "connected", False))
            self.chrome_status.config(
                text=("Chrome: đã kết nối" if connected else "Chrome: chưa kết nối"),
                fg=GREEN if connected else "#8aa0ba")
        self.connection_label.config(text=self._window_text(self._window_config()))
        self.open_button.config(state="disabled" if self.chrome_busy else "normal")
        self.attach_button.config(state="disabled" if self.chrome_busy else "normal")
        self.disconnect_button.config(text="Ngắt kết nối" if self.browser.mode == "cdp" else "Đóng")
        self.enter_lobby_button.config(state="disabled" if self.chrome_busy or self.sweep_busy else "normal")
        self.sweep_button.config(state="disabled" if self.chrome_busy or self.sweep_busy else "normal")
        self.auto_open_button.config(state="disabled" if self.chrome_busy or self.sweep_busy
                                     or self.open_table_busy else "normal")
        self.sweep_note.config(text=self.sweep_note_text,
                               fg=self._sweep_note_color(self.sweep_note_text))
        self._refresh_hunt_banner()
        if hasattr(self, "tables_note"):
            self.tables_note.config(text="Số bàn muốn chơi: %d · đã xong %d bàn trong phiên." % (
                self._tables_target(), self.tables_done), fg=MUTED)
        plan = self.sim.pending or self.sim.plan()
        side = plan["side"]
        self.plan_side.config(text=E.SIDE_NAME.get(side, "CHỜ BÀN").upper(), fg=COLORS.get(side, MUTED))
        self.plan_stake.config(text=("%s VN2 · bậc %d/%d · %s" % (
            M.fmt_money(plan["stake"]), min(plan["ladder_step"] + 1, len(self.sim.ladder)),
            len(self.sim.ladder), "đã chốt" if self.sim.running else "dự kiến")) if side else "Chưa có kế hoạch")
        strong = plan["strong"]
        for i, label in enumerate(self.cycle_labels):
            cell_side = strong if i < ME.strong_hands(self.sim.profile) else (
                ("P" if strong == "B" else "B") if strong else None)
            active = i == plan["block_pos"] and side
            label.config(text=cell_side or str(i + 1), bg=COLORS.get(cell_side, BG) if active else BG,
                         fg="white" if active else COLORS.get(cell_side, MUTED))
        self.checks_label.config(text="\n".join(("Đạt · " if c["ok"] else "Chờ · ") + c["name"] for c in plan["checks"]))
        self._update_chip_status()
        self.sim_note.config(text=self.sim.note, fg=ACCENT if self.sim.running else MUTED)
        self._render_tables()
        self.history_note.config(text="%d tay · %d thắng / %d thua / %d hòa · Lãi/lỗ %s VN2\nSụt vốn lớn nhất: %s đ" % (
            book.hands_played, book.wins, book.losses, book.pushes, signed(book.profit), M.fmt_money(book.max_drawdown)))
        if self.history_size != len(self.sim.log):
            for item in self.history_tree.get_children():
                self.history_tree.delete(item)
            groups = {}
            for row in self.sim.log:
                group_key = row.get("table_key") or row.get("table") or "(B\u00e0n kh\u00f4ng r\u00f5)"
                groups.setdefault(group_key, []).append(row)
            for group_key, rows in groups.items():
                name = rows[-1].get("table") or str(group_key)
                wins = sum(row.get("outcome") == "win" for row in rows)
                losses = sum(row.get("outcome") == "loss" for row in rows)
                pushes = sum(row.get("outcome") == "push" for row in rows)
                profit = sum(float(row.get("delta", 0) or 0) for row in rows)
                parent = self.history_tree.insert("", "end", tags=("table_group",), values=(
                    "", "%s \u00b7 %d l\u01b0\u1ee3t" % (name, len(rows)), "", "%dT/%dB/%dH" % (wins, losses, pushes),
                    "", "", signed(profit), M.fmt_money(rows[-1].get("balance_after", 0))))
                for row in rows:
                    self.history_tree.insert(parent, "end", tags=(row.get("outcome", ""),), values=(
                        time.strftime("%H:%M:%S", time.localtime(row["time"])), "", row.get("hand", ""),
                        row.get("bet", ""), row.get("result", ""), M.fmt_money(row.get("stake", 0)),
                        signed(row.get("delta", 0)), M.fmt_money(row.get("balance_after", 0))))
            self.history_size = len(self.sim.log)

    def _render_tables(self):
        if not hasattr(self, "live_tree"):
            return
        rows = []
        for row in self.tables.values():
            if time.time() - row["ts"] > LIVE_STALE:
                continue
            info = ME.check_table(list(row["letters"]), self.sim.min_hands, self.sim.max_hands,
                                  self.sim.max_streak, self.sim.min_lead)
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
                verdict, status = "Bàn đặc biệt · không theo dõi", "Đọc được Canvas"
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

    def _refresh_hunt_banner(self):
        """Hiện/ẩn banner chờ khi vòng tự quét tìm bàn đang chạy, kèm đếm ngược và lượt thử."""
        take_profit = bool(getattr(self.sim, "take_profit_done", False))
        filter_exit = bool(getattr(self.sim, "filter_exit_done", False))
        waiting = bool(getattr(self, "cycle_waiting", False)) and (
            bool(self.auto_hunt_var.get()) or take_profit or filter_exit)
        if not waiting:
            self.hunt_banner.pack_forget()
            return
        self._hunt_frame = getattr(self, "_hunt_frame", 0) + 1
        spinner = "●○"[self._hunt_frame % 2]
        remaining = max(0.0, self.cycle_retry_at - time.time()) if self.cycle_retry_at else 0.0
        if filter_exit:
            if remaining > 0:
                text = "Bàn vi phạm điều kiện lọc · thử rời bàn lại sau %.0f giây." % remaining
            else:
                text = "Bàn vi phạm điều kiện lọc · đang rời bàn để quét bàn khác…"
        elif take_profit:
            # Đã chốt lời: banner chỉ nhắc việc đóng bàn, không phải săn bàn mới.
            if remaining > 0:
                text = "Đã chốt lời · thử đóng bàn lại sau %.0f giây (lượt %d)." % (
                    remaining, self.cycle_attempts)
            else:
                text = ("Đã chốt lời · đang đóng bàn để dừng phiên…" if self._session_complete()
                        else "Đã chốt lời · đang rời bàn để sang bàn kế tiếp…")
        elif remaining > 0:
            text = "Đang tự quét tìm bàn… nghỉ %.0f giây nữa rồi quét lại (đã thử %d lượt)." % (
                remaining, self.cycle_attempts)
        else:
            text = "Đang tự quét tìm bàn đạt chuẩn… (đã thử %d lượt)." % self.cycle_attempts
        self.hunt_spinner.config(text=spinner)
        self.hunt_text.config(text=text)
        anchor = getattr(self, "_hunt_anchor", None)
        if anchor is not None and anchor.winfo_exists():
            self.hunt_banner.pack(fill="x", pady=(0, 12), before=anchor)
        else:
            self.hunt_banner.pack(fill="x", pady=(0, 12))

    def _sweep_note_color(self, text):
        """Màu dòng trạng thái quét để người dùng nhận biết nhanh trạng thái.

        Lỗi/không đạt → đỏ; đang quét/đang mở/mở thành công → xanh lá; còn lại trung tính.
        """
        text = str(text or "")
        bad = ("Chưa", "không", "Không", "Gắn Chrome", "Tạm dừng", "Dừng", "lỗi", "Lỗi",
               "thất bại", "mất phiên", "Chưa bật")
        good = ("Đang quét", "Đang mở", "Đã vào bàn", "Đã nhận", "Đã bật", "Đã lưu",
                "đang quét lại", "quét lại ngay")
        if any(word in text for word in bad):
            return RED
        if any(word in text for word in good):
            return GREEN
        return ACCENT

    def _sync_cycle_strip(self):
        """Dải ô tay của khối: 6 ô cho 3-3, 5 ô cho 3-2, dựng lại khi đổi phương pháp."""
        for label in self.cycle_labels:
            label.destroy()
        self.cycle_labels = []
        for i in range(self.sim.cycle_hands):
            label = self._label(self.cycle_strip, str(i + 1), 10, MUTED, True, width=3, pady=3)
            label.pack(side="left", padx=(0, 5))
            self.cycle_labels.append(label)

    def format_money_inputs(self):
        """Định dạng lại mọi ô nhập tiền khi giá trị được set bằng code, không có phím thật."""
        alive = []
        for entry, regroup in self.money_inputs:
            if not entry.winfo_exists():
                continue
            regroup()
            alive.append((entry, regroup))
        self.money_inputs = alive

    def _update_chip_status(self):
        """So chip thật với kế hoạch đang chốt và vẽ trạng thái khớp/lệch."""
        if not hasattr(self, "chip_status"):
            return
        plan = self.sim.pending or self.sim.plan()
        unit = (self.chip_live or {}).get("unit") or CHIP.DEFAULT_CHIP_UNIT
        self.chip_check = ME.compare_chips(plan.get("side"), plan.get("stake") or 0,
                                           self.chip_live, unit=unit)
        check = self.chip_check
        status = check["status"]
        self.chip_status.config(text="%s · %s" % (ME.CHIP_STATUS_LABEL.get(status, status), check["note"]),
                                fg={"match": GREEN, "no_bet": MUTED, "unknown": MUTED}.get(status, RED))
        live = self.chip_live or {}
        lines = []
        chips = check.get("suggested_chips") or []
        if chips and check["side"]:
            lines.append("Cần bấm: %s (%d chip) = %s VN2" % (
                " + ".join(str(c) for c in chips), len(chips), M.fmt_money(check["expected"])))
        if live.get("ok"):
            parts = []
            for side in ("B", "P", "T"):
                entry = (live.get("spots") or {}).get(side) or {}
                if entry.get("amount_vnd"):
                    chips_txt = " + ".join(str(c) for c in entry.get("chips", []))
                    parts.append("%s %s%s" % (E.SIDE_NAME[side], M.fmt_money(entry["amount_vnd"]),
                                              (" [" + chips_txt + "]") if chips_txt else ""))
            lines.append("Trên bàn: %s" % (" · ".join(parts) if parts else "chưa có chip"))
            if live.get("total") is not None:
                lines.append("Tổng đặt: %s VN2" % M.fmt_money(live["total"]))
        elif live.get("error"):
            lines.append("Chưa đọc được chip (%s)." % live["error"])
        else:
            lines.append("Vào bàn để đọc chip đang đặt.")
        self.chip_detail.config(text="\n".join(lines))

    def _set_place_status(self, ok, note):
        if hasattr(self, "place_status"):
            self.place_status.config(text=note, fg=(GREEN if ok else RED))
