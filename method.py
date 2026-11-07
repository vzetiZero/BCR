# -*- coding: utf-8 -*-
"""Các phương pháp vào cầu: lọc bàn, đánh khối bên mạnh -> bên đổi, bậc cược nhập tay theo khối.

Quy tắc của người dùng:
1. Chọn bàn: đã đi trong khoảng tay người dùng đặt (mặc định 10-50),
   cầu ngắn (không có bệt từ 6 tay trở lên), chưa từng có cầu 3-3.
2. Cầu đang mạnh bên nào -> đánh mấy tay đầu khối bên đó, rồi đổi sang bên kia mấy tay còn lại.
   Khối 3-3 đi 6 tay, khối 3-2 đi 5 tay.
3. Thắng thì quay về bậc 1. Thua thì sang bậc kế tiếp trong danh sách mức cược người dùng nhập.
4. Cầu hoà (Tie) vẫn tính là 1 tay của chu kỳ; tiền được hoàn lại nên không lên bậc.
"""

import math

import engine as E
import money as M
import table_bets as TB

PROFILES = {          # (số tay bên mạnh, số tay bên đổi) của một khối
    "3-3": (3, 3),
    "3-2": (3, 2),
}
DEFAULT_PROFILE = "3-3"
# Mức cược mặc định chỉ để điền sẵn vào ô nhập; người dùng tự đổi từng bậc, không nhân cứng.
# Đơn vị theo đúng số hiển thị trên sàn (VN2): bậc 1 = 100, không phải 100.000.
DEFAULT_LADDER = {
    "3-3": [100, 300, 700, 1500, 3100, 6400],
    "3-2": [40, 160, 360, 740, 1700],
}
# Hệ số nhân cố định cho cơ chế "tự nhân": bậc n = bậc 1 × MULTIPLIER_LADDER[n-1].
# Cấu trúc gốc 1-3-7-15-32-64; khối 3-2 chỉ cần 5 bậc đầu.
MULTIPLIER_LADDER = (1, 3, 7, 15, 32, 64)
PROFILE_LABEL = {"3-3": "3-3 · 6 tay (3 mạnh + 3 đổi)", "3-2": "3-2 · 5 tay (3 mạnh + 2 đổi)"}
MAX_STREAK_ALLOWED = 6  # bệt tối đa 6 tay còn nhận; từ 7 tay liền loại bàn
MIN_HANDS = 10
MAX_HANDS = 50
# Người dùng chỉnh khoảng tay đã đi trong Cài đặt; hai mốc dưới là biên hợp lệ.
HANDS_LOWER = 10
HANDS_UPPER = 50


def normalize_hand_range(min_hands=MIN_HANDS, max_hands=MAX_HANDS):
    """Chuẩn hoá khoảng tay đã đi: kẹp trong HANDS_LOWER..HANDS_UPPER và bảo đảm min<=max."""
    def clamp(value, fallback):
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = fallback
        return max(HANDS_LOWER, min(HANDS_UPPER, number))
    low = clamp(min_hands, MIN_HANDS)
    high = clamp(max_hands, MAX_HANDS)
    if low > high:
        low, high = high, low
    return low, high


def block_hands(profile):
    return sum(PROFILES[profile])


def strong_hands(profile):
    return PROFILES[profile][0]


def multiply_ladder(base, profile=DEFAULT_PROFILE):
    """Sinh danh sách mức cược bằng hệ số cố định 1-3-7-15-32-64 từ bậc 1 (``base``).

    Số bậc khớp số tay của khối: 6 bậc cho 3-3, 5 bậc cho 3-2.
    """
    need = block_hands(profile)
    base = float(base)
    return [base * MULTIPLIER_LADDER[i] for i in range(need)]


def ladder_stakes(profile, ladder=None):
    """Chuẩn hoá danh sách mức cược: đúng số tay của khối, mỗi mức là số dương."""
    stakes = list(DEFAULT_LADDER[profile] if ladder is None else ladder)
    need = block_hands(profile)
    if len(stakes) != need:
        raise ValueError("Phương pháp %s cần đúng %d mức cược, đang có %d mức." % (profile, need, len(stakes)))
    if not all(math.isfinite(float(s)) and float(s) > 0 for s in stakes):
        raise ValueError("Mỗi bậc cược phải là một số lớn hơn 0.")
    return [float(s) for s in stakes]


