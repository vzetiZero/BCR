# -*- coding: utf-8 -*-
"""Logic soi cầu Baccarat: đường cầu (road) và nhận diện pattern. Không phụ thuộc giao diện."""

BANKER, PLAYER, TIE = "B", "P", "T"
BIG_ROAD_ROWS = 6
BIG_ROAD_VIEW_COLS = 12

SIDE_NAME = {BANKER: "Banker", PLAYER: "Player", TIE: "Tie (Hoà)"}
SIDE_SHORT = {BANKER: "B", PLAYER: "P", TIE: "T"}

PAYOUT = {BANKER: 0.95, PLAYER: 1.00, TIE: 8.00}

HOUSE_FACTS = [
    ("Xác suất Banker thắng", "45.86%"),
    ("Xác suất Player thắng", "44.62%"),
    ("Xác suất hoà (Tie)", "9.52%"),
    ("Lợi thế nhà cái — cửa Banker", "1.06%"),
    ("Lợi thế nhà cái — cửa Player", "1.24%"),
    ("Lợi thế nhà cái — cửa Tie", "14.36%"),
]


def decisions_only(results):
    return [r for r in results if r in (BANKER, PLAYER)]


def build_big_road(results):
    """Dựng Big Road: mỗi vệt cùng cửa chạy xuống, hết hàng hoặc gặp ô chắn thì rẽ phải.
    Tie không tạo ô mới mà chấm vào ô của vệt đang chạy."""
    cells = []
    used = set()
    last_cell = {}
    newest = None
    max_col = -1
    leading_ties = 0

    for raw in results:
        if raw == TIE:
            if newest is None:
                leading_ties += 1
            else:
                cells[newest]["ties"] += 1
            continue

        ties = leading_ties
        leading_ties = 0
        prev = last_cell.get(raw)

        if prev is not None and prev == newest:
            row, col = cells[prev]["row"], cells[prev]["col"]
            if row + 1 < BIG_ROAD_ROWS and (row + 1, col) not in used:
                row += 1
            else:
                col += 1
                if (row, col) in used:
                    row += 1
        else:
            row, col = 0, max_col + 1

        cells.append({"row": row, "col": col, "side": raw, "ties": ties})
        used.add((row, col))
        newest = len(cells) - 1
        last_cell[raw] = newest
        max_col = max(max_col, col)

    return {"cells": cells, "cols": max_col + 1, "rows": BIG_ROAD_ROWS}


def run_lengths(results):
    """Độ dài các vệt liên tiếp, bỏ qua Tie: [(side, length), ...]"""
    runs = []
    for d in decisions_only(results):
        if runs and runs[-1][0] == d:
            runs[-1] = (d, runs[-1][1] + 1)
        else:
            runs.append((d, 1))
    return runs


def analyze(results):
    """Gợi ý cửa cho tay kế tiếp kèm lý do và độ tin cậy."""
    runs = run_lengths(results)
    out = {
        "suggestion": None,
        "pattern": "Chưa đủ dữ liệu",
        "reason": "Cần ít nhất 4 tay phân thắng bại để nhận diện cầu.",
        "confidence": 0.0,
        "streak": runs[-1][1] if runs else 0,
        "side": runs[-1][0] if runs else None,
        "stand_aside": True,
    }
    if len(decisions_only(results)) < 4:
        return out

    current_side, current_len = runs[-1]
    opposite = PLAYER if current_side == BANKER else BANKER
    _fill(out, current_side, current_len, runs, opposite)
    if out["suggestion"] is None and out["pattern"] != "Cầu rối / không rõ nhịp":
        out["stand_aside"] = True
    return out


