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
import human_mouse as HM

SIDES = ("B", "P", "T")
SPOT_ROLE = (("Banker", "B"), ("Player", "P"), ("Tie", "T"))
# 1 đơn vị chip = 1 đơn vị hiển thị trên sàn (VN2). Chip "100" = 100, "20" = 20.
# Không quy đổi sang VNĐ nữa để UI, khay chip và số dư luôn cùng một thang số.
DEFAULT_CHIP_UNIT = 1
# Mệnh giá khay chip theo đơn vị hiển thị; khay thật luôn được đọc từ DOM và thay thế.
DEFAULT_DENOMINATIONS = (20, 50, 100, 500, 2000, 10000)
# Giữa hai ván, dealer "mở bài"/gom chip thì khay chip và ô cược tạm biến mất khỏi DOM
# rồi render lại sau vài giây. Phải CHỜ-ĐỌC-LẠI tới khi đủ dữ liệu, không được đọc một
# lần rồi bỏ cuộc — nếu không, place_bet trả lỗi "Không bấm được chip nào" ngay cả khi
# khay vừa render lại ngay sau đó, và không một sự kiện chuột nào từng được gửi.
TARGETS_WAIT_TIMEOUT = 20.0
TARGETS_WAIT_POLL = 0.5
# Nhãn "chip đang chọn" chỉ đổi sau cú bấm một nhịp, và nhịp đó dài ra khi dealer đang
# mở bài/gom chip. Một lần đọc ngay sau click_delay vì thế hay còn nguyên mệnh giá của
# chip trước và báo nhầm "Bấm chọn chip 100 nhưng khay đang chọn 20" — nên xác nhận bằng
# nhiều lần đọc trong một cửa sổ ngắn.
CHIP_CONFIRM_TIMEOUT = 2.0
CHIP_CONFIRM_POLL = 0.2


