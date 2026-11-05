# -*- coding: utf-8 -*-
"""Kiểm chứng phương pháp 3-3 bằng mô phỏng Monte Carlo.

Chạy:  python backtest.py [số_ván]
Mỗi tay được rút độc lập theo xác suất Baccarat chuẩn 8 bộ bài:
Banker 45.86%, Player 44.62%, Tie 9.52% (hoa hồng Banker 5%).
"""

import random
import statistics
import sys

import engine as E
import method as ME
import money as M

PROBS = [(E.BANKER, 0.45860), (E.PLAYER, 0.44620), (E.TIE, 0.09520)]
BASE_STAKE = 100000
COMMISSION = 0.95


def draw(rng):
    x = rng.random()
    if x < PROBS[0][1]:
        return E.BANKER
    if x < PROBS[0][1] + PROBS[1][1]:
        return E.PLAYER
    return E.TIE


def gen_shoe(rng, mean=65, spread=8):
    n = max(25, int(rng.gauss(mean, spread)))
    return [draw(rng) for _ in range(n)]


def play_method(shoe, restart_after_bust=False):
    """Đi hết ván theo phương pháp. Trả về thống kê một ván."""
    results = []
    state = {"started": False, "ladder_step": 0, "hands_in_block": 0, "strong": None}
    pnl = 0.0
    bets = 0
    staked = 0.0
    wins = losses = pushes = 0
    bust = False
    peak = running = 0.0
    worst = 0.0

    for raw in shoe:
        if bust:
            results.append(raw)
            continue
        p = ME.plan(results, BASE_STAKE, state["ladder_step"], state["hands_in_block"],
                      state["started"], state["strong"])
        if p["action"] == "halt":
            if restart_after_bust:
                state = {"started": True, "ladder_step": 0, "hands_in_block": state["hands_in_block"],
                         "strong": state["strong"]}
                p = ME.plan(results, BASE_STAKE, 0, state["hands_in_block"], True, state["strong"])
                bust = False
            else:
                bust = True
                results.append(raw)
                continue
        if p["action"] in ("start", "bet"):
            state, res = ME.advance(state, raw, p["side"], p["stake"])
            state["strong"] = p["strong"]
            pnl += res["delta"]
            bets += 1
            staked += p["stake"]
            wins += res["outcome"] == "win"
            losses += res["outcome"] == "loss"
            pushes += res["outcome"] == "push"
            running += res["delta"]
            peak = max(peak, running)
            worst = min(worst, running - peak)
        else:
            state["strong"] = state["strong"] or p["strong"]
        results.append(raw)

    return {"pnl": pnl, "bets": bets, "staked": staked, "wins": wins, "losses": losses,
            "pushes": pushes, "bust": bust, "drawdown": worst}


def play_flat(shoe, side):
    pnl = staked = 0.0
    bets = 0
    running = peak = worst = 0.0
    for raw in shoe:
        if raw == E.TIE:
            continue
        bets += 1
        staked += BASE_STAKE
        if side == raw:
            delta = BASE_STAKE * (COMMISSION if raw == E.BANKER else 1.0)
        else:
            delta = -BASE_STAKE
        pnl += delta
        running += delta
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return {"pnl": pnl, "bets": bets, "staked": staked, "bust": False, "drawdown": worst}


def report(name, rows):
    pnls = [r["pnl"] for r in rows]
    total = sum(pnls)
    bets = sum(r["bets"] for r in rows)
    staked = sum(r["staked"] for r in rows)
    positive = sum(1 for p in pnls if p > 0)
    busts = sum(1 for r in rows if r.get("bust"))
    print("\n=== %s ===" % name)
    print("  Ván mô phỏng            : %d   (tay đặt cược: %d, trung bình %.1f tay/ván)"
          % (len(rows), int(bets), bets / len(rows)))
    print("  Cược trung bình 1 tay   : %s" % M.fmt_money(staked / bets if bets else 0))
    print("  Tổng lãi/lỗ             : %s" % _signed(total))
    print("  Lãi/lỗ trung bình 1 ván : %s" % _signed(total / len(rows)))
    print("  Lãi/lỗ trung bình 1 tay : %s" % _signed(total / bets if bets else 0))
    print("  %% ván có lãi            : %.1f%%" % (positive / len(rows) * 100))
    print("  %% ván cháy ladder 6 bậc : %.1f%%" % (busts / len(rows) * 100))
    print("  Ván lỗ nặng nhất        : %s" % _signed(min(pnls)))
    print("  Ván lãi cao nhất        : %s" % _signed(max(pnls)))
    print("  Sụt vốn sâu nhất 1 ván  : %s" % _signed(min(r["drawdown"] for r in rows)))
    if staked:
        print("  Tổng tiền đã đặt        : %s" % M.fmt_money(staked))
        print("  Phần trăm mất trên tiền đã đặt: %.2f%% (lợi thế nhà cái lý thuyết 1.06-1.24%%)"
              % (abs(total) / staked * 100))


def _signed(value):
    return ("+" if value > 0 else "") + M.fmt_money(value)


def main():
    shoes = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    rng = random.Random(20261105)
    print("Mô phỏng %d ván. Cược gốc %s, ladder %s." % (
        shoes, M.fmt_money(BASE_STAKE),
        "-".join(M.fmt_money(s) for s in ME.ladder_stakes(BASE_STAKE))))

    dataset = [gen_shoe(rng) for _ in range(shoes)]
    hands = sum(len(s) for s in dataset)

    report("Phương pháp 3-3 (dừng khi cháy ladder)", [play_method(s) for s in dataset])
    report("Phương pháp 3-3 (nối lại sau khi cháy)",
           [play_method(s, restart_after_bust=True) for s in dataset])
    report("Mốc so sánh: cược đều 100k cửa Banker mọi tay",
           [play_flat(s, E.BANKER) for s in dataset])
    report("Mốc so sánh: cược đều 100k cửa Player mọi tay",
           [play_flat(s, E.PLAYER) for s in dataset])

    # Bao nhiêu ván đủ điều kiện vào theo nguyên tắc 1
    eligible = 0
    for s in dataset:
        for i in range(ME.MIN_HANDS, min(len(s), ME.MAX_HANDS + 1)):
            if ME.check_table(s[:i])["eligible"]:
                eligible += 1
                break
    print("\nNguyên tắc 1 (lọc bàn): %d/%d ván (%.1f%%) có thời điểm đủ điều kiện để vào."
          % (eligible, shoes, eligible / shoes * 100))


if __name__ == "__main__":
    main()
