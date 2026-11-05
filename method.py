# -*- coding: utf-8 -*-
"""Các phương pháp vào cầu: lọc bàn, đánh khối bên mạnh -> bên đổi, bậc cược nhập tay theo khối.

Quy tắc của người dùng:
1. Chọn bàn: đã đi 15-20 tay, cầu ngắn (không có bệt từ 7 tay trở lên), chưa từng có cầu 3-3.
2. Cầu đang mạnh bên nào -> đánh mấy tay đầu khối bên đó, rồi đổi sang bên kia mấy tay còn lại.
   Khối 3-3 đi 6 tay, khối 3-2 đi 5 tay.
3. Thắng thì quay về bậc 1. Thua thì sang bậc kế tiếp trong danh sách mức cược người dùng nhập.
4. Cầu hoà (Tie) vẫn tính là 1 tay của chu kỳ; tiền được hoàn lại nên không lên bậc.
"""

import math

import engine as E
import money as M

PROFILES = {          # (số tay bên mạnh, số tay bên đổi) của một khối
    "3-3": (3, 3),
    "3-2": (3, 2),
}
DEFAULT_PROFILE = "3-3"
# Mức cược mặc định chỉ để điền sẵn vào ô nhập; người dùng tự đổi từng bậc, không nhân cứng.
DEFAULT_LADDER = {
    "3-3": [100_000, 300_000, 700_000, 1_500_000, 3_100_000, 6_400_000],
    "3-2": [40_000, 160_000, 360_000, 740_000, 1_700_000],
}
PROFILE_LABEL = {"3-3": "3-3 · 6 tay (3 mạnh + 3 đổi)", "3-2": "3-2 · 5 tay (3 mạnh + 2 đổi)"}
STREAK_IS_BET = 7       # từ 7 tay liên tiếp tính là bệt -> loại bàn
MIN_HANDS = 15
MAX_HANDS = 20
LOOKBACK_STRONG = 10    # số tay phân thắng bại gần nhất để xác định bên mạnh


def block_hands(profile):
    return sum(PROFILES[profile])


def strong_hands(profile):
    return PROFILES[profile][0]


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