def parse_road(text):
    """Đọc cầu dạng chữ: B=Banker, P=Player, T=Hoà. Mọi ký tự khác bị bỏ qua."""
    mapping = {"B": E.BANKER, "P": E.PLAYER, "T": E.TIE}
    return [mapping[ch] for ch in text.upper() if ch in mapping]


def has_long_streak(results, limit=MAX_STREAK_ALLOWED + 1):
    """Tìm vệt dài vượt mức cho phép: vệt dài hơn ``limit - 1`` bị coi là bệt.

    Mặc định loại bàn khi có 7 tay liền trở lên (tức chỉ nhận tối đa 6 tay).
    """
    for side, length in E.run_lengths(results):
        if length >= limit:
            return side, length
    return None, 0


def has_3_3(results):
    """Đã từng có hai vệt 3 tay liên tiếp nhau (cầu 3-3) trong ván này chưa."""
    runs = E.run_lengths(results)
    for i in range(len(runs) - 1):
        if runs[i][1] == 3 and runs[i + 1][1] == 3:
            return True, (runs[i][0], runs[i + 1][0])
    return False, None


def strong_side(results, lookback=None):
    """Bên mạnh = bên ra nhiều tay hơn trên TOÀN BỘ ván đang đi (đúng số liệu
    Banker/Player hiển thị trên UI), không chỉ vài tay gần nhất. Hòa điểm thì
    lấy bên của vệt hiện tại.

    ``lookback`` được giữ lại trong chữ ký để tương thích lời gọi cũ nhưng
    không còn cắt bớt lịch sử; truyền số dương vẫn hoạt động như trước nếu có
    nơi nào cố tình muốn giới hạn, nhưng mặc định None nghĩa là toàn ván.
    """
    dec = E.decisions_only(results)
    if lookback:
        dec = dec[-lookback:]
    if not dec:
        return None, {}
    b, p = dec.count(E.BANKER), dec.count(E.PLAYER)
    if b == p:
        runs = E.run_lengths(results)
        side = runs[-1][0] if runs else None
        return side, {"banker": b, "player": p, "tie_break": True}
    side = E.BANKER if b > p else E.PLAYER
    return side, {"banker": b, "player": p, "tie_break": False}


def check_table(results, min_hands=MIN_HANDS, max_hands=MAX_HANDS):
    """Kết luận bàn này có đủ điều kiện để vào theo nguyên tắc 1 hay không."""
    min_hands, max_hands = normalize_hand_range(min_hands, max_hands)
    hands = len(results)
    streak_side, streak_len = has_long_streak(results)
    already_33, pair = has_3_3(results)

    if streak_side:
        streak_detail = "có bệt %s %d tay" % (E.SIDE_NAME[streak_side], streak_len)
    else:
        streak_detail = "chưa có vệt nào quá %d tay" % MAX_STREAK_ALLOWED
    if already_33:
        three3_detail = "đã có 3-3 (%s-%s)" % (E.SIDE_NAME[pair[0]], E.SIDE_NAME[pair[1]])
    else:
        three3_detail = "chưa có hai vệt 3 tay liên tiếp"

    checks = [
        {"name": "Số tay đã đi %d-%d" % (min_hands, max_hands),
         "ok": min_hands <= hands <= max_hands,
         "detail": "ván này đã đi %d tay" % hands},
        {"name": "Cầu ngắn, bệt tối đa %d tay" % MAX_STREAK_ALLOWED,
         "ok": streak_side is None, "detail": streak_detail},
        {"name": "Chưa từng có cầu 3-3", "ok": not already_33, "detail": three3_detail},
    ]
    side, detail = strong_side(results)
    runs = E.run_lengths(results)
    return {
        "eligible": all(c["ok"] for c in checks),
        "hands": hands,
        "checks": checks,
        "strong": side,
        "weak": E.PLAYER if side == E.BANKER else E.BANKER,
        "strong_detail": detail,
        "streak": runs[-1] if runs else None,
    }


