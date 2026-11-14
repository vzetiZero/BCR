# -*- coding: utf-8 -*-
"""Tầng Controller: vòng tự quét sảnh → chọn bàn → vào bàn → rời bàn.

Điều phối Adapter (sảnh, mở bàn) và Model (điều kiện cầu) nhưng không vẽ widget.
"""
import threading
import time

from tkinter import messagebox

from bcr.config import *
from bcr.models import engine as E
from bcr.models import method as ME
from bcr.models import money as M
from bcr.models.live_sim import is_fast_table, road_is_current, same_shoe, row_problem


class HuntMixin:
    """Săn bàn tự động: quét sảnh, mở bàn đạt điều kiện, rời bàn đúng lúc."""

    def auto_open_table(self, allow_sweep=False, exclude_key=None):
        if self.chrome_busy or self.sweep_busy or self.open_table_busy or self.exit_requested:
            return
        if getattr(self.sim, "take_profit_done", False):
            # Đã chốt lời: chỉ đóng bàn rồi dừng, không mở bàn mới.
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
        skipped_fast = 0
        for row in self.tables.values():
            if not row.get("table_id") or row["key"] == exclude_key or row_problem(row):
                continue
            if not road_is_current(row):
                continue
            # Không săn vào bàn Siêu Tốc Độ: cửa sổ đặt cược ngắn hơn thời gian chờ
            # khay chip + lướt chuột + xác nhận, nên mọi tay đều rơi vào not_placed.
            if is_fast_table(row.get("label")):
                skipped_fast += 1
                continue
            info = ME.check_table(list(row["letters"]), self.sim.min_hands, self.sim.max_hands,
                                  self.sim.max_streak, self.sim.min_lead)
            if info["eligible"]:
                candidates.append((info["hands"], row, info))
        if not candidates:
            if not self.cycle_waiting and self.auto_hunt_var.get():
                # A first lobby scan can return tables that simply do not meet
                # the filters. Start the same visible retry loop used later in
                # the hunt instead of leaving auto-hunt idle after that scan.
                self.cycle_waiting = True
                self.cycle_attempts = 0
                self.cycle_table_key = exclude_key
            reason = "Chưa có bàn đạt 3 điều kiện"
            if skipped_fast:
                reason += " · bỏ qua %d bàn Siêu Tốc Độ (cửa sổ cược quá ngắn)" % skipped_fast
            if self._schedule_cycle_retry(reason):
                return
            if self.cycle_waiting or not allow_sweep:
                self.sweep_note_text = ("Chưa có bàn đạt 3 điều kiện · bấm “Quét toàn sảnh” để thử lại, "
                                        "hoặc bật “Tự quét tìm bàn” để app tự lặp cho tới khi vào được.")
                self.refresh()
                return
            self.cycle_attempts += 1
            self.sweep_note_text = "Chưa có bàn đạt 3 điều kiện (lượt %d) · đang quét lại sảnh…" % (
                self.cycle_attempts)
            self.sweep_lobby()
            return
        ideal = (self.sim.min_hands + self.sim.max_hands) / 2.0
        candidates.sort(key=lambda item: (abs(item[0] - ideal), item[1]["label"].casefold()))
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

    def _on_hunt_toggle(self):
        """Bật “Tự quét” thì quét NGAY lần đầu, không chờ hết chu kỳ nghỉ."""
        self._save_state()
        if not self.auto_hunt_var.get():
            return
        if self.sim.running or self.cycle_waiting or self.sweep_busy:
            return
        if not self._cdp_available():
            self.sweep_note_text = "Gắn Chrome đang mở trước rồi bật “Tự quét”."
            self.refresh()
            return
        # Vào vòng săn và kích hoạt lượt quét đầu tiên ngay lập tức.
        self.cycle_waiting = True
        self.cycle_attempts = 0
        self.cycle_table_key = None
        self.cycle_retry_at = 0.0
        self.sweep_lobby()

    def cancel_auto_hunt(self):
        """Người dùng chủ động dừng vòng tự quét tìm bàn."""
        self.cycle_waiting = False
        self.cycle_retry_at = 0.0
        self.cycle_table_key = None
        self.sweep_note_text = "Đã dừng tự quét tìm bàn."
        self._trace("hunt_cancelled")
        self.refresh()

    def _schedule_cycle_retry(self, reason, exclude_key=None):
        """Vòng tìm bàn không có trần số lần: nghỉ CYCLE_RETRY_SECONDS rồi quét lại, tới khi vào được bàn."""
        if self.session_lost:
            return False
        if getattr(self.sim, "take_profit_done", False):
            # Sau chốt lời không còn vòng săn bàn nào để hẹn giờ.
            return False
        if not (self.cycle_waiting and self.auto_hunt_var.get()) or self.exit_requested:
            return False
        if exclude_key:
            self.cycle_table_key = exclude_key
        # Lượt đầu tiên của vòng tự quét phải chạy NGAY: trước đây luôn nghỉ 15 giây
        # nên lần bấm “Tự quét” đầu tiên trông như bị treo. Chỉ áp dụng cho việc SĂN BÀN;
        # chốt lời/sai bộ lọc vẫn nghỉ để tránh đóng bàn liên tục.
        hunting = not (getattr(self.sim, "take_profit_done", False)
                       or getattr(self.sim, "filter_exit_done", False))
        first_attempt = hunting and self.cycle_attempts == 0
        self.cycle_attempts += 1
        wait = 0 if first_attempt else CYCLE_RETRY_SECONDS
        self.cycle_retry_at = time.time() + wait
        self.sweep_note_text = ("%s · quét lại ngay…" % reason if first_attempt else
                                "%s · tự quét lại sau %d giây (đã thử %d lượt)." % (
                                    reason, CYCLE_RETRY_SECONDS, self.cycle_attempts))
        self.refresh()
        return True

    def leave_table_for_lobby(self, take_profit=False):
        """Đưa tab về danh sách bàn.

        Bình thường: hết khối thì quét - chọn - mở bàn mới.
        ``take_profit=True``: chỉ rời bàn và dừng phiên, không quét/mở bàn mới.
        """
        take_profit = bool(take_profit or getattr(self.sim, "take_profit_done", False))
        filter_exit = bool(getattr(self.sim, "filter_exit_done", False))
        if self.exit_requested:
            return
        if not take_profit and not filter_exit and not self.auto_cycle_var.get():
            return
        self.cycle_waiting = True
        self.cycle_attempts = 0
        self.cycle_table_key = self.sim.table_key
        self.cycle_retry_at = 0.0
        self._trace("take_profit_exit" if take_profit else "filter_exit" if filter_exit else "block_done",
                    dict(table=self.sim.table_key, hands=self.sim.block_hands,
                         target_pct=self.sim.money.take_profit_pct))
        if self.chrome_busy or self.sweep_busy or self.open_table_busy:
            self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
            self.sweep_note_text = (
                "Đã chốt lời nhưng app đang bận · sẽ tự rời bàn sau %d giây." % CYCLE_RETRY_SECONDS
                if take_profit else
                "App đang bận nên việc đổi bàn hẹn quét lại sau %d giây." % CYCLE_RETRY_SECONDS)
            self.refresh()
            return
        if not self._cdp_available():
            self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
            self.sweep_note_text = (
                "Đã chốt lời nhưng chưa gắn Chrome · sẽ rời bàn khi kết nối lại."
                if take_profit else
                "Chưa gắn Chrome · sẽ tự quét tìm bàn ngay khi kết nối lại.")
            self.refresh()
            return
        self.sweep_busy = True
        self.sweep_note_text = (
            "Đã đạt mục tiêu lãi %.0f%% vốn · rời bàn, dừng phiên…"
            % self.sim.money.take_profit_pct if take_profit else
            "Bàn không còn đạt điều kiện lọc · đang rời bàn để quét lại…" if filter_exit else
            "Đã đi đủ %d tay · rời bàn, về danh sách bàn Baccarat…" % self.sim.cycle_hands)
        def work():
            try:
                self.events.put(("cycle_back", self.stream.back_to_lobby()))
            except Exception as exc:
                self.events.put(("cycle_back", (False, "Không về được sảnh (%s)." % type(exc).__name__)))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def _start_next_table(self):
        """Một bàn vừa chốt lời và đã rời khỏi bàn thành công, nhưng chưa đủ số bàn cấu hình.

        Đồng bộ vốn ban đầu = số dư hiện tại để bàn kế tiếp chốt lời theo % trên số tiền
        mới nhất (người dùng không phải sửa tay), rồi mở lại vòng quét tìm bàn.
        """
        book = self.sim.money
        balance = book.rebase_to_balance()
        if "capital" in self.entries:
            self.entries["capital"].set(M.fmt_input_money(balance))
        self.sim.take_profit_done = False
        self.sim.note = "Bàn trước đã chốt lời · vốn mới %s · đang tìm bàn kế tiếp." % M.fmt_money(balance)
        self.cycle_waiting = True
        self.cycle_attempts = 0
        self.cycle_retry_at = 0.0
        self.cycle_table_key = self.sim.table_key
        self.sweep_note_text = ("Bàn %d/%d đã chốt lời · vốn mới %s · đang quét tìm bàn kế tiếp…" % (
            self.tables_done, self._tables_target(), M.fmt_money(balance)))
        self._save_state()
        self.sweep_lobby()

    def _tables_target(self):
        """Số bàn muốn chơi trong phiên (≥ 1); ô nhập sai thì giữ mức tối thiểu 1."""
        try:
            return max(1, int(str(self.tables_var.get()).strip()))
        except (ValueError, TypeError, AttributeError):
            return 1

    def _session_complete(self):
        return self.tables_done >= self._tables_target()

    def apply_tables_target(self):
        """Lưu số bàn muốn chơi: mỗi bàn đủ chốt lời thì rời bàn, tự quét vào bàn kế tiếp."""
        try:
            count = int(self.tables_var.get().strip())
            if count < 1:
                raise ValueError()
        except (ValueError, TypeError):
            messagebox.showerror("Số bàn chưa hợp lệ",
                                 "Nhập số bàn muốn chơi ≥ 1 (ví dụ 3).", parent=self.root)
            return
        self.tables_var.set(str(count))
        self._save_state()
        self._sync_method_texts()
        self.refresh()

    def _auto_start_verified_table(self):
        key = self.auto_start_table_key
        if not key:
            return
        if getattr(self, "_wallet_startup_sync_pending", False):
            self.sweep_note_text = "Đang chờ đồng bộ vốn theo số dư ví trước khi theo dõi bàn."
            return
        row = self.tables.get(key)
        if not row or row.get("source") != "evo-bead" or row.get("screen_stable", 0) < 2:
            if time.time() > self.auto_start_deadline:
                self.sweep_note_text = ("Vào bàn rồi nhưng %d giây chưa đọc được cầu bàn đã mở hai lần giống nhau. "
                                        "Đang tìm bàn khác…" % ENTRY_WAIT)
                self.auto_start_table_key = None
                self._cycle_search(key)
            return
        problem = row_problem(row)
        if problem or not same_shoe(self.auto_start_letters, row["letters"]):
            self.sweep_note_text = ("Chưa tự bắt đầu: " + problem) if problem else (
                "Cầu trong bàn (%d tay) không cùng shoe với cầu đã lọc ở sảnh (%d tay)." % (
                    len(row["letters"]), len(self.auto_start_letters)))
            self.auto_start_table_key = None
            self._cycle_search(key)
            return
        if self.sim.arm_at_entry(row):
            self.sweep_note_text = "Đã vào bàn và bắt đầu theo dõi · %s" % self.sim.note
            self.auto_start_table_key = None
            self.cycle_waiting = False
            self.cycle_attempts = 0
            self.cycle_retry_at = 0.0
            self.cycle_table_key = None
        else:
            self.sweep_note_text = "Chưa thể bắt đầu: " + self.sim.note
            self.auto_start_table_key = None
            self._cycle_search(key)

    def _cycle_search(self, key):
        """Chu trình đang chờ bàn mới: loại bàn vừa hỏng và tìm bàn khác trong sảnh."""
        if self.cycle_waiting:
            self.auto_open_table(allow_sweep=True, exclude_key=key)

    def _resume_cycle_hunt(self, now):
        """Hết thời gian nghỉ: phục hồi phiên nếu đang văng, ngược lại quét-chọn-vào bàn."""
        take_profit = bool(getattr(self.sim, "take_profit_done", False))
        filter_exit = bool(getattr(self.sim, "filter_exit_done", False))
        if not (self.cycle_waiting and (self.auto_hunt_var.get() or take_profit or filter_exit)
                and self.cycle_retry_at and now >= self.cycle_retry_at):
            return
        self.cycle_retry_at = 0.0
        if self.exit_requested or self.sim.running or self.auto_start_table_key:
            return
        if take_profit or filter_exit:
            # Đang chờ đóng bàn sau chốt lời: thử lại đúng việc rời bàn,
            # không quét và không mở bàn mới.
            self.leave_table_for_lobby(take_profit=take_profit)
            return
        # Vẫn đang mất phiên: thử mở lại EVO thay vì quét trên trang đã bị đá.
        if self.session_lost:
            self._try_recover_session()
            if self.session_lost:
                # Chưa phục hồi được (hoặc đang chạy nền): giữ nhịp chờ, không quét.
                self.cycle_retry_at = now + CYCLE_RETRY_SECONDS
            return
        if self.chrome_busy or self.sweep_busy or self.open_table_busy or not self._cdp_available():
            self.cycle_retry_at = now + CYCLE_RETRY_SECONDS
            return
        self.sweep_note_text = "Đang tự quét tìm bàn đạt chuẩn (lượt %d)…" % (self.cycle_attempts + 1)
        self._trace("hunt_retry", dict(attempt=self.cycle_attempts + 1))
        self.sweep_lobby()

    def load_live_selected(self):
        """Chọn bàn là vào đánh luôn: không còn nút Bắt đầu/Tạm dừng hay ô xác nhận tay.

        Bàn không đạt bộ lọc thì ``sim.start`` tự trả lời lý do (qua ``plan``) và không
        chốt cược nào; đang theo dõi đúng bàn đó thì giữ nguyên khối đang đi.
        """
        if getattr(self, "_wallet_startup_sync_pending", False):
            self.sim.note = "Đang chờ đọc số dư ví để đặt lại vốn phiên trước khi theo dõi bàn."
            self.refresh()
            return
        selected = self.live_tree.selection()
        row = self.tables.get(selected[0]) if selected else None
        if not row:
            self.sim.note = "Chọn một dòng bàn trong danh sách trước."
        elif self.sim.running and row["key"] == self.sim.table_key:
            self.sim.note = "Đang theo dõi bàn này."
        else:
            self.sim.bind(row)
            self.sim.start(row, verified=True)
        self._save_state()
        self.refresh()