def confirm_selected_chip(denom, read, wait=CHIP_CONFIRM_TIMEOUT, poll=CHIP_CONFIRM_POLL,
                          clock=time.monotonic, nap=time.sleep):
    """Đọc mệnh giá đang chọn nhiều lần trong cửa sổ ``wait`` trước khi kết luận là sai.

    Trả (ok, giá trị đọc được cuối cùng): khớp ở lần đọc nào cũng ok ngay; hết cửa sổ mà
    vẫn ra một mệnh giá khác thì (False, giá trị đó). Suốt cửa sổ không đọc được giá trị
    nào thì coi như không xác nhận được và KHÔNG chặn, giống hành vi cũ.
    """
    deadline = clock() + max(0.0, float(wait))
    last = None
    while True:
        value = read()
        if value is not None:
            if value == denom:
                return True, value
            last = value
        if clock() >= deadline:
            return last is None, last
        nap(poll)

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
  const tray = document.querySelector('[data-role="chip-stack"]');
  // Nhận diện bàn bằng KHU VỰC CƯỢC, không chỉ bằng cầu: khay chip + ô cược đã hiện
  // nhưng Bead-road chưa render/đang ẩn vẫn là bàn đặt được. Đòi mỗi Bead-road thì app
  // báo "no_table_view" mãi dù người dùng đã thấy khay chip, nên không bao giờ bấm.
  // Ô cược + khay chỉ có trong màn hình bàn, ô bàn ở sảnh không có nên không nhận nhầm.
  const hasTable = !!(document.querySelector('[data-role="Bead-road"]')
    || (tray && document.querySelector('[data-role="bet-spot-Banker"]')));
  const totalEl = document.querySelector('[data-role="total-bet-label"]');

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
  const denominations = tray
    ? [...new Set([...tray.querySelectorAll('[data-role="chip"]')]
        .filter(visible)
        .map((c) => parseInt(c.getAttribute('data-value'), 10))
        .filter((v) => Number.isInteger(v) && v > 0))].sort((a, b) => a - b)
    : [];

  // Tổng tiền tính từ CHIP THẬT đã đặt; nhãn "TỔNG ĐẶT CƯỢC" có thể trễ một nhịp so với chip.
  const spots_total = Object.values(spots).reduce((sum, s) => sum + ((s && s.amount) || 0), 0);
  const selectedChip = (() => {
    // Cùng thứ tự ưu tiên với SELECTED_CHIP_JS: chip được đánh dấu trong khay đổi ngay
    // khi bấm, nhãn [data-role="selected-chip"] trễ hơn một nhịp. Chỉ tìm TRONG KHAY —
    // chip preview của nhãn và chip đã đặt trên ô cược cũng mang data-role="chip", đếm
    // chung sẽ ra 2 phần tử "đang chọn" và rơi xuống nhãn trễ, báo nhầm mệnh giá.
    const marked = [...(tray || document).querySelectorAll('[data-role="chip"][data-selected="true"]')]
      .map((el) => parseInt(el.getAttribute('data-value'), 10))
      .filter((v) => Number.isInteger(v) && v > 0);
    if (marked.length === 1) return marked[0];
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


def read(session, timeout=None, unit=DEFAULT_CHIP_UNIT):
    """Đọc chip đang đặt trên bàn đang mở và quy đổi về VNĐ.

    Số in trên chip chính là số tiền VN2 hiển thị trên sàn (chip 20 = 20), nên ``unit``
    mặc định 1 và mọi giá trị trả ra ngoài ``raw_*`` đều đã nhân ``unit``. Trả dict:
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


# Vị trí (tâm) của ô cược và của từng chip mệnh giá trên khay, trả về bằng getBoundingClientRect.
# Kèm frame_offset: getBoundingClientRect ở đây là toạ độ CỦA CHÍNH KHUNG này, còn chuột
# CDP bấm theo khung gốc của tab. Bàn EVO nằm trong iframe nên phải cộng dồn vị trí các
# iframe cha, nếu không mọi cú bấm lệch đúng bằng khoảng cách khung bàn tới mép tab:
# bấm chip 100 không đổi mệnh giá (khay vẫn chọn 20) và bấm ô cược không ra chip nào.
TARGETS_JS = r"""(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 4 && r.height > 4 && st.display !== 'none' && st.visibility !== 'hidden';
  };
  const center = (r) => (r.width > 4 && r.height > 4
    ? {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)} : null);
  const frameOffset = (() => {
    let x = 0, y = 0, w = window, depth = 0;
    while (w !== w.top) {
      let el = null;
      try { el = w.frameElement; } catch (e) { return null; }
      if (!el) return null;   // khung cha khác origin: phải đo bằng CDP, không đoán
      const r = el.getBoundingClientRect();
      const st = w.parent.getComputedStyle(el);
      const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : 0; };
      x += r.x + num(st.borderLeftWidth) + num(st.paddingLeft);
      y += r.y + num(st.borderTopWidth) + num(st.paddingTop);
      w = w.parent;
      depth += 1;
      if (depth > 8) return null;
    }
    return {x: Math.round(x), y: Math.round(y), depth};
  })();
  const role = %s;
  const spot = document.querySelector('[data-role="bet-spot-' + role + '"]');
  const tray = document.querySelector('[data-role="chip-stack"]');
  const chips = {};
  if (tray) {
    // Có thể có nhiều phần tử trùng data-value trong khay (icon mờ/placeholder chồng
    // lên đúng nút chip để bấm). Chỉ giữ phần tử ẨN/KHÔNG hiển thị bị loại, và khi còn
    // nhiều ứng viên hiển thị cùng mệnh giá thì lấy đúng ô có DIỆN TÍCH LỚN NHẤT — nút
    // chip thật để bấm luôn to hơn icon trang trí/chỉ báo. Thiếu lọc này là nguyên nhân
    // bấm nhầm (vd chọn chip 20 lại ra toạ độ của icon mệnh giá khác).
    for (const el of tray.querySelectorAll('[data-role="chip"][data-value]')) {
      if (!visible(el)) continue;
      const v = parseInt(el.getAttribute('data-value'), 10);
      if (!Number.isInteger(v) || v <= 0) continue;
      const r = el.getBoundingClientRect();
      const area = r.width * r.height;
      const prev = chips[v];
      if (prev && prev.area >= area) continue;
      const c = center(r);
      if (c) chips[v] = {x: c.x, y: c.y, area};
    }
  }
  return {
    spot: spot ? center(spot.getBoundingClientRect()) : null,
    chips,
    frame_offset: frameOffset,
    is_top: window === window.top,
  };
})()"""

# Tổng chip THẬT đang nằm trên một ô cược, đọc lại sau khi bấm để biết tiền đã lên bàn
# hay chưa. Không có bước này thì place_bet báo "đã đặt" chỉ vì đã gửi sự kiện chuột,
# kể cả khi mọi cú bấm rơi lệch khung và ô cược vẫn trống.
SPOT_AMOUNT_JS = r"""(() => {
  const role = %s;
  const spot = document.querySelector('[data-role="bet-spot-' + role + '"]');
  if (!spot) return null;
  let amount = 0;
  const chips = [];
  for (const el of spot.querySelectorAll('[data-role="chip"]')) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width <= 4 || r.height <= 4 || st.display === 'none' || st.visibility === 'hidden') continue;
    const v = parseInt(el.getAttribute('data-value'), 10);
    if (!Number.isInteger(v) || v <= 0) continue;
    amount += v;
    chips.push(v);
  }
  return {amount, chips};
})()"""


def page_point(local, offset):
    """Đổi một điểm đo trong khung bàn sang toạ độ khung gốc của tab để bấm chuột."""
    if not isinstance(local, dict) or local.get("x") is None or local.get("y") is None:
        return None
    offset = offset if isinstance(offset, dict) else {}
    return {"x": float(local["x"]) + float(offset.get("x") or 0),
            "y": float(local["y"]) + float(offset.get("y") or 0)}


def frame_offset_via_cdp(command, frame_id):
    """Đo khoảng lệch khung bàn khi JS không với tới iframe cha (khác origin).

    Đi từ khung đang đọc lên khung gốc: mỗi bước hỏi phần tử iframe sở hữu khung con
    (``DOM.getFrameOwner``) rồi lấy hộp của nó (``DOM.getBoxModel``). Viewport khung con
    bắt đầu ở ô CONTENT của iframe (đã nằm trong border + padding), nên chỉ cộng đúng
    content — giống đường JS lấy border-box rồi cộng border + padding. Cộng thêm
    padding − border ở đây là đội thêm đúng bề dày viền, bấm lệch khi iframe có viền.
    Không đo được bước nào thì trả None — thà báo lỗi còn hơn bấm lệch rồi tưởng đã đặt.
    """
    try:
        command("DOM.getDocument", {"depth": 0})
        tree = command("Page.getFrameTree").get("frameTree") or {}
    except Exception:
        return None
    parents = {}

    def walk(node):
        frame = (node.get("frame") or {}).get("id")
        for child in node.get("childFrames") or []:
            child_id = (child.get("frame") or {}).get("id")
            if child_id:
                parents[child_id] = frame
            walk(child)

    walk(tree)
    if not frame_id or frame_id not in parents:
        return {"x": 0.0, "y": 0.0, "depth": 0}
    x = y = 0.0
    depth = 0
    node = frame_id
    while node in parents:
        try:
            owner = command("DOM.getFrameOwner", {"frameId": node}).get("backendNodeId")
            model = command("DOM.getBoxModel", {"backendNodeId": owner}).get("model") or {}
        except Exception:
            return None
        content = model.get("content") or []
        if len(content) < 2 or not owner:
            return None
        x += float(content[0])
        y += float(content[1])
        node = parents[node]
        depth += 1
        if depth > 8:
            return None
    return {"x": x, "y": y, "depth": depth}


def confirm_spot_amount(start_amount, expected, read, wait=CHIP_CONFIRM_TIMEOUT,
                        poll=CHIP_CONFIRM_POLL, clock=time.monotonic, nap=time.sleep):
    """Chờ tổng chip trên ô cược tăng đúng ``expected`` so với lúc bắt đầu bấm.

    DOM cập nhật chậm hơn cú bấm một nhịp nên đọc lại trong cửa sổ ngắn; khớp là ok
    ngay. Hết cửa sổ vẫn thiếu/thừa thì (False, tổng đọc được cuối) để app báo đúng số
    tiền thật đã lên bàn, không báo "đã đặt" theo số cú bấm.
    """
    deadline = clock() + max(0.0, float(wait))
    last = start_amount
    while True:
        value = read()
        if value is not None:
            last = value
            if value - start_amount == expected:
                return True, value
        if clock() >= deadline:
            return False, last
        nap(poll)

# Mệnh giá chip đang được CHỌN trên khay ngay lúc đọc — dùng để xác nhận cú bấm chọn
# mệnh giá vừa rồi có thật sự trúng đúng chip mong muốn hay không, trước khi bấm tiền
# thật vào ô cược. Cùng cách đọc digits() với READ_JS để nhất quán khi DOM chỉ lộ chữ.
#
# Site lộ trạng thái "đang chọn" ở HAI chỗ không cùng nhịp: chip trong khay gắn
# data-selected="true" (đổi ngay theo cú bấm, cũng là chỗ extension/commands.js đọc) và
# nhãn [data-role="selected-chip"] (trễ hơn một nhịp, thậm chí giữ nguyên mệnh giá cũ
# khi dealer đang mở bài). Ưu tiên tín hiệu khay — chỉ tìm TRONG [data-role="chip-stack"]
# và chỉ khi có ĐÚNG MỘT chip được đánh dấu — rồi mới tới nhãn. Nhãn cũng chứa một chip
# preview cùng data-role, đếm chung cả document sẽ thành 2 và luôn rơi xuống nhãn trễ.
SELECTED_CHIP_JS = r"""(() => {
  const digits = (s) => {
    if (s === null || s === undefined) return null;
    const m = String(s).replace(/[⁦⁩]/g, '').match(/\d[\d.,]*/);
    return m ? parseInt(m[0].replace(/[^\d]/g, ''), 10) : null;
  };
  const tray = document.querySelector('[data-role="chip-stack"]');
  const marked = [...(tray || document).querySelectorAll('[data-role="chip"][data-selected="true"]')]
    .map((el) => parseInt(el.getAttribute('data-value'), 10))
    .filter((v) => Number.isInteger(v) && v > 0);
  if (marked.length === 1) return marked[0];
  const wrap = document.querySelector('[data-role="selected-chip"]');
  if (!wrap) return null;
  const inner = wrap.querySelector('[data-role="chip"][data-value]');
  if (inner) {
    const v = parseInt(inner.getAttribute('data-value'), 10);
    if (Number.isInteger(v)) return v;
  }
  return digits((wrap.innerText || wrap.textContent || '').trim());
})()"""


def place_bet(session, side, chips, unit=DEFAULT_CHIP_UNIT, human_mouse=True,
              click_delay=0.28, amount=None):
    """Dùng chuột đặt các chip (kết quả của ``decompose``) vào ô cược ``side``.

    Evolution Baccarat: bấm một chip mệnh giá trên khay để chọn mệnh giá đó, rồi mỗi
    cú bấm vào ô bet-spot đặt thêm một chip mệnh giá đó. Vì vậy đặt ``count`` chip cùng
    mệnh giá nghĩa là bấm ô cược ``count`` lần liên tiếp.

    side: 'B' (Banker), 'P' (Player) hoặc 'T' (Tie).
    chips: danh sách mệnh giá hiển thị (đơn vị) cần bấm, từ ``decompose``.
    amount: số tiền cần đặt. Có ``amount`` thì mệnh giá được CHỐT LẠI theo khay đọc tươi
    ngay trong vòng chờ, ``chips`` chỉ còn là gợi ý: khay thường render sau lúc app chốt
    kế hoạch, quyết định theo bản đọc cũ thì lượt bấm bị bỏ trước khi bàn kịp mở khay.

    Trả (ok, note, placed) trong đó placed là dict mệnh giá -> số chip đã bấm được.
    Không ném lỗi ra UI; mọi lỗi gói vào note.
    """
    role = {"B": "Banker", "P": "Player", "T": "Tie"}.get(side)
    placed = {}
    if role is None:
        return False, "Cửa không hợp lệ: %r." % (side,), placed
    chips = [int(c) for c in (chips or []) if int(c) > 0]
    if not chips and not amount:
        return False, "Không có chip nào để bấm.", placed
    ws = None
    try:
        page = session._cdp_page()
        if page is None:
            return False, "Không có tab đang mở để đặt chip.", placed
        import websocket
        ws = websocket.create_connection(
            BR.validated_endpoint(page), suppress_origin=True, timeout=BR.SCRIPT_TIMEOUT,
            redirect_limit=0, http_no_proxy=["127.0.0.1", "localhost", "::1"])
        seq = 0
        contexts = []
        context_frames = {}

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
                        # frameId để đo khoảng lệch iframe khi khung bàn khác origin với
                        # khung gốc (JS không với tới window.frameElement được).
                        context_frames[context["id"]] = (context.get("auxData") or {}).get("frameId")
                if message.get("id") == seq:
                    if "error" in message:
                        raise RuntimeError("cdp_command_failed")
                    return message.get("result", {})

        command("Runtime.enable")
        deadline = time.monotonic() + 6
        while not contexts and time.monotonic() < deadline:
            command("Runtime.evaluate", {"expression": "1", "returnByValue": True})

        targets = None
        matched_context = None
        targets_partial = None
        context_partial = None
        needed_denoms = {int(c) for c in chips}
        wait_deadline = time.monotonic() + TARGETS_WAIT_TIMEOUT
        while targets is None and time.monotonic() < wait_deadline:
            for context_id in list(contexts):
                # Dealer đang mở bài có thể re-render cả màn hình bàn giữa chừng,
                # làm context/iframe tạm đóng và Runtime.evaluate ném lỗi. Một lần
                # đọc hỏng không được phá vỡ cả vòng chờ — bỏ qua context đó, poll
                # tiếp ở vòng sau thay vì để RuntimeError/TimeoutError văng ra
                # ngoài (đó là nguyên nhân lỗi "Không đặt được chip (RuntimeError)"
                # xuất hiện ngay cả khi khay vẫn sắp render lại bình thường).
                try:
                    value = command("Runtime.evaluate", {
                        "expression": TARGETS_JS % json.dumps(role), "contextId": context_id,
                        "returnByValue": True, "silent": True,
                        "timeout": BR.SCRIPT_TIMEOUT * 1000}).get("result", {}).get("value")
                except Exception:
                    if context_id in contexts:
                        contexts.remove(context_id)
                    continue
                if not (isinstance(value, dict) and value.get("spot")):
                    continue
                tray_keys = set()
                for key in (value.get("chips") or {}):
                    try:
                        tray_keys.add(int(key))
                    except (TypeError, ValueError):
                        continue
                # Giữ bản đọc GẦN NHẤT có ô cược: khay re-render liên tục trong lúc chờ,
                # bản đầu tiên có thể chỉ mới lộ một phần mệnh giá.
                targets_partial, context_partial = value, context_id
                # Có amount: chốt mệnh giá theo đúng khay THẬT vừa đọc, đủ và đúng số
                # tiền thì dừng chờ ngay. Đây là chỗ sửa lỗi "khay đã mở mà vẫn không
                # chọn được chip": bản gợi ý của app chụp lúc khay chưa render xong.
                if amount:
                    # Khay TRỐNG thì chưa kết luận gì: decompose nhận list rỗng sẽ tự rơi
                    # về mệnh giá mặc định và báo "ghép được" trên một khay không có chip.
                    live_split = decompose(amount, unit, sorted(tray_keys)) if tray_keys else None
                    if live_split and live_split["exact"] and live_split["chips"]:
                        chips = live_split["chips"]
                        targets, matched_context = value, context_id
                        break
                # Đủ cả ô cược lẫn mọi mệnh giá cần dùng: dừng chờ ngay, không cần
                # đợi hết TARGETS_WAIT_TIMEOUT.
                elif needed_denoms <= tray_keys:
                    targets, matched_context = value, context_id
                    break
            if targets is not None:
                break
            if not contexts:
                # Mọi context đã đóng (ví dụ trang vừa điều hướng/tải lại): dò lại
                # context mặc định mới thay vì poll mãi trên danh sách rỗng.
                probe_deadline = time.monotonic() + 3
                while not contexts and time.monotonic() < probe_deadline:
                    try:
                        command("Runtime.evaluate", {"expression": "1", "returnByValue": True})
                    except Exception:
                        pass
                    if not contexts:
                        time.sleep(0.2)
            time.sleep(TARGETS_WAIT_POLL)
        if targets is None:
            if amount:
                # Hết cửa sổ chờ mà khay thật vẫn không ghép đủ số tiền: KHÔNG bấm tạm
                # phần thiếu (đặt thiếu tiền vẫn là đặt sai). Báo đúng mệnh giá khay đang
                # có để phân biệt "bàn chỉ có chip từ 100" với "khay chưa render xong".
                seen = sorted({int(k) for k in ((targets_partial or {}).get("chips") or {})
                               if str(k).isdigit()})
                if not seen:
                    return False, ("Không tìm thấy ô cược %s hoặc khay chip sau %ds chờ."
                                   % (role, int(TARGETS_WAIT_TIMEOUT))), placed
                return False, ("Khay chip của bàn chỉ có mệnh giá %s — không ghép được %g "
                               "đơn vị cược nên chưa bấm chip. Kiểm tra mức cược tối thiểu "
                               "của bàn này."
                               % (", ".join(str(d) for d in seen), amount)), placed
            # Dealer vẫn đang mở bài/gom chip: khay chưa render đủ mọi mệnh giá
            # cần dùng dù ô cược đã có. Dùng tạm bản đọc gần nhất (có thể thiếu
            # vài mệnh giá) để vẫn bấm được những gì khay đang có, thay vì bỏ
            # cuộc hoàn toàn — phần còn thiếu sẽ báo rõ trong "placed" thiếu hụt.
            if targets_partial is not None:
                targets, matched_context = targets_partial, context_partial
            else:
                return False, "Không tìm thấy ô cược %s hoặc khay chip." % role, placed

        def evaluate(expression):
            """Đọc một giá trị DOM trong đúng khung bàn; lỗi giữa chừng trả None."""
            try:
                return command("Runtime.evaluate", {
                    "expression": expression, "contextId": matched_context,
                    "returnByValue": True, "silent": True,
                    "timeout": BR.SCRIPT_TIMEOUT * 1000}).get("result", {}).get("value")
            except Exception:
                return None

        def read_selected_chip():
            """Một lần đọc mệnh giá đang CHỌN; trả None nếu DOM chưa kịp trả giá trị."""
            value = evaluate(SELECTED_CHIP_JS)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def read_targets():
            value = evaluate(TARGETS_JS % json.dumps(role))
            return value if isinstance(value, dict) else None

        def read_spot_amount():
            value = evaluate(SPOT_AMOUNT_JS % json.dumps(role))
            if isinstance(value, dict) and isinstance(value.get("amount"), (int, float)):
                return float(value["amount"])
            return None

        def tray_points(snapshot):
            # JSON trả khóa object dưới dạng CHUỖI ("20", "100"); phải đổi về số nguyên,
            # nếu không mọi lần tra cứu theo mệnh giá (số) đều trượt và app báo
            # "chưa có chip nào" dù khay thật có đủ.
            points = {}
            for key, value in ((snapshot or {}).get("chips") or {}).items():
                try:
                    points[int(key)] = value
                except (TypeError, ValueError):
                    continue
            return points

        # Toạ độ đo trong khung bàn phải đổi sang khung gốc của tab trước khi bấm chuột.
        offset = targets.get("frame_offset")
        if offset is None:
            offset = frame_offset_via_cdp(command, context_frames.get(matched_context))
        if offset is None:
            return False, ("Không đo được vị trí khung bàn trong tab nên chưa bấm chip — "
                           "bấm lúc này sẽ lệch ô cược. Thử lại khi bàn đã tải xong."), placed

        tray_chips = tray_points(targets)
        spot = targets["spot"]
        # Mốc chip trên ô cược trước khi bấm: chỉ chấp nhận "đã đặt" khi tổng chip tăng
        # đúng số tiền vừa bấm, không chấp nhận theo số cú bấm đã gửi.
        start_amount = None
        amount_deadline = time.monotonic() + CHIP_CONFIRM_TIMEOUT
        while start_amount is None and time.monotonic() < amount_deadline:
            start_amount = read_spot_amount()
            if start_amount is None:
                time.sleep(CHIP_CONFIRM_POLL)
        if start_amount is None:
            return False, "Không đọc được ô cược %s để đối chiếu chip nên chưa bấm." % role, placed
        try:
            command("Page.bringToFront")
        except Exception:
            pass

        # Cài con trỏ xanh hiển thị ngay trên màn hình bàn, giống lớp overlay đã cài
        # ở sảnh (lobby_stream._click_at). Thiếu bước này thì chuột người thật vẫn
        # gửi đúng sự kiện CDP (đặt chip vẫn chạy) nhưng người dùng không nhìn thấy
        # con trỏ ảo di chuyển trên bàn, dễ hiểu lầm là HumanMouse không hoạt động.
        if bool(human_mouse):
            try:
                HM.install_cursor_overlay(lambda method, params=None: command(method, params))
            except Exception:
                pass

        mouse = HM.HumanMouse(lambda method, params: command(method, params),
                              enabled=bool(human_mouse))

        # Gộp chip cùng mệnh giá để bấm ô cược đúng số lần.
        counts = {}
        for denom in chips:
            counts[denom] = counts.get(denom, 0) + 1

        for denom in sorted(counts, reverse=True):
            # Đọc lại vị trí ngay trước khi bấm: khay re-render và đổi cỡ chip đang chọn,
            # toạ độ chụp từ đầu lượt có thể đã lệch vài chục pixel.
            fresh = read_targets()
            if fresh is not None:
                tray_chips = tray_points(fresh) or tray_chips
                if fresh.get("spot"):
                    spot = fresh["spot"]
                if fresh.get("frame_offset") is not None:
                    offset = fresh["frame_offset"]
            pos = page_point(tray_chips.get(denom), offset)
            if pos is None:
                continue
            bet_point = page_point(spot, offset)
            if bet_point is None:
                return False, "Không xác định được ô cược %s để bấm chip." % role, placed
            mouse.click(pos["x"], pos["y"])
            time.sleep(click_delay)
            # Xác nhận đúng mệnh giá đã được chọn trước khi bấm tiền thật vào ô cược,
            # bằng nhiều lần đọc trong một cửa sổ ngắn (DOM đổi chậm hơn một nhịp click).
            ok, confirmed = confirm_selected_chip(denom, read_selected_chip)
            if not ok:
                # Bấm khay chip chỉ đổi mệnh giá, KHÔNG đặt tiền, nên thử lại đúng một
                # lần với vị trí vừa đọc lại là an toàn. Vẫn sai thì dừng, không đoán
                # toạ độ khác để tránh đặt nhầm cửa/nhầm tiền.
                again = read_targets()
                pos = page_point((tray_points(again) or tray_chips).get(denom), offset)
                if pos is not None:
                    mouse.click(pos["x"], pos["y"])
                    time.sleep(click_delay)
                    ok, confirmed = confirm_selected_chip(denom, read_selected_chip)
            if not ok:
                return False, ("Bấm chọn chip %d nhưng khay đang chọn %s — dừng lại để "
                               "không đặt nhầm tiền. Kiểm tra lại vị trí chip trên bàn."
                               % (denom, "?" if confirmed is None else confirmed)), placed
            for _ in range(counts[denom]):
                mouse.click(bet_point["x"], bet_point["y"])
                time.sleep(click_delay)
            placed[denom] = counts[denom]

        if not placed:
            return False, "Không bấm được chip nào (mệnh giá cần không có trên khay).", placed
        total_units = sum(k * v for k, v in placed.items())
        detail = ", ".join("%d×%d" % (v, k) for k, v in sorted(placed.items()))
        ok_amount, actual = confirm_spot_amount(start_amount, total_units, read_spot_amount)
        gained = None if actual is None else actual - start_amount
        if not ok_amount:
            return False, ("Đã bấm %d đơn vị chip vào %s nhưng ô cược chỉ nhận %s đơn vị — "
                           "chip chưa lên bàn đúng số tiền, tay này không được tính là đã đặt."
                           % (total_units, role, "?" if gained is None else int(gained))), placed
        return True, ("Đã đặt %d đơn vị chip vào %s (%s) · ô cược xác nhận +%d." % (
            total_units, role, detail, int(gained))), placed
    except Exception as exc:
        return False, "Không đặt được chip (%s)." % type(exc).__name__, placed
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
