# -*- coding: utf-8 -*-
"""Chiến thuật LONG-LONG (hai bát súc xắc: Vàng / Đỏ).

BÀN CHƠI
    Hai bát, mỗi bát ra một mặt: VÀNG (V) hoặc ĐỎ (D). Một tay cho ra một BỘ:
        VV = vàng + vàng      -> ô "2 vàng"      trả 2.90 : 1
        DD = đỏ + đỏ          -> ô "2 đỏ"        trả 2.90 : 1
        VD hoặc DV            -> ô "1 vàng 1 đỏ" trả 0.95 : 1
    Thứ tự vàng/đỏ trong bộ không quan trọng: VD và DV cùng ăn chung một ô.

NGUYÊN TẮC ĐÁNH (chỉ đánh ô "1 vàng 1 đỏ")
    1. Nhìn tay NGAY TRƯỚC:
         - Ra VV hoặc DD  -> BỎ, không đánh, chờ tiếp.
         - Ra VD hoặc DV  -> tay kế tiếp ĐẶT vào ô "1 vàng 1 đỏ".
    2. Kết quả tay đã đặt:
         - THẮNG (tay đó cũng ra VD/DV) -> đánh tiếp tay sau với VỐN 1 (bậc 1),
           thắng xong luôn quay về mức gốc.
         - THUA (tay đó ra VV/DD) -> dừng lại, CHỜ tay nào ra bộ VD/DV rồi mới
           đánh lại, và lúc đó đánh GẤP THẾP: lên bậc kế tiếp trong thang cược để
           lấy lại đúng số đã thua trước đó.
    3. Không có bộ VD/DV thì không đánh. Không đánh ô 2 vàng / 2 đỏ.

    DỪNG PHIÊN: thua ngay ở bậc CUỐI của thang (mặc định 4.920.000đ) là hết vốn — phiên
    dừng hẳn, không đánh tiếp bậc cuối lần nữa. Muốn chạy lại thì Reset phiên.

    Ví dụ (thang 40, 80, 160…):
        tay 1 VV  -> bỏ
        tay 2 VD  -> tay 3 đặt 40 vào ô 1 vàng 1 đỏ
        tay 3 DV  -> THẮNG +38 (40 × 0.95); tay 4 đặt tiếp 40
        tay 4 VV  -> THUA -40; chờ
        tay 5 DD  -> bỏ (không phải bộ vàng-đỏ)
        tay 6 VD  -> tay 7 đặt 80 (gấp thếp bù 40 đã thua)
        tay 7 VD  -> THẮNG +76; về vốn 1

Module thuần trạng thái: không phụ thuộc giao diện, trình duyệt hay Baccarat.
"""

import re

# Một tay cho ra một bộ; VD và DV là cùng một ô cược nên giữ nguyên thứ tự đã ra.
RAWS = ("VV", "DD", "VD", "DV")
MIXED = ("VD", "DV")
# Ô đánh duy nhất của chiến thuật này.
DOOR = "M"
DOOR_NAME = "1 vàng 1 đỏ"
DOOR_LABEL = "V-D / D-V"
# Tỉ lệ trả thưởng của sàn (ăn theo bậc cược, chưa gồm vốn).
PAYOUT = {DOOR: 0.95, "VV": 2.90, "DD": 2.90}
RAW_LABEL = {"VV": "2 vàng", "DD": "2 đỏ", "VD": "vàng-đỏ", "DV": "đỏ-vàng"}

# Thang gấp thếp tính theo ĐƠN VỊ CỦA SẢNH: 1 đv = 1.000đ, nên 40 đv = 40.000đ.
DEFAULT_BASE = 40
DEFAULT_STEPS = 8
# Vốn phiên mặc định: 10.000 đv = 10.000.000đ.
DEFAULT_CAPITAL = 10000
# Làm tròn bậc cuối về bội số 10 đv (= 10.000đ) để số tiền luôn chẵn, dễ bấm chip.
ROUND_STEP = 10

