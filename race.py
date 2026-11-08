# -*- coding: utf-8 -*-
"""Chiến thuật Đua ngựa (3 cửa) — tách hẳn khỏi logic Baccarat.

Nguyên tắc:
- Chỉ đánh cửa 1 và chỉ khi tay NGAY TRƯỚC đó ra cửa 1.
- Không có điều kiện thì chờ, không cược.
- Thắng -> về mức cược gốc. Thua -> nhân đôi (Martingale) theo thang cấu hình.

Module thuần trạng thái, không phụ thuộc giao diện hay trình duyệt, nên có thể
kiểm thử độc lập và không giữ bất kỳ state nào của Baccarat.
"""

BET_SIDE = "1"
SIDES = ("1", "2", "3")

# Thang gấp thếp 12 bậc tính theo ĐƠN VỊ CỦA SẢNH (chip 20 = 20.000đ). Nhờ vậy
# người dùng nhập đúng con số nhìn thấy trên khay chip: 20, 40, 80, ...
DEFAULT_BASE = 20
DEFAULT_LADDER = [DEFAULT_BASE * (2 ** i) for i in range(12)]

LADDER_LOWER = 1
LADDER_UPPER = 20


def multiply_ladder(base, steps=12):
    """Sinh thang gấp thếp từ một mức gốc theo đơn vị sảnh (×2 mỗi bậc).

    Ví dụ base=20 -> 20, 40, 80, 160, ... đúng các mệnh giá chip 20/40/50.
    """
    try:
        value = float(str(base).replace(".", "").replace(",", "").strip())
    except (TypeError, ValueError):
        value = float(DEFAULT_BASE)
    if not value > 0:
        value = float(DEFAULT_BASE)
    steps = max(1, min(int(steps), LADDER_UPPER))
    return [int(value * (2 ** i)) for i in range(steps)]


def normalize_result(value):
    """Chuẩn hoá kết quả một tay về '1'/'2'/'3'; giá trị lạ trả None (bỏ qua)."""
    text = str(value or "").strip()
    return text if text in SIDES else None


def normalize_ladder(stakes=None):
    """Chuẩn hoá thang cược: mỗi bậc là số dương; sai thì về thang mặc định."""
    if not stakes:
        return list(DEFAULT_LADDER)
    clean = []
    for stake in stakes:
        try:
            value = float(str(stake).replace(".", "").replace(",", "").strip())
        except (TypeError, ValueError):
            return list(DEFAULT_LADDER)
        if not value > 0:
            return list(DEFAULT_LADDER)
        clean.append(int(value))
    return clean[:LADDER_UPPER] or list(DEFAULT_LADDER)


