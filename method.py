# -*- coding: utf-8 -*-
"""Phương pháp vào cầu 3-3: lọc bàn, đánh 3 tay bên mạnh -> 3 tay bên kia, gấp thếp 6 bậc.

Quy tắc của người dùng:
1. Chọn bàn: đã đi 15-20 tay, cầu ngắn (không có bệt từ 7 tay trở lên), chưa từng có cầu 3-3.
2. Cầu đang mạnh bên nào -> đánh 3 tay bên đó, rồi đổi sang bên kia 3 tay (khối 6 tay).
3. Ăn thì quay về cược gốc 100k. Bậc gấp thếp tối đa 6 tay.
4. Cầu hoà (Tie) vẫn tính là 1 tay của chu kỳ; tiền được hoàn lại nên không lên bậc.
"""

import engine as E
import money as M

BET_CYCLE = 3           # số tay mỗi bên trong khối 3-3
BLOCK_HANDS = 6         # 3 tay bên mạnh + 3 tay bên kia
LADDER_MULT = [1, 3, 7, 15, 31, 64]   # x cược gốc: 100k-300k-700k-1.5tr-3.1tr-6.4tr
MAX_LADDER = len(LADDER_MULT)
STREAK_IS_BET = 7       # từ 7 tay liên tiếp tính là bệt -> loại bàn
MIN_HANDS = 15
MAX_HANDS = 20
LOOKBACK_STRONG = 10    # số tay phân thắng bại gần nhất để xác định bên mạnh


def parse_road(text):
    """Đọc cầu dạng chữ: B=Banker, P=Player, T=Hoà. Mọi ký tự khác bị bỏ qua."""
    mapping = {"B": E.BANKER, "P": E.PLAYER, "T": E.TIE}
    return [mapping[ch] for ch in text.upper() if ch in mapping]


def ladder_stakes(base_stake):
    return [base_stake * m for m in LADDER_MULT]


def has_long_streak(results, limit=STREAK_IS_BET):
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


def strong_side(results, lookback=LOOKBACK_STRONG):
    """Bên mạnh = nhiều tay thắng hơn trong lookback tay phân thắng bại gần nhất.
    Hòa điểm thì lấy bên của vệt hiện tại."""
    dec = E.decisions_only(results)[-lookback:]
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
    hands = len(results)
    streak_side, streak_len = has_long_streak(results)
    already_33, pair = has_3_3(results)

    if streak_side:
        streak_detail = "có bệt %s %d tay" % (E.SIDE_NAME[streak_side], streak_len)
    else:
        streak_detail = "chưa có vệt nào tới %d tay" % STREAK_IS_BET
    if already_33:
        three3_detail = "đã có 3-3 (%s-%s)" % (E.SIDE_NAME[pair[0]], E.SIDE_NAME[pair[1]])
    else:
        three3_detail = "chưa có hai vệt 3 tay liên tiếp"

    checks = [
        {"name": "Số tay đã đi %d-%d" % (min_hands, max_hands),
         "ok": min_hands <= hands <= max_hands,
         "detail": "ván này đã đi %d tay" % hands},
        {"name": "Cầu ngắn, không bệt từ %d tay" % STREAK_IS_BET,
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


def plan(results, base_stake=100000, ladder_step=0, hands_in_block=0, started=False,
         strong=None, min_hands=MIN_HANDS, max_hands=MAX_HANDS):
    """Bàn tay tiếp theo nên làm gì. Trả về cửa, số tiền, vị trí trong khối 3-3."""
    stakes = ladder_stakes(base_stake)
    info = check_table(results, min_hands, max_hands)

    if not started:
        if not info["eligible"]:
            return {"action": "wait", "side": None, "stake": 0, "reason": next(
                c["detail"] for c in info["checks"] if not c["ok"]), "checks": info["checks"],
                "block_pos": 0, "ladder_step": 0, "strong": info["strong"]}
        return {"action": "start", "side": info["strong"], "stake": stakes[0],
                "reason": "Bàn đủ điều kiện. Cầu mạnh bên %s -> đánh 3 tay đầu khối." % E.SIDE_NAME[info["strong"]],
                "checks": info["checks"], "block_pos": 0, "ladder_step": 0, "strong": info["strong"]}

    if strong is None:
        strong = info["strong"] or E.BANKER
    weak = E.PLAYER if strong == E.BANKER else E.BANKER

    if hands_in_block >= BLOCK_HANDS or (started and hands_in_block == 0):
        hands_in_block = 0          # sang khối mới, tính lại bên mạnh
        strong = info["strong"] or strong
        weak = E.PLAYER if strong == E.BANKER else E.BANKER

    pos = hands_in_block
    side = strong if pos < BET_CYCLE else weak
    streak_side, streak_len = has_long_streak(results)
    already_33, _ = has_3_3(results)

    if ladder_step >= MAX_LADDER:
        return {"action": "halt", "side": None, "stake": 0,
                "reason": "Đã thua hết %d bậc gấp thếp (mất %s). Dừng, quay về cược gốc %s." % (
                    MAX_LADDER, M.fmt_money(sum(stakes)), M.fmt_money(base_stake)),
                "checks": info["checks"], "block_pos": pos, "ladder_step": ladder_step, "strong": strong}

    stake = stakes[ladder_step]
    label = "3 tay bên mạnh" if pos < BET_CYCLE else "3 tay bên đổi"
    reason = "%s: tay %d/%d của khối, bậc %d/%d -> bắt %s %s." % (
        label, pos + 1, BLOCK_HANDS, ladder_step + 1, MAX_LADDER, E.SIDE_NAME[side], M.fmt_money(stake))
    if streak_side and streak_len >= STREAK_IS_BET:
        reason += " Cẩn thận: bàn vừa xuất hiện bệt %d tay." % streak_len
    if already_33:
        reason += " Bàn đã có 3-3, nguyên tắc 1 không còn thoả."
    return {"action": "bet", "side": side, "stake": stake, "reason": reason,
            "checks": info["checks"], "block_pos": pos, "ladder_step": ladder_step, "strong": strong}


def settle(bet_side, result, stake, commission=0.95):
    """Trả về (delta tiền, kết quả, bước gấp thếp kế tiếp: 'reset'|'hold'|'up')."""
    if result == E.TIE:
        return 0.0, "push", "hold"
    if bet_side == result:
        rate = commission if bet_side == E.BANKER else 1.0
        return stake * rate, "win", "reset"
    return -stake, "loss", "up"


def advance(state, result, bet_side, stake):
    """Cập nhật trạng thái sau một tay. state: dict(ladder_step, hands_in_block, started, strong)."""
    delta, outcome, step_action = settle(bet_side, result, stake)
    state = dict(state)
    state["hands_in_block"] = 0 if state.get("hands_in_block", 0) + 1 >= BLOCK_HANDS \
        else state["hands_in_block"] + 1
    if step_action == "up":
        state["ladder_step"] = state.get("ladder_step", 0) + 1
    elif step_action == "reset":
        state["ladder_step"] = 0
    state["started"] = True
    return state, {"delta": delta, "outcome": outcome}
