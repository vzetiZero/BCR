# -*- coding: utf-8 -*-
"""Tầng Controller: máy trạng thái chốt kết quả ván LONG-LONG (chống đếm trùng/nhiễu).

Sàn báo "ô thắng" bằng một class hiệu ứng gắn thêm trên ô đó, tồn tại vài giây rồi mất.
Trong lúc chạy animation, phép so class có thể lóe sang ô khác một nhịp, và vòng đọc
2 giây có thể thấy hai "ô thắng" khác nhau trong 2 giây — nếu chốt ngay thì sinh ra
VÁN MA: sổ vốn cộng/trừ tiền cho tay chưa hề mở (đúng lỗi "tính win luôn mặc dù chưa mở").

Vì vậy một ván chỉ được chốt khi đủ CẢ BA điều kiện:
    1. cùng MỘT ô thắng được đọc ``confirm`` lần liên tiếp (lọc nhiễu animation),
    2. cách ván trước ít nhất ``min_hand_gap`` giây (ván thật không thể sát nhau),
    3. nếu trùng ô với ván trước thì phải qua ``min_gap`` giây (class của ván trước đã tắt).

Không có ô thắng -> không chốt gì. Phần này thuần trạng thái (không CDP) nên kiểm thử được.
"""

DOOR_RAW = {"JNS_B": "VV", "JNS_A": "DD", "JNS_C": "VD"}
RAWS = ("VV", "DD", "VD", "DV")

# Mặc định: 2 lần đọc liên tiếp (≈2–4 giây với nhịp 2 giây) và 2 ván cách nhau ≥ 6 giây.
DEFAULT_CONFIRM = 2
DEFAULT_MIN_HAND_GAP = 6.0


class HandTracker:
    """Chốt một bộ kết quả cho mỗi ván, bỏ qua lần đọc lặp và nhiễu hiệu ứng."""

    def __init__(self, min_gap=15.0, min_hand_gap=DEFAULT_MIN_HAND_GAP,
                 confirm=DEFAULT_CONFIRM):
        self.min_gap = max(0.0, float(min_gap))
        self.min_hand_gap = max(0.0, float(min_hand_gap))
        self.confirm = max(1, min(int(confirm), 5))
        self.last_winner = None
        self.last_fire_at = 0.0
        self.hands = 0
        self._seen = None          # ô thắng của lần đọc ngay trước
        self._streak = 0           # số lần đọc liên tiếp cùng ô

    def configure(self, min_gap=None, min_hand_gap=None, confirm=None):
        """Đổi tham số giữa phiên (giá trị lạ bị kẹp, không làm hỏng bộ đếm)."""
        if min_gap is not None:
            self.min_gap = max(0.0, float(min_gap))
        if min_hand_gap is not None:
            self.min_hand_gap = max(0.0, float(min_hand_gap))
        if confirm is not None:
            self.confirm = max(1, min(int(confirm), 5))

    def reset(self):
        self.last_winner = None
        self.last_fire_at = 0.0
        self.hands = 0
        self._seen = None
        self._streak = 0

    def feed(self, winner, winner_raw, now):
        """Nhận tín hiệu ô thắng; trả về bộ ('VV'/'DD'/'VD') khi ván mới chốt, else None."""
        now = float(now)
        if winner not in DOOR_RAW:
            self._seen = None
            self._streak = 0
            return None

        # 1. Xác nhận: cùng một ô thắng phải được đọc liên tiếp ``confirm`` lần.
        if winner == self._seen:
            self._streak += 1
        else:
            self._seen, self._streak = winner, 1
        if self._streak < self.confirm:
            return None

        # 2. Hai ván thật không thể sát nhau — chặn "ván ma" do nhiễu hiệu ứng.
        if self.last_fire_at and (now - self.last_fire_at) < self.min_hand_gap:
            return None

        # 3. Cùng ô với ván vừa chốt: chỉ tính là ván mới khi đã qua ``min_gap``.
        if winner == self.last_winner and (now - self.last_fire_at) < self.min_gap:
            return None

        raw = winner_raw if winner_raw in RAWS else DOOR_RAW[winner]
        self.last_winner = winner
        self.last_fire_at = now
        self.hands += 1
        return raw
