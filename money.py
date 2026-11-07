# -*- coding: utf-8 -*-
"""Theo dõi vốn, cược và các ngưỡng dừng của một phiên chơi."""

import re

FLAT, PERCENT, MARTINGALE = "flat", "percent", "martingale"

BET_MODE_NAME = {
    FLAT: "Cược đều (1 mức)",
    PERCENT: "Cược theo % vốn còn lại",
    MARTINGALE: "Gấp thếp x2 khi thua (rủi ro cao)",
}

BET_MODE_SHORT = {FLAT: "cược đều", PERCENT: "theo % vốn", MARTINGALE: "gấp thếp"}


def fmt_money(value):
    """Định dạng kiểu Việt Nam: 1.500.000 (bỏ phần thập phân nếu là số nguyên)."""
    return "{:,.0f}".format(value).replace(",", ".")


def fmt_amount(value):
    """Số tiền đúng như hiển thị trên sàn: 3.014,74 hoặc 100.

    Giữ tối đa 2 chữ số thập phân và chỉ bỏ khi là số nguyên, để UI không làm tròn
    lệch so với con số người dùng thấy trên web.
    """
    value = float(value or 0)
    if value == int(value):
        return "{:,.0f}".format(value).replace(",", ".")
    return "{:,.2f}".format(value).replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_input_money(value):
    """Nhóm hàng nghìn bằng dấu phẩy cho ô nhập: 2,000,000 để nhìn rõ mốc triệu."""
    return "{:,.0f}".format(value)


def parse_money(text):
    """Đọc ô nhập tiền: 2,000,000 · 2.000.000 · 2000000 đều cho ra 2000000."""
    digits = re.sub(r"[.,]", "", str(text).strip())
    if not re.fullmatch(r"[-+]?\d+", digits):
        raise ValueError(str(text))
    return float(digits)


class MoneyBook:
    def __init__(self, capital=3000.0, unit=100.0, mode=FLAT, percent=5.0,
                 take_profit_pct=25.0, mtg_cap=5):
        self.capital = max(0, float(capital))
        self.start_capital = self.capital
        self.balance = self.capital
        self.unit = max(1, float(unit))
        self.mode = mode
        self.percent = max(0.1, float(percent))
        self.take_profit_pct = max(1.0, float(take_profit_pct))
        self.mtg_cap = max(1, int(mtg_cap))
        self.hands_played = 0
        self.wins = 0
        self.losses = 0
        self.pushes = 0
        self.ties_bet = 0
        self.total_staked = 0.0
        self.total_returned = 0.0
        self.peak = self.capital
        self.max_drawdown = 0.0
        self.loss_streak = 0
        self.consecutive_alerts = 0
        self.blocked = None

    def next_stake(self, bet_side, suggested_side=None):
        """Số tiền nên đặt cho tay tới; 0 nếu không cược hoặc vốn không còn đủ."""
        if bet_side is None:
            return 0.0
        if self.blocked:
            return 0.0
        if self.mode == PERCENT:
            amount = self.balance * self.percent / 100.0
        elif self.mode == MARTINGALE:
            amount = self.unit * (2 ** min(self.loss_streak, self.mtg_cap))
        else:
            amount = self.unit
        return max(0.0, min(amount, self.balance))

    def settle(self, bet_side, result, stake, payout_table):
        """Cân đối một tay đã cược. Trả về {delta, outcome}."""
        self.hands_played += 1
        self.total_staked += stake
        if stake <= 0:
            return {"delta": 0.0, "outcome": "skip"}

        if result == "T":
            if bet_side == "T":
                delta = stake * payout_table["T"]
                self.wins += 1
                self.ties_bet += 1
                self.loss_streak = 0
                outcome = "win"
            else:
                delta = 0.0
                self.pushes += 1
                outcome = "push"
        elif bet_side == result:
            delta = stake * payout_table[bet_side]
            self.wins += 1
            self.loss_streak = 0
            outcome = "win"
        else:
            delta = -stake
            self.losses += 1
            self.loss_streak += 1
            outcome = "loss"

        self.balance += delta
        self.total_returned += max(0.0, stake + delta)
        self.peak = max(self.peak, self.balance)
        self.max_drawdown = max(self.max_drawdown, self.peak - self.balance)
        self.check_limits()
        return {"delta": delta, "outcome": outcome}

    @property
    def profit(self):
        return self.balance - self.start_capital

    def sync_capital(self, balance):
        """Đồng bộ vốn theo số dư THẬT của tài khoản (đã quy đổi ra VNĐ).

        Chỉ đổi ``start_capital`` khi phiên chưa có tay nào, để không làm sai lịch sử
        lãi/lỗ đã ghi. Trả ``(ok, note)``.
        """
        try:
            value = float(balance)
        except (TypeError, ValueError):
            return False, "Số dư không hợp lệ."
        if value <= 0 or value != value:
            return False, "Số dư phải lớn hơn 0."
        if self.hands_played:
            return False, ("Phiên đã có %d tay; không đổi vốn giữa phiên để giữ đúng lãi/lỗ. "
                           "Tạo phiên mới nếu muốn dùng số dư thật." % self.hands_played)
        self.capital = value
        self.start_capital = value
        self.balance = value
        self.peak = value
        self.max_drawdown = 0.0
        self.check_limits()
        return True, "Đã đồng bộ vốn thật: %s đ." % fmt_money(value)

    def rebase_to_balance(self):
        """Chốt lời xong một bàn: vốn ban đầu của bàn kế tiếp = số dư hiện tại.

        Nhờ vậy mục tiêu chốt lời (% vốn) của bàn sau tính trên số tiền mới nhất mà
        người dùng không phải sửa ô "Vốn ban đầu" bằng tay. Không reset thống kê tay
        (sổ vẫn cumulative cả phiên); chỉ dời mốc vốn và mở lại ngưỡng dừng.
        Trả về số dư đã dùng làm vốn mới.
        """
        self.capital = self.balance
        self.start_capital = self.balance
        self.check_limits()
        return self.balance

    def check_limits(self):
        won_pct = (self.balance - self.start_capital) / self.start_capital * 100 if self.start_capital else 0
        if won_pct >= self.take_profit_pct:
            self.blocked = ("TAKE_PROFIT", "Lãi %.0f%% vốn (%s). NGỪNG — đã chốt lời." % (won_pct, fmt_money(self.profit)))
        elif self.balance < self.unit:
            self.blocked = ("NO_CAPITAL", "Vốn còn lại thấp hơn 1 đơn vị cược. Không còn gì để đánh.")
        else:
            self.blocked = None
        return self.blocked

    def summary(self):
        return {
            "capital": self.start_capital,
            "balance": self.balance,
            "profit": self.profit,
            "hands": self.hands_played,
            "wins": self.wins,
            "losses": self.losses,
            "pushes": self.pushes,
            "win_rate": (self.wins / self.hands_played * 100) if self.hands_played else 0.0,
            "staked": self.total_staked,
            "drawdown": self.max_drawdown,
            "alert": self.blocked,
        }
