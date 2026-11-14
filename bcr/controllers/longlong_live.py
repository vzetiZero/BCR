# -*- coding: utf-8 -*-
"""Tầng Controller: cập nhật realtime số dư + trạng thái bàn LONG-LONG lên UI.

Đọc bàn qua Adapter ``longlong_table`` trong một thread riêng (mặc định 2 giây/lần),
rồi đẩy kết quả về UI thread bằng ``root.after``. View chỉ hiển thị, không tự gọi CDP.
"""

import threading
import time

from bcr.adapters import longlong_table
from bcr.controllers import longlong_auto
from bcr.models import longlong
from bcr.models.longlong import raw_label


class LongLongLiveMixin:
    """Vòng đọc bàn LONG-LONG theo nhịp cho tab LONG-LONG."""

    LONG_LONG_INTERVAL = 2.0

    # --------------------------------------------------------------- vòng đời
    @property
    def longlong_live_running(self):
        return getattr(self, "longlong_live_stop_event", None) is not None

    def longlong_live_start(self, interval=None):
        """Bật đọc realtime (idempotent: gọi nhiều lần vẫn chỉ có một thread)."""
        if self.longlong_live_running:
            return
        self.longlong_interval = float(interval or self.LONG_LONG_INTERVAL)
        if not getattr(self, "longlong_tracker", None):
            self.longlong_tracker = longlong_auto.HandTracker()
        self.longlong_pending_place = None
        self.longlong_place_attempts = 0
        self.longlong_place_busy = False
        self.longlong_arm_hand = None
        self.longlong_live_stop_event = threading.Event()
        if not getattr(self, "longlong_table", None):
            self.longlong_table = longlong_table.LongLongTable(self.browser)
        thread = threading.Thread(target=self._longlong_live_loop, daemon=True,
                                  name="longlong-live")
        self.longlong_live_thread = thread
        thread.start()
        if hasattr(self, "_longlong_render_live"):
            self._longlong_render_live()

    def longlong_live_stop(self):
        event = getattr(self, "longlong_live_stop_event", None)
        if event is not None:
            event.set()
            self.longlong_live_stop_event = None
        if hasattr(self, "_longlong_render_live"):
            self._longlong_render_live()

    def longlong_live_toggle(self):
        if self.longlong_live_running:
            self.longlong_live_stop()
        else:
            self.longlong_live_start()
        self._longlong_render_live()

    def longlong_read_now(self):
        """Đọc ngay một lần (không cần bật vòng realtime)."""
        if not getattr(self, "longlong_table", None):
            self.longlong_table = longlong_table.LongLongTable(self.browser)

        def work():
            state = self.longlong_table.read()
            self.root.after(0, lambda s=state: self._longlong_apply_live(s))

        threading.Thread(target=work, daemon=True, name="longlong-read").start()

    def longlong_live_autostart(self):
        """Tự bật khi đang ở tab LONG-LONG và Chrome đã kết nối; tự tắt khi rời đi."""
        wanted = (getattr(self, "game_mode", "") == "longlong"
                  and bool(getattr(self.browser, "connected", False))
                  and not self.closing)
        if wanted and not self.longlong_live_running:
            self.longlong_live_start()
        elif not wanted and self.longlong_live_running:
            self.longlong_live_stop()

    # ------------------------------------------------------------------ thread
    def _longlong_live_loop(self):
        event = getattr(self, "longlong_live_stop_event", None)
        while event is not None and not event.is_set():
            if getattr(self, "closing", False) or getattr(self, "exit_requested", False):
                break
            state = self.longlong_table.read()
            state["read_at"] = state.get("read_at") or time.time()
            try:
                self.root.after(0, lambda s=dict(state): self._longlong_apply_live(s))
            except Exception:
                break
            event.wait(getattr(self, "longlong_interval", self.LONG_LONG_INTERVAL))

    def _longlong_apply_live(self, state):
        """Nhận trạng thái bàn (UI thread): chốt ván, đặt chip, rồi nhờ View vẽ lại."""
        state = dict(state or {})
        state["status_note"] = longlong_table.read_state_text(state)
        state["read_at"] = state.get("read_at") or time.time()
        self.longlong_live_state = state
        if not hasattr(self, "longlong_tracker") or self.longlong_tracker is None:
            self.longlong_tracker = longlong_auto.HandTracker()
        if state.get("ok") and self.longlong_auto_result_on():
            raw = self.longlong_tracker.feed(state.get("winner"), state.get("winner_raw"),
                                             state.get("read_at"))
            if raw:
                self._longlong_on_hand(raw)
        if state.get("ok"):
            self._longlong_try_place(state)
        if hasattr(self, "_longlong_render_live"):
            self._longlong_render_live()

    # ------------------------------------------------------- công tắc trên View
    def longlong_auto_result_on(self):
        var = getattr(self, "longlong_auto_var", None)
        return bool(var.get()) if var is not None else True

    def longlong_place_real_on(self):
        var = getattr(self, "longlong_place_var", None)
        return bool(var.get()) if var is not None else False

    def _longlong_log(self, text):
        """Ghi một dòng vào nhật ký tự động ngắn trên panel."""
        if not hasattr(self, "longlong_auto_lines"):
            self.longlong_auto_lines = []
        self.longlong_auto_lines.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
        del self.longlong_auto_lines[:-6]
        label = getattr(self, "longlong_auto_log", None)
        if label is not None:
            label.config(text="\n".join(self.longlong_auto_lines))

    # ------------------------------------------------------------ chốt một ván
    def _longlong_on_hand(self, raw):
        """Một ván vừa có kết quả: tất toán ván đã đặt (nếu có) và tính kế hoạch mới."""
        sim = self._longlong_simulation()
        strategy = sim.strategy
        had_pending = strategy.pending is not None
        parsed, delta = sim.append(raw)
        if parsed is None:
            return
        self.longlong_place_attempts = 0
        self.longlong_arm_hand = None
        if had_pending:
            self._longlong_log("Ván %d: ra %s · %s (%s%d đv)" % (
                self.longlong_tracker.hands, raw_label(raw),
                "THẮNG" if delta > 0 else "THUA",
                "+" if delta >= 0 else "", int(round(abs(delta)))))
        else:
            self._longlong_log("Ván %d: ra %s · chưa cược ván này" % (
                self.longlong_tracker.hands, raw_label(raw)))
        plan = strategy.plan()
        self.longlong_pending_place = plan if plan["action"] == "bet" else None
        if plan["action"] == "bet":
            self._longlong_log("Kế hoạch: đặt %s đv vào ô %s." % (
                "{:,}".format(plan["stake"]).replace(",", "."), longlong.DOOR_NAME))
        elif plan["action"] == "stop":
            self._longlong_log("Phiên đã dừng: chạm đáy thang.")
        if not self.longlong_place_real_on() and plan["action"] == "bet":
            # Chế độ mô phỏng: chốt lệnh trên giấy để sổ vốn vẫn phản ánh chiến thuật.
            strategy.arm()
            self.longlong_pending_place = None

    # ------------------------------------------------------------- đặt chip thật
    def _longlong_try_place(self, state):
        """Đặt chip cho kế hoạch đang chờ, chỉ khi đã bật "đặt chip thật"."""
        plan = getattr(self, "longlong_pending_place", None)
        if not plan or plan.get("action") != "bet":
            return
        if not self.longlong_place_real_on():
            return
        if getattr(self, "longlong_place_busy", False):
            return
        stake = plan["stake"]
        balance = state.get("balance_vn2")
        if balance is not None and balance + 0.001 < float(stake):
            self._longlong_log("Không đủ số dư (%s < %s VN2): bỏ ván này." % (
                "{:,.2f}".format(balance), "{:,.2f}".format(stake)))
            self.longlong_pending_place = None
            return
        attempts = getattr(self, "longlong_place_attempts", 0)
        if attempts >= 3:
            self._longlong_log("Đặt chip hỏng 3 lần: bỏ ván, chờ ván sau.")
            self.longlong_pending_place = None
            return
        self.longlong_place_attempts = attempts + 1
        self.longlong_place_busy = True
        if not getattr(self, "longlong_table", None):
            self.longlong_table = longlong_table.LongLongTable(self.browser)

        def work():
            result = self.longlong_table.place(stake)
            try:
                self.root.after(0, lambda r=result, s=stake: self._longlong_after_place(r, s))
            except Exception:
                self.longlong_place_busy = False

        threading.Thread(target=work, daemon=True, name="longlong-place").start()

    def _longlong_after_place(self, result, stake):
        self.longlong_place_busy = False
        if isinstance(result, dict) and result.get("ok"):
            chips = result.get("placed") or []
            self._longlong_log("Đã đặt %s VN2 vào ô %s · chip %s · tổng của bạn %s" % (
                "{:,.2f}".format(stake), longlong.DOOR_NAME,
                " + ".join(str(c) for c in chips),
                "{:,.2f}".format(result.get("total") or 0.0)))
            # Chỉ chốt lệnh trên giấy SAU khi chip thật đã vào, để sổ vốn khớp tiền thật.
            self._longlong_simulation().strategy.arm()
            self.longlong_arm_hand = self.longlong_tracker.hands
            self.longlong_pending_place = None
            return
        error = (result or {}).get("error") or "không rõ lỗi"
        self._longlong_log("Đặt chip chưa được: %s" % error)
