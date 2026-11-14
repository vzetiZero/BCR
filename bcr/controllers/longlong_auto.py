# -*- coding: utf-8 -*-
"""Tầng Controller: máy trạng thái chốt kết quả ván LONG-LONG (chống đếm trùng).

Sàn báo "ô thắng" bằng một class hiệu ứng gắn thêm trên ô đó, tồn tại vài giây rồi mất.
Vì vậy mỗi ván phải chốt ĐÚNG MỘT LẦN, dù vòng đọc thấy cùng một ô thắng rất nhiều lần.

Quy tắc (đã đối chiếu nhịp ván thật ~30–45 giây/ván):
    - Ô thắng KHÁC ô thắng lần trước  -> ván mới, chốt ngay.
    - Ô thắng GIỐNG lần trước         -> chỉ chốt nếu đã qua ``min_gap`` giây, vì lúc đó
      chắc chắn là ván mới (class hiệu ứng của ván trước đã tắt từ lâu).
    - Không có ô thắng               -> không chốt gì.

Phần này thuần trạng thái (không tkinter, không CDP) nên kiểm thử được.
"""

DOOR_RAW = {"JNS_B": "VV", "JNS_A": "DD", "JNS_C": "VD"}


class HandTracker:
    """Chốt một bộ kết quả cho mỗi ván, bỏ qua các lần đọc lặp của cùng một ván."""

    def __init__(self, min_gap=15.0):
        self.min_gap = float(min_gap)
        self.last_winner = None
        self.last_fire_at = 0.0
        self.hands = 0

    def reset(self):
        self.last_winner = None
        self.last_fire_at = 0.0
        self.hands = 0

    def feed(self, winner, winner_raw, now):
        """Nhận tín hiệu ô thắng; trả về bộ ('VV'/'DD'/'VD') khi ván mới chốt, else None."""
        if winner not in DOOR_RAW:
            return None
        raw = winner_raw if winner_raw in ("VV", "DD", "VD", "DV") else DOOR_RAW[winner]
        same_as_before = winner == self.last_winner
        if same_as_before and (now - self.last_fire_at) < self.min_gap:
            return None            # vẫn là ván vừa chốt, chưa sang ván mới
        self.last_winner = winner
        self.last_fire_at = now
        self.hands += 1
        return raw