# Vốn 10 triệu chia 8 tay gấp đôi: 40 → 4.920 (bậc cuối bù phần lẻ), tổng đúng 10.000 đv.
DEFAULT_LADDER = [40, 80, 160, 320, 640, 1280, 2560, 4920]

LADDER_LOWER = 1
LADDER_UPPER = 20

# Sơ đồ quy đổi ô lịch sử của sàn về bộ VV/DD/VD/DV. Sàn vẽ hình nên decoder ưu tiên
# nhãn chữ; ô nào chỉ có số thì dùng bảng này. Bàn thật mã hoá khác thì chỉ sửa ở đây.
RESULT_CODES = {
    "vv": "VV", "dd": "DD", "vd": "VD", "dv": "DV",
    "2v": "VV", "2d": "DD", "1v1d": "VD", "1d1v": "DV",
    # Mã số theo thứ tự ô của sàn: 1 = 2 vàng, 2 = 2 đỏ, 3 = vàng-đỏ.
    "1": "VV", "2": "DD", "3": "VD",
}

# Từ khoá màu trên ô lịch sử của sàn (có dấu và không dấu, kèm tiếng Anh).
GOLD_WORDS = ("vàng", "vang", "gold", "yellow")


def normalize_result(value):
    """Chuẩn hoá một tay về 'VV'/'DD'/'VD'/'DV'; không đủ tin thì trả None.

    Nhận: bộ hai ký tự ('VD'), nhãn chữ của sàn ('vàng đỏ', '2 vàng', '1 vàng 1 đỏ'),
    và mã số theo ``RESULT_CODES``. Một ký tự đơn ('V' hoặc 'D') KHÔNG đủ để biết bộ
    nên bị từ chối — thà bỏ tay còn hơn đoán sai.
    """
    text = str(value or "").strip()
    if not text:
        return None
    token = re.sub(r"[\s\-_+/|]", "", text.casefold())
    if token in RESULT_CODES:
        return RESULT_CODES[token]
    if token in RAWS:
        return token
    # Nhãn chữ: phải đọc ra ĐỦ HAI MẶT của tay, không đoán từ một mặt.
    low = text.casefold()
    gold_hits = sum(low.count(word) for word in GOLD_WORDS)
    red_hits = len(re.findall(r"đỏ|\bred\b|\bdo\b", low))
    if gold_hits and red_hits:
        # Giữ thứ tự nếu đọc được ("vàng đỏ" vs "đỏ vàng"), còn không thì mặc định VD.
        gold_at = min(low.find(word) for word in GOLD_WORDS if word in low)
        red_at = min(pos for pos in (low.find("đỏ"), low.find("red"), low.find("do"))
                     if pos >= 0)
        return "VD" if gold_at <= red_at else "DV"
    digits = re.findall(r"\d+", low)
    if gold_hits and (digits or gold_hits >= 2):
        return "VV"
    if red_hits and (digits or red_hits >= 2):
        return "DD"
    return None


def split_results(value):
    """Tách dữ liệu đọc được thành danh sách bộ (mỗi tay một phần tử).

    Nhận list sẵn, hoặc chuỗi có/không có dấu phân cách: 'VV|VD', 'VV,VD', 'VV VD',
    và cả chuỗi dính 'VVVD' (đọc 2 ký tự một). Giá trị lạ bị bỏ qua.
    """
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        text = str(value or "").strip()
        if not text:
            return []
        if re.search(r"[\s,;|]", text):
            parts = [part for part in re.split(r"[\s,;|]+", text) if part]
        else:
            compact = re.sub(r"\s+", "", text).upper()
            if len(compact) % 2 == 0 and re.fullmatch(r"[VD]+", compact):
                parts = [compact[i:i + 2] for i in range(0, len(compact), 2)]
            else:
                parts = [text]
    out = []
    for part in parts:
        raw = normalize_result(part)
        if raw is not None:
            out.append(raw)
    return out


def is_mixed(raw):
    """True nếu tay ra bộ vàng-đỏ (VD hoặc DV) — tức ô 0.95 ăn."""
    return raw in MIXED


