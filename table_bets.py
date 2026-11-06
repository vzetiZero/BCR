# -*- coding: utf-8 -*-
"""Đọc tiền cược đang đặt thật trên bàn (chip) qua DOM/CDP, chỉ đọc.

Markup Evolution Baccarat đã khảo sát trên bàn thật:
    [data-role="bet-spot-Banker"|"bet-spot-Player"|"bet-spot-Tie"]
        -> [data-role="chip"][data-value="100"]   (chip đã đặt vào cửa)
    [data-role="total-bet-label"]   -> "TỔNG ĐẶT CƯỢC 100 VN2"
    [data-role="selected-chip"]     -> mệnh giá chip đang chọn ở khay
    [data-role="chip-stack"]        -> khay chip (20/40/100/500/2000/10k)

Chip ẩn (0×0) và chip trong khay bị loại bằng kích thước thật. Không bấm, không đặt cược,
không đọc cookie/token; chuỗi nối CDP chỉ dùng để trả giá trị DOM.
"""

import json
import time

import browser as BR

SIDES = ("B", "P", "T")
SPOT_ROLE = (("Banker", "B"), ("Player", "P"), ("Tie", "T"))
# 1 đơn vị hiển thị trên chip = 1.000 VNĐ (chip "20" = 20.000 đ, "100" = 100.000 đ).
DEFAULT_CHIP_UNIT = 1000
# Mệnh giá khay chip Evolution theo đơn vị hiển thị (20 = 20.000 đ khi unit=1.000).
# Khay thật trên bàn luôn được đọc từ DOM và thay thế danh sách dự phòng này.
DEFAULT_DENOMINATIONS = (20, 50, 100, 500, 2000, 10000)

READ_JS = r"""(() => {
  const txt = (el) => (el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '');
  const digits = (s) => {
    if (s === null || s === undefined) return null;
    const m = String(s).replace(/[⁦⁩]/g, '').match(/\d[\d.,]*/);
    return m ? parseInt(m[0].replace(/[^\d]/g, ''), 10) : null;
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && st.display !== 'none' && st.visibility !== 'hidden';
  };
  const hasTable = !!document.querySelector('[data-role="Bead-road"]');
  const totalEl = document.querySelector('[data-role="total-bet-label"]');
  const selEl = document.querySelector('[data-role="selected-chip"]');

  const spots = {};
  for (const [role, side] of [['Banker', 'B'], ['Player', 'P'], ['Tie', 'T']]) {
    const spot = document.querySelector('[data-role="bet-spot-' + role + '"]');
    if (!spot) { spots[side] = null; continue; }
    const chips = [...spot.querySelectorAll('[data-role="chip"]')]
      .filter(visible)
      .map((c) => parseInt(c.getAttribute('data-value'), 10))
      .filter((v) => Number.isInteger(v) && v > 0);
    spots[side] = {amount: chips.reduce((s, v) => s + v, 0), chips};
  }

  // Mệnh giá thực có trên khay chip của bàn này.
  const tray = document.querySelector('[data-role="chip-stack"]');
  const denominations = tray
    ? [...new Set([...tray.querySelectorAll('[data-role="chip"]')]
        .filter(visible)
        .map((c) => parseInt(c.getAttribute('data-value'), 10))
        .filter((v) => Number.isInteger(v) && v > 0))].sort((a, b) => a - b)
    : [];

  // Tổng tiền tính từ CHIP THẬT đã đặt; nhãn "TỔNG ĐẶT CƯỢC" có thể trễ một nhịp so với chip.
  const spots_total = Object.values(spots).reduce((sum, s) => sum + ((s && s.amount) || 0), 0);
  const selectedChip = (() => {
    const wrap = document.querySelector('[data-role="selected-chip"]');
    if (!wrap) return null;
    const inner = wrap.querySelector('[data-role="chip"][data-value]');
    if (inner) {
      const v = parseInt(inner.getAttribute('data-value'), 10);
      if (Number.isInteger(v)) return v;
    }
    return digits(txt(wrap));
  })();

  return {
    has_table: hasTable,
    total_raw: totalEl ? txt(totalEl) : null,
    total_label: totalEl ? digits(txt(totalEl)) : null,
    total_units: spots_total,
    selected_chip: selectedChip,
    spots,
    denominations,
  };
})()"""