def plan(results, stakes, ladder_step=0, hands_in_block=0, started=False,
         strong=None, profile=DEFAULT_PROFILE, min_hands=MIN_HANDS, max_hands=MAX_HANDS):
    """Bàn tay tiếp theo nên làm gì. Trả về cửa, số tiền, vị trí trong khối theo phương pháp đã chọn."""
    stakes = ladder_stakes(profile, stakes)
    cycle = strong_hands(profile)
    block = block_hands(profile)
    info = check_table(results, min_hands, max_hands)

    if not started:
        if not info["eligible"]:
            return {"action": "wait", "side": None, "stake": 0, "reason": next(
                c["detail"] for c in info["checks"] if not c["ok"]), "checks": info["checks"],
                "block_pos": 0, "ladder_step": 0, "strong": info["strong"]}
        return {"action": "start", "side": info["strong"], "stake": stakes[0],
                "reason": "Bàn đủ điều kiện. Cầu mạnh bên %s -> đánh %d tay đầu khối %s." % (
                    E.SIDE_NAME[info["strong"]], cycle, profile),
                "checks": info["checks"], "block_pos": 0, "ladder_step": 0, "strong": info["strong"]}

    if strong is None:
        strong = info["strong"] or E.BANKER
    weak = E.PLAYER if strong == E.BANKER else E.BANKER

    if hands_in_block >= block or (started and hands_in_block == 0):
        hands_in_block = 0          # sang khối mới, tính lại bên mạnh
        strong = info["strong"] or strong
        weak = E.PLAYER if strong == E.BANKER else E.BANKER

    pos = hands_in_block
    side = strong if pos < cycle else weak
    streak_side, streak_len = has_long_streak(results)
    already_33, _ = has_3_3(results)

    if ladder_step >= len(stakes):
        return {"action": "halt", "side": None, "stake": 0,
                "reason": "Đã thua hết %d bậc gấp thếp (mất %s). Dừng, quay về bậc 1 %s." % (
                    len(stakes), M.fmt_money(sum(stakes)), M.fmt_money(stakes[0])),
                "checks": info["checks"], "block_pos": pos, "ladder_step": ladder_step, "strong": strong}

    stake = stakes[ladder_step]
    label = "%d tay bên mạnh" % cycle if pos < cycle else "%d tay bên đổi" % (block - cycle)
    reason = "%s: tay %d/%d của khối %s, bậc %d/%d -> bắt %s %s." % (
        label, pos + 1, block, profile, ladder_step + 1, len(stakes),
        E.SIDE_NAME[side], M.fmt_money(stake))
    if streak_side:
        reason += " Cẩn thận: bàn vừa xuất hiện bệt %d tay." % streak_len
    if already_33:
        reason += " Bàn đã có 3-3, nguyên tắc 1 không còn thoả."
    return {"action": "bet", "side": side, "stake": stake, "reason": reason,
            "checks": info["checks"], "block_pos": pos, "ladder_step": ladder_step, "strong": strong}