def die_of(value):
    """Một MẶT của bát: 'V' hoặc 'D'; không rõ thì None.

    Dùng khi ô lịch sử của sàn ghi từng con xúc xắc riêng (mỗi tay hai ô liền nhau)
    thay vì ghi cả bộ trong một ô. Ô nào mang dấu hiệu của một BỘ (có chữ số, hoặc có
    cả vàng lẫn đỏ, hoặc lặp hai lần một màu) thì trả None để decoder hiểu là bộ.
    """
    text = str(value or "").strip()
    if not text:
        return None
    token = re.sub(r"[\s\-_+/|]", "", text.casefold())
    if token in ("v", "d"):
        return token.upper()
    if re.search(r"\d", token):
        return None
    low = text.casefold()
    gold_hits = sum(low.count(word) for word in GOLD_WORDS)
    red_hits = len(re.findall(r"đỏ|\bred\b|\bdo\b", low))
    if gold_hits + red_hits == 1:
        return "V" if gold_hits == 1 else "D"
    return None


def raw_label(raw):
    """Nhãn đọc được của một tay: VV -> '2 vàng', VD -> 'vàng-đỏ'."""
    return RAW_LABEL.get(raw, str(raw or "?"))


def multiply_ladder(base, steps=DEFAULT_STEPS):
    """Sinh thang gấp thếp ×2 từ mức gốc theo đơn vị sảnh (40 → 40, 80, 160, ...)."""
    try:
        value = float(str(base).replace(".", "").replace(",", "").strip())
    except (TypeError, ValueError):
        value = float(DEFAULT_BASE)
    if not value > 0:
        value = float(DEFAULT_BASE)
    steps = max(1, min(int(steps), LADDER_UPPER))
    return [int(value * (2 ** i)) for i in range(steps)]