class RaceStrategy:
    """Máy trạng thái: chờ cửa 1 -> cược cửa 1 -> thắng reset / thua lên bậc."""

    def __init__(self, ladder=None):
        self.ladder = normalize_ladder(ladder)
        self.results = []
        self.step = 0            # bậc hiện tại trong thang (0 = mức gốc)
        self.pending = None      # lệnh cược đang chờ kết quả: {"side","stake","step"}
        self.last_was_bet = False  # tay vừa rồi có phải tay mình đã đặt cược
        self.log = []
        self.note = "Chờ tay ra cửa 1 để đặt cược cửa 1 ở tay kế tiếp."

    @property
    def last(self):
        return self.results[-1] if self.results else None

    def _triggered(self):
        """Tay vừa rồi là cửa 1 VÀ không phải tay mình đã cược.

        Một tay đã đặt cược không tự kích hoạt tay kế tiếp: sau mỗi lệnh phải chờ
        một tay quan sát mới ra cửa 1 (đúng ví dụ: tay 8 cược xong, tay 9 không cược).
        """
        return self.last == BET_SIDE and not self.last_was_bet

    def plan(self):
        """Tính hành động cho tay kế tiếp dựa trên kết quả đã thấy."""
        if self.pending is not None:
            return dict(action="wait", side=None, stake=0,
                        reason="Đang chờ kết quả tay đã đặt cửa %s." % self.pending["side"])
        if not self._triggered():
            return dict(action="wait", side=None, stake=0,
                        reason="Tay trước không phải cửa 1; tiếp tục chờ.")
        stake = self.ladder[min(self.step, len(self.ladder) - 1)]
        return dict(action="bet", side=BET_SIDE, stake=stake, step=self.step,
                    reason="Tay trước ra cửa 1; đặt cửa 1 bậc %d." % (self.step + 1))

    def arm(self):
        """Chốt lệnh cược cho tay kế tiếp; trả về lệnh hoặc None nếu chưa đủ điều kiện."""
        decision = self.plan()
        if decision["action"] != "bet":
            self.note = decision["reason"]
            return None
        self.pending = {"side": decision["side"], "stake": decision["stake"],
                        "step": decision["step"]}
        self.note = "Đã đặt %s vào cửa %s (bậc %d)." % (
            "{:,}".format(decision["stake"]).replace(",", "."), decision["side"],
            decision["step"] + 1)
        return dict(self.pending)

    def settle(self, result):
        """Nhận kết quả một tay và cập nhật thang. Trả về (ket_qua, tien_thang_thua).

        result là '1'/'2'/'3'. Nếu đang có lệnh cược thì tất toán trước, sau đó mới
        ghi kết quả vào lịch sử để quyết định tay kế tiếp.
        """
        side = normalize_result(result)
        if side is None:
            return None, 0
        if self.pending is None:
            self.results.append(side)
            self.last_was_bet = False
            self.note = ("Tay ra cửa %s; chờ cửa 1 để vào." % side)
            return None, 0

        bet = self.pending
        self.pending = None
        self.last_was_bet = True
        won = side == bet["side"]
        stake = bet["stake"]
        delta = stake if won else -stake
        if won:
            self.step = 0
        else:
            self.step = min(self.step + 1, len(self.ladder) - 1)
        self.results.append(side)
        entry = {"result": side, "bet": bet["side"], "stake": stake,
                 "won": won, "delta": delta, "step": bet["step"]}
        self.log.append(entry)
        amount = "{:,}".format(abs(delta)).replace(",", ".")
        if won:
            self.note = "Thắng cửa %s (+%s đv) · về bậc 1." % (side, amount)
        else:
            self.note = "Thua cửa %s (-%s đv) · lên bậc %d." % (side, amount, self.step + 1)
        return side, delta

    def reset(self):
        self.pending = None
        self.step = 0
        self.last_was_bet = False
        self.note = "Đã reset phiên; chờ tay ra cửa 1."


class RaceSimulation:
    """Vòng đời live cho Đua ngựa: nạp dần kết quả đọc từ trang và giữ sổ vốn riêng.

    Tách khỏi ``LiveSimulation``/``MoneyBook`` của Baccarat: mỗi lần đọc chỉ xử lý các
    tay MỚI so với lần trước, nên lịch sử dài không bị tính lại. Tiền tính theo đơn vị
    sảnh (chip 20 = 20.000đ).
    """

    def __init__(self, ladder=None, capital=0.0):
        self.strategy = RaceStrategy(ladder)
        self.seen = ""                 # chuỗi kết quả đã xử lý (theo thứ tự cũ -> mới)
        self.start_capital = float(capital or 0.0)
        self.balance = float(capital or 0.0)
        self.log = []
        self.note = "Chưa nạp kết quả."

    def set_ladder(self, ladder):
        self.strategy.ladder = normalize_ladder(ladder)
        self.strategy.step = min(self.strategy.step, len(self.strategy.ladder) - 1)

    def ingest(self, letters, auto_arm=True):
        """Nạp chuỗi kết quả mới. Chỉ tất toán các tay chưa thấy; trả về số tay mới.

        ``auto_arm=True``: trước mỗi tay mới, nếu đủ điều kiện thì chốt lệnh cược như
        app đã đặt thật; chế độ này dùng để tính sổ từ lịch sử. Đặt cược thật trên bàn
        vẫn là bước riêng (place).
        """
        text = "".join(ch for ch in str(letters or "") if ch in SIDES)
        if not text:
            self.note = "Không có kết quả mới."
            return 0
        if text.startswith(self.seen):
            fresh = text[len(self.seen):]
        else:
            self.seen = ""
            self.strategy.results = []
            self.strategy.pending = None
            self.strategy.last_was_bet = False
            fresh = text
        added = 0
        for value in fresh:
            if auto_arm:
                self.strategy.arm()
            before = len(self.strategy.log)
            self.strategy.settle(value)
            if len(self.strategy.log) > before:
                entry = dict(self.strategy.log[-1])
                entry["index"] = len(self.log) + 1
                self.log.append(entry)
                self.balance += entry["delta"] * 1000.0
            added += 1
        self.seen = text
        self.note = self.strategy.note
        return added

    @property
    def profit(self):
        return self.balance - self.start_capital

    def reset(self, capital=None):
        if capital is not None:
            self.start_capital = self.balance = float(capital)
        self.seen = ""
        self.log = []
        self.strategy = RaceStrategy(self.strategy.ladder)
        self.note = "Đã reset phiên Đua ngựa."