def compare_chips(plan_side, plan_stake, live, unit=None):
    """So chip thật đang đặt trên bàn với kế hoạch của khối 3-3/3-2.

    plan_side/plan_stake: cửa và số tiền (VNĐ) kế hoạch đã chốt cho tay tiếp theo.
    live: dict từ table_bets.read(); ``spots[side]["amount_vnd"]`` đã là VNĐ nên so trực tiếp
    với kế hoạch. ``suggested_chips`` cho biết cần bấm những chip nào để đạt số tiền đó.

    Trả dict(status, side, expected, actual, extra, suggested_chips, note):
      status: "match"        chip đúng cửa, đúng tiền
              "no_bet"       chưa đặt gì
              "wrong_side"   đặt sang cửa khác
              "wrong_amount" đúng cửa nhưng khác tiền
              "extra_bets"   đúng cửa nhưng đặt thêm cửa khác
              "ungroupable"  số tiền không ghép được bằng mệnh giá chip đang có
              "unknown"      chưa đọc được bàn
    """
    expected = float(plan_stake or 0)
    from_chip = TB
    if not isinstance(live, dict) or not live.get("ok"):
        split = from_chip.decompose(expected, unit or from_chip.DEFAULT_CHIP_UNIT)
        return {"status": "unknown", "side": plan_side, "expected": expected, "actual": 0.0,
                "extra": {}, "suggested_chips": split["chips"],
                "note": "Chưa đọc được chip trên bàn."}

    chip_unit = int(live.get("unit") or unit or from_chip.DEFAULT_CHIP_UNIT)
    spots = live.get("spots") or {}
    amounts = {side: float((spots.get(side) or {}).get("amount_vnd") or 0)
               for side in (E.BANKER, E.PLAYER, E.TIE)}
    total = sum(amounts.values())
    actual = amounts.get(plan_side, 0.0)
    extra = {side: value for side, value in amounts.items() if side != plan_side and value > 0}
    split = from_chip.decompose(expected, chip_unit, live.get("denominations") or None)

    if not split["exact"]:
        return {"status": "ungroupable", "side": plan_side, "expected": expected,
                "actual": actual, "extra": extra, "suggested_chips": split["chips"],
                "note": "%s không ghép được bằng mệnh giá chip đang có (thiếu %s)." % (
                    M.fmt_money(expected), M.fmt_money(split["remainder_vnd"]))}
    if total <= 0:
        return {"status": "no_bet", "side": plan_side, "expected": expected, "actual": 0.0,
                "extra": {}, "suggested_chips": split["chips"],
                "note": "Chưa đặt chip nào trên bàn."}
    if actual <= 0 and extra:
        placed = next(iter(extra))
        return {"status": "wrong_side", "side": plan_side, "expected": expected, "actual": 0.0,
                "extra": extra, "suggested_chips": split["chips"],
                "note": "Đang đặt %s ở %s — kế hoạch là %s." % (
                    M.fmt_money(sum(extra.values())), E.SIDE_NAME[placed], E.SIDE_NAME[plan_side])}
    if abs(actual - expected) >= 1:
        return {"status": "wrong_amount", "side": plan_side, "expected": expected, "actual": actual,
                "extra": extra, "suggested_chips": split["chips"],
                "note": "Đặt %s nhưng kế hoạch là %s ở %s." % (
                    M.fmt_money(actual), M.fmt_money(expected), E.SIDE_NAME[plan_side])}
    if extra:
        return {"status": "extra_bets", "side": plan_side, "expected": expected, "actual": actual,
                "extra": extra, "suggested_chips": split["chips"],
                "note": "Đúng %s %s nhưng còn đặt thêm %s ở cửa khác." % (
                    E.SIDE_NAME[plan_side], M.fmt_money(actual),
                    M.fmt_money(sum(extra.values())))}
    return {"status": "match", "side": plan_side, "expected": expected, "actual": actual,
            "extra": {}, "suggested_chips": split["chips"],
            "note": "Chip khớp kế hoạch: %s %s." % (
                E.SIDE_NAME[plan_side], M.fmt_money(actual))}


CHIP_STATUS_LABEL = {
    "match": "KHỚP",
    "no_bet": "CHƯA ĐẶT",
    "wrong_side": "SAI CỬA",
    "wrong_amount": "SAI TIỀN",
    "extra_bets": "ĐẶT THÊM",
    "ungroupable": "KHÔNG GHÉP ĐƯỢC",
    "unknown": "CHƯA ĐỌC",
}


def settle(bet_side, result, stake, commission=0.95):
    """Trả về (delta tiền, kết quả, bước gấp thếp kế tiếp: 'reset'|'hold'|'up')."""
    if result == E.TIE:
        return 0.0, "push", "hold"
    if bet_side == result:
        rate = commission if bet_side == E.BANKER else 1.0
        return stake * rate, "win", "reset"
    return -stake, "loss", "up"


def advance(state, result, bet_side, stake, profile=DEFAULT_PROFILE):
    """Cập nhật trạng thái sau một tay. state: dict(ladder_step, hands_in_block, started, strong)."""
    delta, outcome, step_action = settle(bet_side, result, stake)
    state = dict(state)
    state["hands_in_block"] = 0 if state.get("hands_in_block", 0) + 1 >= block_hands(profile) \
        else state["hands_in_block"] + 1
    if step_action == "up":
        state["ladder_step"] = state.get("ladder_step", 0) + 1
    elif step_action == "reset":
        state["ladder_step"] = 0
    state["started"] = True
    return state, {"delta": delta, "outcome": outcome}