def decompose(amount, unit=DEFAULT_CHIP_UNIT, denominations=None):
    """Chia số tiền VNĐ thành các chip cần bấm, theo mệnh giá thật của khay.

    amount được hiểu theo VNĐ; chip trên bàn hiển thị theo ``unit`` (mặc định 1.000 đ).
    Ví dụ unit=1000: 120.000 -> [100, 20] (chip 100 và chip 20);
    140.000 -> [100, 20, 20].

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


def read(session, timeout=None, unit=DEFAULT_CHIP_UNIT):
    """Đọc chip đang đặt trên bàn đang mở và quy đổi về VNĐ.

    Số trên chip là đơn vị hiển thị (20 = 20.000 đ khi unit=1000), nên mọi giá trị trả ra
    ngoài ``raw_*`` đều đã nhân ``unit``. Trả dict:
        {ok, error, total, total_units, selected_chip, selected_chip_vnd,
         spots:{B:{amount,amount_vnd,chips,chips_vnd}}, denominations, unit, ts}
    ok=False khi chưa ở trong bàn hoặc CDP không đọc được; không ném lỗi ra UI.
    """
    out = {"ok": False, "error": "", "total": None, "total_units": None,
           "total_label": None, "total_label_units": None,
           "selected_chip": None, "selected_chip_vnd": None,
           "spots": {}, "denominations": [], "unit": int(unit or DEFAULT_CHIP_UNIT),
           "ts": time.time()}
    ws = None
    try:
        page = session._cdp_page()
        if page is None:
            out["error"] = "no_cdp_page"
            return out
        import websocket
        ws = websocket.create_connection(
            BR.validated_endpoint(page), suppress_origin=True, timeout=timeout or BR.SCRIPT_TIMEOUT,
            redirect_limit=0, http_no_proxy=["127.0.0.1", "localhost", "::1"])
        seq = 0
        contexts = []

        def command(method, params=None):
            nonlocal seq
            seq += 1
            ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
            deadline = time.monotonic() + BR.SCRIPT_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("cdp_timeout")
                ws.settimeout(remaining)
                message = json.loads(ws.recv())
                if message.get("method") == "Runtime.executionContextCreated":
                    context = message.get("params", {}).get("context", {})
                    if (context.get("auxData") or {}).get("isDefault"):
                        contexts.append(context["id"])
                if message.get("id") == seq:
                    if "error" in message:
                        raise RuntimeError("cdp_command_failed")
                    return message.get("result", {})

        command("Runtime.enable")
        # Chờ context chính xuất hiện (tab vừa mở cần một nhịp).
        deadline = time.monotonic() + 6
        while not contexts and time.monotonic() < deadline:
            command("Runtime.evaluate", {"expression": "1", "returnByValue": True})
        best = None
        for context_id in list(contexts):
            try:
                value = command("Runtime.evaluate", {
                    "expression": READ_JS, "contextId": context_id,
                    "returnByValue": True, "silent": True,
                    "timeout": BR.SCRIPT_TIMEOUT * 1000}).get("result", {}).get("value")
            except Exception:
                continue
            if isinstance(value, dict) and value.get("has_table"):
                best = value
                break
        if best is None:
            out["error"] = "no_table_view"
            return out

        unit = max(1, int(unit or DEFAULT_CHIP_UNIT))
        spots = {}
        for side in SIDES:
            entry = (best.get("spots") or {}).get(side)
            if isinstance(entry, dict):
                chips = [int(v) for v in (entry.get("chips") or [])]
                units = int(entry.get("amount") or sum(chips))
            else:
                chips, units = [], 0
            spots[side] = {"amount": units, "amount_vnd": float(units * unit),
                           "chips": chips, "chips_vnd": [float(c * unit) for c in chips]}
        # Tổng lấy từ chip thật; nhãn chỉ để đối chiếu vì có thể trễ một nhịp.
        total_units = int(best.get("total_units") or sum(s["amount"] for s in spots.values()))
        label_units = best.get("total_label")
        selected = best.get("selected_chip")
        out.update(
            ok=True,
            total_units=total_units,
            total=float(total_units * unit),
            total_label_units=label_units,
            total_label=(float(label_units * unit) if label_units is not None else None),
            selected_chip=selected,
            selected_chip_vnd=(float(selected * unit) if selected is not None else None),
            spots=spots,
            denominations=([int(v) for v in (best.get("denominations") or [])]
                           or list(DEFAULT_DENOMINATIONS)),
        )
        return out
    except Exception as exc:
        out["error"] = type(exc).__name__
        return out
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
