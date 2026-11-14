# -*- coding: utf-8 -*-
"""Tang Model: chia so tien thanh menh gia chip (thuan so hoc, khong I/O).

Truoc day ham nay nam trong ``table_bets`` (tang Adapter) nen ``method`` - mot module
Model - phai import Adapter. Tach ra day de Model khong con phu thuoc Adapter.
"""

# 1 don vi chip = 1 don vi hien thi tren san (VN2). Chip "100" = 100, "20" = 20.
DEFAULT_CHIP_UNIT = 1
# Menh gia khay chip theo don vi hien thi; khay that luon duoc doc tu DOM va thay the.
DEFAULT_DENOMINATIONS = (20, 40, 100, 500, 2000, 10000)


def decompose(amount, unit=DEFAULT_CHIP_UNIT, denominations=None):
    """Chia số tiền thành các chip cần bấm, theo mệnh giá thật của khay.

    Toàn bộ dùng chung một thang số với sàn (VN2): amount = 140 nghĩa là 140 trên
    bảng, unit mặc định 1 nên 140 -> [100, 20, 20].

    Trả dict:
      exact: True nếu chia hết và ghép đủ đúng số tiền
      chips: danh sách mệnh giá (đơn vị hiển thị) theo thứ tự lớn -> nhỏ
      units: tổng đơn vị chip đã ghép
      remainder_vnd: phần tiền không ghép được bằng mệnh giá hiện có
      total_vnd: số tiền quy đổi từ chips
    """
    denominations = sorted({int(d) for d in (denominations or DEFAULT_DENOMINATIONS) if int(d) > 0})
    try:
        amount = float(amount or 0)
    except (TypeError, ValueError):
        amount = 0.0
    unit = max(1, int(unit or DEFAULT_CHIP_UNIT))
    target = int(round(amount / unit)) if amount > 0 else 0
    if target <= 0:
        return {"exact": True, "chips": [], "units": 0, "remainder_vnd": 0.0, "total_vnd": 0.0}
    if not denominations:
        return {"exact": False, "chips": [], "units": 0,
                "remainder_vnd": amount, "total_vnd": 0.0}

    # Quy hoạch động: số chip ít nhất để đạt đúng từng mốc đơn vị (không giới hạn số chip).
    best = {0: ()}
    for value in range(1, target + 1):
        choice = None
        for denom in denominations:
            if denom > value:
                break
            prev = best.get(value - denom)
            if prev is None:
                continue
            candidate = prev + (denom,)
            if choice is None or len(candidate) < len(choice):
                choice = candidate
        if choice is not None:
            best[value] = choice
    chips = best.get(target)
    if chips is not None:
        ordered = tuple(sorted(chips, reverse=True))
        return {"exact": True, "chips": list(ordered), "units": target,
                "remainder_vnd": 0.0, "total_vnd": float(target * unit)}

    # Không ghép đủ: tham lam phần lớn nhất có thể để vẫn gợi ý hữu ích.
    remaining, picked = target, []
    for denom in sorted(denominations, reverse=True):
        count, remaining = divmod(remaining, denom)
        picked.extend([denom] * count)
    return {"exact": False, "chips": picked, "units": target - remaining,
            "remainder_vnd": float(remaining * unit), "total_vnd": float((target - remaining) * unit)}
