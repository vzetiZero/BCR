# -*- coding: utf-8 -*-
"""Tầng Controller: đặt chip thật, chờ ván mới và xác nhận đã đặt.

Là nơi duy nhất bấm cược; mọi tính toán số tiền lấy từ Model ``method``/``money``.
"""
import threading
import time


from bcr.adapters import table_bets as TB
from bcr.config import *
from bcr.models import engine as E
from bcr.models import money as M
from bcr.models.live_sim import table_is_clear


class BettingMixin:
    """Đặt chip theo kế hoạch, chờ ván mới và xác nhận đã vào tiền."""

    def place_armed_bet(self, plan):
        """Đặt số tiền của kế hoạch vào ô cược bằng chuột; mệnh giá chip do nơi bấm chốt.

        Gọi từ hook ``self.sim.on_arm`` mỗi khi kế hoạch được chốt. Chỉ chạy khi bật
        “Tự đặt chip” và kế hoạch có cửa + số tiền; chạy ở thread nền, không chặn UI.

        Khi tự đặt đang bật, MỌI nhánh thoát đều phải báo về ``sim.mark_placed``: có tay
        chỉ được tính khi chip thật đã đặt thành công. Nhánh nào im lặng thoát là nhánh
        đó để tay chưa đặt bị đếm như đã đặt (đúng lỗi vào bàn chậm ở tay đầu tiên).
        """
        if not self.auto_place_var.get():
            # The live cycle may only advance after the browser confirms a real bet.
            # Previously this returned with no placement state, so process() treated
            # the next result like a simulated successful wager.
            self.sim.mark_placing(plan)
            self.sim.mark_placed(False, "Chưa đặt: hãy bật ‘Tự đặt chip’ để chỉ tính tay đã được web xác nhận.", plan)
            self._set_place_status(False, plan.get("place_note", "Chưa đặt được chip."))
            return
        side, stake = plan.get("side"), plan.get("stake") or 0
        if not side or stake <= 0:
            self.sim.mark_placing(plan)
            self.sim.mark_placed(False, "Chưa đặt: kế hoạch cược không hợp lệ.", plan)
            return
        self.sim.mark_placing(plan)
        if self.exit_requested:
            self._finish_place(plan, False, "Đang đóng app, huỷ tự đặt chip.")
            return
        if self.place_busy:
            self._finish_place(plan, False, "Lượt bấm chip trước chưa xong nên tay này chưa được bấm.")
            return
        # Số in trên chip chính là số tiền VN2 của sàn (chip 20 = 20), nên không nhân
        # hệ số nào: khay 20/40/50 ghép thẳng mức cược 20. Nhân thêm là bỏ tay oan.
        unit = TB.DEFAULT_CHIP_UNIT
        # KHÔNG chốt mệnh giá ở đây. chip_live là bản đọc từ trước/trong lúc bàn render
        # khay, quyết định theo nó là bỏ tay oan — đúng lỗi "bàn đã mở cửa sổ chọn chip
        # lên nhưng lại không chọn được". place_bet đọc lại khay thật ngay chỗ bấm (chờ
        # tới 20s) và chốt mệnh giá theo đúng những gì khay đang có.
        self.place_busy = True
        self.place_status.config(text="Đang tự đặt %s VN2 vào %s…" % (
            M.fmt_money(stake), E.SIDE_NAME[side]), fg=ACCENT)
        self._trace("place_start", dict(table=self.sim.table_key, side=side, stake=stake,
                                        step=plan.get("ladder_step")))

        def work():
            try:
                ok_gate, gate_note = self._wait_for_next_hand(unit)
                self._trace("place_gate", dict(ok=ok_gate, note=gate_note))
                if not ok_gate:
                    self.events.put(("placed", (False, gate_note, plan)))
                    return
                # Kết quả tay này đã về trong lúc chờ gate thì cửa sổ đặt cược đã đóng;
                # bấm nữa là chip rơi sang tay sau (đúng lỗi "tay 1 chưa đặt, tay 2 mang
                # chip 40 trong khi kế hoạch báo 120"). Bỏ lượt, không bấm liều.
                row_letters = (self.tables.get(self.sim.table_key) or {}).get("letters")
                if row_letters and row_letters != plan.get("baseline"):
                    self.events.put(("placed", (False, "Kết quả tay này đã về trước khi bấm chip; "
                                                       "không bấm sang cửa sổ tay sau.", plan)))
                    return
                # Timestamp each actual bet-spot click in table_bets, not the start of DOM setup.
                # If the result arrives while setup is waiting, a later chip click is
                # attributed to the next window and cannot settle this hand.
                def record_bet_click():
                    plan["placed_at"] = time.time()

                ok, note, _placed = TB.place_bet(self.browser, side, [], unit=unit,
                                                 human_mouse=self.human_mouse_var.get(),
                                                 amount=stake, on_bet_click=record_bet_click)
                self.events.put(("placed", (ok, note, plan)))
            except Exception as exc:
                # Thread chết giữa chừng mà không báo thì kế hoạch kẹt ở "pending" và
                # việc tính tay dừng lại mãi — phải luôn trả về một kết quả.
                self.events.put(("placed", (False, "Lỗi khi tự đặt chip (%s)."
                                            % type(exc).__name__, plan)))

        threading.Thread(target=work, daemon=True).start()

    def _finish_place(self, plan, ok, note):
        """Báo kết quả lượt đặt chip ngay trên luồng UI (nhánh không cần thread nền)."""
        self.sim.mark_placed(ok, note, plan)
        self._set_place_status(ok, note)

    def _on_placed(self, ok, note, plan):
        """Nhánh "placed" của luồng poll: kết quả chỉ có ý nghĩa với kế hoạch đang chờ.

        Về muộn (plan đã bị thay/tạm dừng) thì ghi vào lịch sử cũng không sao nhưng sim
        sẽ bỏ qua — tay mới chưa bấm chip và vẫn phải bị tính là chưa đặt.
        """
        self.place_busy = False
        self.sim.mark_placed(ok, note, plan)
        self._set_place_status(ok, note)

    def _wait_for_next_hand(self, unit, timeout=BET_GATE_TIMEOUT, poll=BET_GATE_POLL):
        """Chờ bàn gom hết chip ván trước (cả 3 cửa về 0) rồi mới cho phép tự đặt.

        Evolution chưa lộ rõ đồng hồ đếm giờ đặt cược qua DOM nên dùng tín hiệu gián
        tiếp: chip trên bàn trống nghĩa là ván trước đã kết toán và bàn đang ở cửa
        sổ đặt cược mới, an toàn để bấm. Hết ``timeout`` giây mà bàn còn chip của ván
        trước thì bỏ, không bấm liều lên chip cũ. Chạy trong thread nền của
        ``place_armed_bet``, không chặn UI.
        """
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.exit_requested:
                return False, "Đang đóng app, huỷ tự đặt chip."
            live = self._read_chip_state(unit)
            if table_is_clear(live):
                return True, ""
            # Phân biệt "bàn còn chip ván trước" với "không đọc được bàn": báo nhầm hai
            # cái này khiến người dùng tưởng bàn đang bận trong khi app chưa thấy khay.
            last_error = "" if (live or {}).get("ok") else ((live or {}).get("error") or "no_read")
            time.sleep(poll)
        if last_error:
            return False, ("Không tự đặt: sau %ds vẫn không đọc được bàn (%s) nên chưa bấm chip."
                           % (int(timeout), last_error))
        return False, ("Không tự đặt: sau %ds bàn vẫn còn chip ván trước, chưa chắc "
                       "đang ở lượt đặt cược mới." % int(timeout))

    def _on_cycle_back(self, ok, note):
        """Kết quả đưa tab về danh sách bàn.

        Bàn chốt lời: rời bàn thành công thì tính là xong một bàn. Chưa đủ số bàn cấu
        hình thì đồng bộ vốn và quét bàn kế tiếp; đủ rồi thì dừng hẳn (không quét/mở).
        """
        self.sweep_note_text = note
        take_profit = bool(getattr(self.sim, "take_profit_done", False))
        self._trace("cycle_back", dict(ok=ok, take_profit=take_profit))
        if take_profit:
            if ok:
                self.cycle_waiting = False
                self.cycle_retry_at = 0.0
                self.cycle_table_key = None
                self.tables_done += 1
                self._trace("table_completed", dict(done=self.tables_done,
                                                     target=self._tables_target(),
                                                     balance=self.sim.money.balance))
                if self._session_complete():
                    self.sweep_note_text = (
                        "Đã đạt mục tiêu lãi %.0f%% vốn (%s) · đã đóng bàn và dừng phiên "
                        "(xong %d/%d bàn)." % (
                            self.sim.money.take_profit_pct, M.fmt_money(self.sim.money.profit),
                            self.tables_done, self._tables_target()))
                else:
                    self._start_next_table()
            elif self._auth_blame():
                self._session_lost_step(note)
            else:
                # Không về được sảnh: hẹn thử lại đúng việc rời bàn,
                # không chuyển sang săn bàn mới.
                self.cycle_waiting = True
                self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
                self.sweep_note_text = (
                    "%s · sẽ thử đóng bàn lại sau %d giây." % (note, CYCLE_RETRY_SECONDS))
            self.refresh()
            return
        if ok:
            self.sweep_lobby()
        elif self._auth_blame():
            self._session_lost_step(note)
        elif not self._schedule_cycle_retry(note):
            self.cycle_waiting = False
            self.refresh()

    def request_chip_read(self, force=False):
        """Đọc chip thật đang đặt trên bàn ở thread nền; không chặn UI, không bấm gì."""
        now = time.time()
        if self.chip_busy or self.exit_requested:
            return
        if not force and now - self.chip_read_at < CHIP_REFRESH:
            return
        if not self.sim.table_key:
            return
        if not self.browser.connected:
            return
        self.chip_busy = True
        self.chip_read_at = now
        unit = TB.DEFAULT_CHIP_UNIT
        def work():
            live = self._read_chip_state(unit)
            self.events.put(("chips", live))
        threading.Thread(target=work, daemon=True).start()

    def _read_chip_state(self, unit):
        """Đọc chip thật đang đặt trên bàn (đồng bộ); gọi từ thread nền.

        Dùng chung cho cả việc hiển thị trạng thái chip và cho gate "chờ ván mới"
        trước khi tự đặt cược.
        """
        try:
            return TB.read(self.browser, unit=unit)
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}