def ladder_for_capital(capital, base=DEFAULT_BASE, steps=DEFAULT_STEPS, round_to=ROUND_STEP):
    """Chia vốn thành thang gấp đôi: các bậc ×2, bậc cuối nhận phần còn lại.

    Ví dụ vốn 10.000 đv, gốc 40, 8 bậc -> 40, 80, 160, 320, 640, 1.280, 2.560, 4.920
    (tổng đúng 10.000 đv = 10.000.000đ). Bậc cuối được làm tròn XUỐNG bội số
    ``round_to`` để số tiền chẵn; nếu vốn không đủ cho đủ số bậc thì cắt bớt bậc cuối.
    """
    steps = max(1, min(int(steps or DEFAULT_STEPS), LADDER_UPPER))
    doubling = multiply_ladder(base, steps)
    try:
        target = int(capital)
    except (TypeError, ValueError):
        target = 0
    if target <= 0:
        return doubling
    if steps == 1:
        return [max(1, target)]
    # Vốn không đủ cho bậc đầu: giữ đúng một bậc bằng vốn.
    if doubling[0] > target:
        return [target]
    body = [stake for stake in doubling[:-1]]
    while len(body) > 1 and sum(body) + body[-1] > target:
        body.pop()
    rest = target - sum(body)
    step = max(1, int(round_to or 1))
    last = (rest // step) * step
    if last < body[-1]:
        # Phần còn lại nhỏ hơn bậc trước: bỏ bớt một bậc để bậc cuối vẫn lớn dần.
        if len(body) > 1:
            body.pop()
            rest = target - sum(body)
            last = (rest // step) * step
        if last < body[-1]:
            # Vốn quá nhỏ cho thang gấp đôi: giữ các bậc đã vừa vốn, không thêm bậc lẻ.
            return body
    if last <= 0:
        return body
    return body + [last]


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


def ladder_total(ladder):
    """Tổng vốn mà thang cược cần (đơn vị sảnh)."""
    return sum(int(stake) for stake in normalize_ladder(ladder))


def payout_of(door=DOOR):
    """Tỉ lệ ăn của ô đang đánh (0.95 cho ô 1 vàng 1 đỏ)."""
    return PAYOUT.get(door, PAYOUT[DOOR])


def summarize_history(entries, recent=40):
    """Thống kê hiệu quả của phiên từ sổ tay đã cược (``LongLongSimulation.log``).

    Chỉ tính các tay THẬT SỰ có tiền trên bàn (mỗi phần tử có ``won``/``delta``), nên
    tỉ lệ thắng/thua phản ánh đúng hiệu quả đặt cược, không phải số tay đã xem.

    Trả dict cho chart ở UI:
        hands, wins, losses, win_rate, loss_rate
        max_loss_streak   — chuỗi thua dài nhất (số tay thua liên tiếp)
        current_loss_streak — chuỗi thua đang chạy ở cuối sổ
        max_win_streak
        worst_loss        — tay lỗ nặng nhất (đơn vị sảnh, số âm)
        net_units         — tổng lãi/lỗ của phiên (đơn vị sảnh)
        streak_histogram  — {"1": số lần, "2": số lần, ...} độ dài chuỗi thua
        recent            — "W"/"L" của ``recent`` tay gần nhất (cũ -> mới)
        curve             — lãi/lỗ luỹ kế theo từng tay đã cược
    """
    flags = [bool(entry.get("won")) for entry in (entries or [])]
    deltas = [float(entry.get("delta") or 0.0) for entry in (entries or [])]
    hands = len(flags)
    wins = sum(1 for flag in flags if flag)
    losses = hands - wins

    max_loss = current_loss = 0
    max_win = current_win = 0
    histogram = {}
    run = 0
    for flag in flags:
        if flag:
            current_win += 1
            current_loss = 0
            max_win = max(max_win, current_win)
            if run:                      # kết thúc một chuỗi thua -> đếm 1 lần
                histogram[run] = histogram.get(run, 0) + 1
                run = 0
        else:
            run += 1
            current_loss = run
            current_win = 0
            max_loss = max(max_loss, run)
    if run:                              # chuỗi thua đang chạy ở cuối sổ
        histogram[run] = histogram.get(run, 0) + 1

    curve = []
    running = 0.0
    for delta in deltas:
        running += delta
        curve.append(round(running, 2))

    return {
        "hands": hands,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / hands * 100, 1) if hands else 0.0,
        "loss_rate": round(losses / hands * 100, 1) if hands else 0.0,
        "max_loss_streak": max_loss,
        "current_loss_streak": current_loss,
        "max_win_streak": max_win,
        "worst_loss": round(min(deltas), 2) if deltas else 0.0,
        "net_units": round(sum(deltas), 2),
        "streak_histogram": {str(k): v for k, v in sorted(histogram.items())},
        "recent": ["W" if flag else "L" for flag in flags[-recent:]],
        "curve": curve[-120:],
    }


class LongLongStrategy:
    """Máy trạng thái: chờ bộ VD/DV -> đặt ô 1 vàng 1 đỏ -> thắng về vốn 1 / thua lên bậc."""

    def __init__(self, ladder=None):
        self.ladder = normalize_ladder(ladder)
        self.results = []          # danh sách bộ đã thấy: 'VV'/'DD'/'VD'/'DV'
        self.step = 0              # bậc hiện tại trong thang (0 = vốn 1)
        self.pending = None        # lệnh đang chờ kết quả: {"side","stake","step"}
        self.log = []
        self.stopped = False       # True khi thua ở bậc cuối (chạm đáy thang)
        self.note = "Chờ tay ra bộ vàng-đỏ (VD/DV) để đặt ô 1 vàng 1 đỏ."

    @property
    def last(self):
        return self.results[-1] if self.results else None

    @property
    def bottom(self):
        """Bậc cuối của thang — chạm đây mà thua là dừng phiên."""
        return len(self.ladder) - 1

    def _triggered(self):
        """Điều kiện vào tiền: tay NGAY TRƯỚC ra bộ vàng-đỏ (VD hoặc DV).

        Tay thua không bao giờ là bộ vàng-đỏ nên tự động không kích hoạt tay kế tiếp;
        tay thắng thì đúng là bộ vàng-đỏ nên được đánh tiếp ngay ở vốn 1.
        """
        return is_mixed(self.last)

    def plan(self):
        """Hành động cho tay kế tiếp dựa trên kết quả đã thấy."""
        if self.stopped:
            return dict(action="stop", side=None, stake=0,
                        reason="Phiên đã dừng vì thua ở bậc cuối thang; bấm Reset phiên để chạy lại.")
        if self.pending is not None:
            return dict(action="wait", side=None, stake=0,
                        reason="Đang chờ kết quả tay đã đặt ô %s." % DOOR_NAME)
        if not self._triggered():
            if self.last is None:
                return dict(action="wait", side=None, stake=0,
                            reason="Chưa có tay nào; chờ tay ra bộ vàng-đỏ (VD/DV).")
            return dict(action="wait", side=None, stake=0,
                        reason="Tay trước ra %s — không phải bộ vàng-đỏ; tiếp tục chờ."
                        % raw_label(self.last))
        stake = self.ladder[min(self.step, self.bottom)]
        return dict(action="bet", side=DOOR, stake=stake, step=self.step,
                    reason="Tay trước ra bộ vàng-đỏ; đặt ô %s ở bậc %d." % (
                        DOOR_NAME, self.step + 1))

    def arm(self):
        """Chốt lệnh cược cho tay kế tiếp; trả về lệnh hoặc None nếu chưa đủ điều kiện."""
        decision = self.plan()
        if decision["action"] != "bet":
            self.note = decision["reason"]
            return None
        self.pending = {"side": decision["side"], "stake": decision["stake"],
                        "step": decision["step"]}
        self.note = "Đã chốt: %s vào ô %s (bậc %d)." % (
            "{:,}".format(decision["stake"]).replace(",", "."), DOOR_NAME,
            decision["step"] + 1)
        return dict(self.pending)

    def settle(self, result):
        """Nhận kết quả một tay. Trả về (bộ, tiền thắng/thua theo đơn vị sảnh).

        Có lệnh đang chờ: tất toán trước (thắng ăn stake × 0.95, thua mất stake), rồi
        mới ghi bộ vào lịch sử để quyết định tay kế tiếp. Không có lệnh: chỉ ghi lịch sử.
        """
        raw = normalize_result(result)
        if raw is None:
            return None, 0
        if self.pending is None:
            self.results.append(raw)
            self.note = "Tay ra %s; %s" % (
                raw_label(raw),
                "được vào tiền tay sau." if is_mixed(raw) else "chờ bộ vàng-đỏ.")
            return None, 0

        bet = self.pending
        self.pending = None
        won = is_mixed(raw)
        stake = bet["stake"]
        rate = payout_of(bet["side"])
        delta = stake * rate if won else -stake
        hit_bottom = False
        if won:
            self.step = 0
        elif bet["step"] >= self.bottom:
            # Thua ngay ở bậc CUỐI của thang: đã cháy hết vốn -> dừng phiên.
            self.step = self.bottom
            self.stopped = True
            hit_bottom = True
        else:
            self.step = bet["step"] + 1
        self.results.append(raw)
        entry = {"result": raw, "bet": bet["side"], "stake": stake, "payout": rate,
                 "won": won, "delta": delta, "step": bet["step"]}
        self.log.append(entry)
        amount = "{:,}".format(abs(int(round(delta)))).replace(",", ".")
        if won:
            self.note = "THẮNG %s (+%s đv, ăn %.2f) · đánh tiếp vốn 1." % (
                raw_label(raw), amount, rate)
        elif hit_bottom:
            self.note = "THUA %s (-%s đv) ở bậc cuối · ĐÃ DỪNG PHIÊN (chạm đáy thang)." % (
                raw_label(raw), amount)
        else:
            self.note = "THUA %s (-%s đv) · chờ bộ vàng-đỏ để gấp thếp bậc %d." % (
                raw_label(raw), amount, self.step + 1)
        return raw, delta

    def reset(self):
        self.pending = None
        self.step = 0
        self.stopped = False
        self.note = "Đã reset phiên; chờ tay ra bộ vàng-đỏ (VD/DV)."


class LongLongSimulation:
    """Vòng đời live cho LONG-LONG: nạp dần kết quả đọc từ trang và giữ sổ vốn riêng.

    Tách khỏi ``LiveSimulation``/``MoneyBook`` của Baccarat: mỗi lần đọc chỉ xử lý các
    tay MỚI so với lần trước nên lịch sử dài không bị tính lại. Tiền tính theo đơn vị
    sảnh (40 đv = 40.000đ).
    """

    def __init__(self, ladder=None, capital=0.0):
        self.strategy = LongLongStrategy(ladder)
        self.seen = []                 # các bộ đã xử lý (thứ tự cũ -> mới)
        self.start_capital = float(capital or 0.0)
        self.balance = float(capital or 0.0)
        self.log = []
        self.note = "Chưa nạp kết quả."

    def set_ladder(self, ladder):
        self.strategy.ladder = normalize_ladder(ladder)
        self.strategy.step = min(self.strategy.step, len(self.strategy.ladder) - 1)

    def ingest(self, results, auto_arm=True):
        """Nạp các tay mới; trả về số tay mới đã xử lý.

        ``results`` là list bộ hoặc chuỗi đọc từ trang ('VV|VD|DD'...). ``auto_arm=True``
        nghĩa là trước mỗi tay mới, nếu đủ điều kiện thì chốt lệnh như app đã đặt thật —
        dùng để tính sổ từ lịch sử. Đặt cược thật trên bàn vẫn là bước riêng.
        """
        fresh_list = split_results(results)
        if not fresh_list:
            self.note = "Không có kết quả mới."
            return 0
        if self.seen and fresh_list[:len(self.seen)] == self.seen:
            fresh = fresh_list[len(self.seen):]
        else:
            # Lịch sử bị cắt đầu hoặc đổi shoe: dựng lại từ đầu để không lệch thang.
            self.seen = []
            self.strategy.results = []
            self.strategy.pending = None
            fresh = fresh_list
        added = 0
        for raw in fresh:
            if auto_arm:
                self.strategy.arm()
            before = len(self.strategy.log)
            self.strategy.settle(raw)
            if len(self.strategy.log) > before:
                entry = dict(self.strategy.log[-1])
                entry["index"] = len(self.log) + 1
                self.log.append(entry)
                self.balance += entry["delta"] * 1000.0
            added += 1
        self.seen = fresh_list
        self.note = self.strategy.note
        return added

    def append(self, result, auto_arm=False):
        """Ghi MỘT tay do người dùng (hoặc vòng đọc realtime) đưa vào sổ.

        Trả về (bộ đã ghi, tiền thắng/thua của tay đó). Tiền bằng 0 khi tay trước không
        có lệnh cược nào — vẫn trả về bộ để bên gọi biết tay đã được ghi.
        """
        parsed = normalize_result(result)
        if parsed is None:
            self.note = "Kết quả không đọc được: %r" % (result,)
            return None, 0
        if auto_arm:
            self.strategy.arm()
        before = len(self.strategy.log)
        _, delta = self.strategy.settle(parsed)
        if len(self.strategy.log) > before:
            entry = dict(self.strategy.log[-1])
            entry["index"] = len(self.log) + 1
            self.log.append(entry)
            self.balance += entry["delta"] * 1000.0
        self.seen.append(parsed)
        self.note = self.strategy.note
        return parsed, delta

    @property
    def profit(self):
        return self.balance - self.start_capital

    def reset(self, capital=None):
        if capital is not None:
            self.start_capital = self.balance = float(capital)
        self.seen = []
        self.log = []
        # Giữ nguyên đối tượng strategy: View/Controller đang giữ tham chiếu tới nó,
        # thay bằng object mới sẽ khiến UI hiển thị trạng thái cũ (đã dừng phiên).
        self.strategy.reset()
        # Xoá cả lịch sử đã thấy: lần đọc sau sẽ nạp lại từ lịch sử trên trang và tính
        # sổ mới, không cộng trùng những tay của phiên trước.
        self.strategy.results = []
        self.note = "Đã reset phiên LONG-LONG."