def _fill(out, current_side, current_len, runs, opposite):
    name = SIDE_NAME[current_side]

    # 1. Cầu bệt
    if current_len >= 3:
        if current_len <= 5:
            out.update(
                pattern="Cầu bệt %s %d tay" % (name, current_len),
                suggestion=current_side,
                confidence=0.50 + 0.05 * (current_len - 3),
                reason="%s ra liên tiếp %d tay — cầu bệt thường còn chạy tới, gợi ý đánh tới theo %s."
                % (name, current_len, name),
            )
        elif current_len <= 7:
            out.update(
                pattern="Cầu bệt dài %s %d tay" % (name, current_len),
                suggestion=current_side,
                confidence=max(0.30, 0.45 - 0.05 * (current_len - 5)),
                reason="Bệt đã %d tay, xác suất đứt tăng dần. Nếu vẫn chơi thì theo %s với cược nhỏ và sẵn sàng dừng."
                % (current_len, name),
            )
        else:
            out.update(
                pattern="Bệt cực dài (%d tay)" % current_len,
                suggestion=None,
                confidence=0.25,
                reason="Bệt %d tay là hiếm; không có cơ sở để đoán nó tiếp tục hay đứt. Nên đứng ngoài." % current_len,
            )
        out["stand_aside"] = out["suggestion"] is None
        return

    # 2. Cầu bập bênh 1-1
    ones = 0
    for _side, length in reversed(runs):
        if length != 1:
            break
        ones += 1
    if ones >= 5:
        out.update(
            pattern="Cầu bập bênh 1-1 (%d tay đổi cửa liên tiếp)" % ones,
            suggestion=opposite,
            confidence=min(0.70, 0.48 + 0.04 * (ones - 5)),
            reason="Cầu đảo cửa đều tay. Gợi ý bắt %s để theo nhịp 1-1." % SIDE_NAME[opposite],
        )
        out["stand_aside"] = False
        return

    # 3. Cầu nhịp đều (2-2, 3-3...)
    recent = [length for _s, length in runs[:-1][-3:]]
    if len(recent) == 3 and len(set(recent)) == 1:
        ref = recent[0]
        if ref >= 2 and current_len <= ref:
            if current_len < ref:
                out.update(
                    pattern="Cầu nhịp %d-%d" % (ref, ref),
                    suggestion=current_side,
                    confidence=0.55,
                    reason="Mỗi cửa đang ra %d tay liên tiếp. Vệt %s mới được %d tay — đánh tới cho đủ nhịp %d."
                    % (ref, name, current_len, ref),
                )
            else:
                out.update(
                    pattern="Cầu nhịp %d-%d (đã đủ nhịp)" % (ref, ref),
                    suggestion=opposite,
                    confidence=0.58,
                    reason="Vệt %s đã đủ %d tay theo nhịp. Gợi ý bắt %s." % (name, ref, SIDE_NAME[opposite]),
                )
            out["stand_aside"] = False
            return

    # 4. Cầu xen kẽ 1-2
    tail = [length for _s, length in runs[-5:]]
    alternating = len(tail) >= 4 and all(
        sorted((tail[i], tail[i + 1])) == [1, 2] for i in range(len(tail) - 1)
    )
    if alternating:
        expected = tail[-2]
        seq = "-".join(str(t) for t in tail)
        if current_len < expected:
            out.update(
                pattern="Cầu xen kẽ 1-2 (%s)" % seq,
                suggestion=current_side,
                confidence=0.52,
                reason="Nhịp xen kẽ dự đoán vệt %s dài %d tay, hiện mới %d — đánh tới." % (name, expected, current_len),
            )
        else:
            out.update(
                pattern="Cầu xen kẽ 1-2 (%s)" % seq,
                suggestion=opposite,
                confidence=0.54,
                reason="Vệt %s đã đạt %d tay theo nhịp xen kẽ — bắt %s." % (name, expected, SIDE_NAME[opposite]),
            )
        out["stand_aside"] = False
        return

    # 5. Không vào nhịp nào
    out.update(
        pattern="Cầu rối / không rõ nhịp",
        suggestion=None,
        confidence=0.35,
        reason="Các vệt gần đây không theo nhịp ổn định. Banker vẫn ít lợi thế nhà cái hơn Player, "
        "nhưng tốt nhất là đứng ngoài chờ cầu rõ.",
    )
    out["stand_aside"] = True


def stats(results):
    dec = decisions_only(results)
    total = len(results)
    runs = run_lengths(results)
    longest = {BANKER: 0, PLAYER: 0}
    for side, length in runs:
        longest[side] = max(longest[side], length)
    return {
        "hands": total,
        "banker": dec.count(BANKER),
        "player": dec.count(PLAYER),
        "tie": results.count(TIE),
        "banker_pct": (dec.count(BANKER) / len(dec) * 100) if dec else 0.0,
        "player_pct": (dec.count(PLAYER) / len(dec) * 100) if dec else 0.0,
        "tie_pct": (results.count(TIE) / total * 100) if total else 0.0,
        "longest": longest,
    }
