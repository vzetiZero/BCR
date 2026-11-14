# -*- coding: utf-8 -*-
"""Adapter đọc bàn LONG-LONG ("DragonDragon" / game ``twocoins``) qua Chrome CDP.

Bàn này nằm trong iframe của portal nên phải đọc đúng execution context, không đọc
được từ frame ngoài. Những gì đọc được (đã dò trên bàn thật):

    Ô cược (``aria-label`` xác nhận):
        JNS_B  -> "Bet on GOLD"  = 2 VÀNG   (2.90)  -> bộ VV
        JNS_A  -> "Bet on RED"   = 2 ĐỎ     (2.90)  -> bộ DD
        JNS_C  -> "Bet on MIX"   = 1 vàng 1 đỏ (0.95) -> bộ VD/DV  <- ô của chiến thuật
    Số dư thật:     [data-role="balance-label-value"]   ("0 VN2", "1.234,56 VN2")
    Tổng đặt cược:  [data-role="total-bet-label-value"]
    Trạng thái ván: [data-role="status-text"]           ("ĐẶT CƯỢC CỦA BẠN", …)
    Kết quả ván:    [data-role="result"], thể hiện bằng class/HTML bên trong
    Tổng theo ô:    text của từng [data-role="JNS_x"] -> "C 0.95:1 14 VN2 3.114,69 76 %"

VN2 là đơn vị của sàn: 1 VN2 = 1.000đ, trùng đúng "đơn vị sảnh" mà thang cược đang dùng
(40 đv = 40.000đ = 40 VN2).
"""

import json
import time

from bcr.adapters import browser as BR

# Ký tự vô hình sàn chèn vào số tiền (isolate marks, NBSP) phải bỏ trước khi parse.
INVISIBLE = dict.fromkeys(map(ord, "\u2066\u2067\u2068\u2069\u200e\u200f\u00a0\u202f"), None)

# Mã ô trên DOM -> (ký hiệu trên bàn, màu, bộ kết quả tương ứng)
DOORS = {
    "JNS_B": ("B", "GOLD", "VV"),
    "JNS_A": ("A", "RED", "DD"),
    "JNS_C": ("C", "MIX", "VD"),
}
DOOR_OF_RAW = {"VV": "JNS_B", "DD": "JNS_A", "VD": "JNS_C", "DV": "JNS_C"}
STRATEGY_DOOR = "JNS_C"

READ_JS = r"""(() => {
  const txt = (el, n) => ((el && (el.innerText || el.textContent) || '')
    .replace(/\s+/g, ' ').trim().slice(0, n || 160));
  const q = (role) => document.querySelector('[data-role="' + role + '"]');
  const spots = {};
  ['JNS_A', 'JNS_B', 'JNS_C'].forEach((role) => {
    const el = q(role);
    spots[role] = el ? {aria: el.getAttribute('aria-label'), text: txt(el, 90),
                        cls: String(el.className || '')} : null;
  });
  const holder = {};
  ['JNS_A', 'JNS_B', 'JNS_C'].forEach((role) => {
    const box = q('betting-spot-' + role + '-chip-container');
    holder[role] = box ? box.querySelectorAll('[data-role="chip"]').length : 0;
  });
  const result = q('result');
  return {
    ok: !!q('twocoins-bet-panel') || !!spots.JNS_C,
    balance: txt(q('balance-label-value'), 40),
    balance_title: txt(q('balance-label-title'), 40),
    total_bet: txt(q('total-bet-label-value'), 40),
    status: txt(q('status-text'), 60),
    result_html: result ? (result.outerHTML || '').replace(/\s+/g, ' ').slice(0, 400) : '',
    result_cls: result ? String(result.className || '') : '',
    spots: spots,
    my_chips: holder,
    url: location.href.slice(0, 200),
  };
})()"""


def clean_text(value):
    """Bỏ ký tự vô hình, gộp khoảng trắng."""
    text = str(value or "").translate(INVISIBLE)
    return " ".join(text.split())


def parse_vn2(text):
    """Đọc số VN2 kiểu Việt Nam: '1.234,56 VN2' -> 1234.56; '0 VN2' -> 0.0.

    Trả None khi không có số (không đoán). Chấp nhận cả dạng không có phần thập phân,
    dạng dùng dấu phẩy thập phân và dạng dùng dấu chấm thập phân.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return None
    pieces = []
    for token in cleaned.replace("VN2", " ").replace("VN", " ").split():
        digits = "".join(ch for ch in token if ch.isdigit() or ch in ".,")
        if any(ch.isdigit() for ch in digits):
            pieces.append(digits)
    if not pieces:
        return None
    raw = pieces[0]
    if "," in raw and "." in raw:
        # Việt Nam: '.' là phân cách nghìn, ',' là thập phân.
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        last = raw.split(",")[-1]
        raw = raw.replace(",", ".") if 1 <= len(last) <= 2 else raw.replace(",", "")
    elif "." in raw:
        last = raw.split(".")[-1]
        # '1.234' / '50.000' là phân cách nghìn; '1.5' / '12.34' là thập phân.
        if raw.count(".") > 1 or len(last) == 3:
            raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_spot(text):
    """Đọc dòng tổng của một ô: 'C 0.95:1 14 VN2 3.114,69 76 %' .

    Trả dict {"code", "rate", "chips", "amount", "percent"}; phần nào không đọc được
    thì để None.
    """
    cleaned = clean_text(text)
    out = {"code": None, "rate": None, "chips": None, "amount": None, "percent": None}
    if not cleaned:
        return out
    tokens = cleaned.split()
    if tokens and tokens[0].upper() in ("A", "B", "C"):
        out["code"] = tokens[0].upper()
    for index, token in enumerate(tokens):
        if token.endswith(":1"):
            try:
                out["rate"] = float(token[:-2])
            except ValueError:
                pass
        elif token == "VN2" and index > 0:
            try:
                out["chips"] = int(tokens[index - 1])
            except ValueError:
                pass
            amount = parse_vn2(" ".join(tokens[index + 1:index + 3]))
            out["amount"] = amount
        elif token == "%" and index > 0:
            try:
                out["percent"] = int(tokens[index - 1])
            except ValueError:
                pass
    return out


def detect_winner(spots):
    """Ô vừa thắng, suy ra từ class hiệu ứng của sàn.

    Trên bàn thật, cả ba ô luôn có 2 class nền; ô THẮNG được gắn thêm một class hiệu
    ứng (dùng chung cho cả ba ô, ví dụ ``InFqhC``) trong vài giây. Vì vậy không bám vào
    tên class cụ thể (hash đổi mỗi lần sàn deploy) mà so SỐ class: ô nào nhiều hơn hẳn
    hai ô còn lại thì đó là ô thắng. Không rõ thì trả None.
    """
    counts = {}
    for role in DOORS:
        detail = (spots or {}).get(role) or {}
        counts[role] = len(set(str(detail.get("cls") or "").split()))
    if not counts or not any(counts.values()):
        return None
    top = max(counts.values())
    leaders = [role for role, count in counts.items() if count == top]
    others = [count for role, count in counts.items() if role not in leaders]
    if len(leaders) != 1 or not others or top <= max(others):
        return None
    return leaders[0]


def read_state_text(state):
    """Chuỗi mô tả trạng thái bàn để hiện trên UI (thuần, không cần tkinter)."""
    if not isinstance(state, dict) or not state.get("ok"):
        return "Chưa đọc được bàn LONG-LONG."
    status = clean_text(state.get("status")) or "—"
    winner = state.get("winner")
    if winner in DOORS:
        code, colour, raw = DOORS[winner]
        return "%s · ván vừa ra ô %s (%s)" % (status, code, colour)
    return "%s · chưa có kết quả ván mới" % status


PLACE_READ_JS = r"""(() => {
  const DOOR = "__DOOR__";
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
  };
  const txt = (el, n) => ((el && (el.innerText || el.textContent) || '')
    .replace(/\s+/g, ' ').trim().slice(0, n || 40));
  const chips = {};
  document.querySelectorAll('[data-role="chip"][data-value]').forEach((el) => {
    const value = el.getAttribute('data-value');
    if (value && !(value in chips)) chips[value] = box(el);
  });
  const door = document.querySelector('[data-role="' + DOOR + '"]');
  const holder = document.querySelector('[data-role="betting-spot-' + DOOR + '-chip-container"]');
  const undo = document.querySelector('[data-role="undo-button"]');
  const cls = (el) => String((el && el.className) || '').split(/\s+/).filter(Boolean);
  const counts = ['JNS_A', 'JNS_B', 'JNS_C'].map((role) => {
    const el = document.querySelector('[data-role="' + role + '"]');
    return cls(el).length;
  });
  const top = Math.max.apply(null, counts);
  return {
    door: door ? box(door) : null,
    chips: chips,
    my_chips: holder ? holder.querySelectorAll('[data-role="chip"]').length : 0,
    undo: undo ? box(undo) : null,
    total_bet: txt(document.querySelector('[data-role="total-bet-label-value"]'), 40),
    status: txt(document.querySelector('[data-role="status-text"]'), 60),
    winner_present: top > 0 && counts.filter((count) => count === top).length === 1,
  };
})()"""


def decompose_amount(amount, denominations):
    """Chia số VN2 thành các mệnh giá chip đang có trên khay (ít chip nhất).

    Tham lam từ mệnh giá lớn xuống (khay EVO có 10/20/40/100/500/2000, mọi mệnh giá đều
    là bội của 10). Trả danh sách mệnh giá cần bấm; nếu không chia khít được thì trả
    None để KHÔNG đặt thiếu tiền.
    """
    try:
        target = int(round(float(amount)))
    except (TypeError, ValueError):
        return None
    values = sorted({int(v) for v in denominations if isinstance(v, (int, float)) and v > 0},
                    reverse=True)
    if target <= 0 or not values:
        return None
    chips = []
    rest = target
    for value in values:
        while rest >= value:
            chips.append(value)
            rest -= value
    if rest:
        return None
    return chips


def clickable(box, limit=None):
    """Hộp có bấm được không (đủ lớn, không âm, nằm trong khung nhìn)."""
    if not isinstance(box, dict):
        return False
    if box.get("w", 0) < 4 or box.get("h", 0) < 4:
        return False
    if box.get("x", 0) < 0 or box.get("y", 0) < 0:
        return False
    if limit and (box["x"] > limit[0] or box["y"] > limit[1]):
        return False
    return True


class LongLongTable:
    """Đọc trạng thái bàn LONG-LONG (chỉ đọc, không bấm) qua CDP 9222."""

    def __init__(self, session=None):
        self.session = session or BR.BrowserSession()
        self._ws_url = None
        self._context = None
        self._ws = None
        self.last_error = ""

    # ---------------------------------------------------------------- kết nối
    def _targets(self):
        try:
            return BR.BrowserSession._cdp_json("list")
        except Exception as exc:
            self.last_error = "Không đọc được CDP 9222 (%s)." % type(exc).__name__
            return []

    def _open(self, ws_url):
        import websocket

        return websocket.create_connection(ws_url, suppress_origin=True,
                                           timeout=BR.SCRIPT_TIMEOUT, redirect_limit=0,
                                           http_no_proxy=["127.0.0.1", "localhost", "::1"])

    def _send(self, ws, method, params=None, request_id=1):
        ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))

    def _evaluate(self, ws, expression, context_id=None, timeout=6.0):
        self._send(ws, "Runtime.evaluate",
                   {"expression": expression, "returnByValue": True, "awaitPromise": False,
                    **({"contextId": context_id} if context_id else {})}, request_id=7)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reply = json.loads(ws.recv())
            except Exception:
                return None
            if reply.get("id") != 7:
                continue
            return (reply.get("result") or {}).get("result", {}).get("value")
        return None

    def _discover(self):
        """Tìm context có ô cược LONG-LONG; cache lại để lần sau khỏi dò."""
        if self._ws_url and self._context is not None:
            return self._ws_url, self._context
        for item in self._targets():
            if item.get("type") not in ("page", "iframe"):
                continue
            url = item.get("url") or ""
            if "evo" not in url:
                continue
            ws_url = item.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            ws = None
            try:
                ws = self._open(ws_url)
                self._send(ws, "Runtime.enable", request_id=0)
                contexts = []
                deadline = time.monotonic() + 1.5
                ws.settimeout(0.4)
                while time.monotonic() < deadline:
                    try:
                        reply = json.loads(ws.recv())
                    except Exception:
                        continue
                    if reply.get("method") == "Runtime.executionContextCreated":
                        context = (reply.get("params") or {}).get("context") or {}
                        contexts.append(context.get("id"))
                ws.settimeout(BR.SCRIPT_TIMEOUT)
                for context_id in sorted({cid for cid in contexts if cid}):
                    value = self._evaluate(ws, READ_JS, context_id)
                    if isinstance(value, dict) and value.get("ok"):
                        self._ws_url, self._context = ws_url, context_id
                        return ws_url, context_id
            except Exception as exc:
                self.last_error = "Không đọc được context bàn (%s)." % type(exc).__name__
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        self.last_error = "Chưa mở bàn LONG-LONG trong Chrome."
        return None, None

    # ------------------------------------------------------------------ đọc
    def read(self):
        """Đọc trạng thái bàn một lần; trả dict (ok=False nếu không đọc được)."""
        ws_url, context_id = self._discover()
        if ws_url is None:
            return {"ok": False, "error": self.last_error or "Chưa mở bàn LONG-LONG."}
        ws = None
        try:
            ws = self._open(ws_url)
            self._send(ws, "Runtime.enable", request_id=0)
            value = self._evaluate(ws, READ_JS, context_id)
            if not isinstance(value, dict) or not value.get("ok"):
                # Bàn đổi tab/context: xoá cache để lần sau dò lại.
                self._ws_url, self._context = None, None
                return {"ok": False, "error": "Context bàn đã đổi; sẽ dò lại."}
            state = dict(value)
            state["balance_vn2"] = parse_vn2(value.get("balance"))
            state["total_bet_vn2"] = parse_vn2(value.get("total_bet"))
            state["winner"] = detect_winner(value.get("spots"))
            state["winner_raw"] = (DOORS.get(state["winner"]) or (None, None, None))[2]
            state["doors"] = {}
            for role, detail in (value.get("spots") or {}).items():
                if not isinstance(detail, dict):
                    continue
                parsed = parse_spot(detail.get("text"))
                parsed["aria"] = detail.get("aria")
                parsed["cls"] = detail.get("cls")
                parsed["raw"] = DOORS.get(role, (None, None, None))[2]
                parsed["my_chips"] = (value.get("my_chips") or {}).get(role)
                state["doors"][role] = parsed
            state["read_at"] = time.time()
            self.last_error = ""
            return state
        except Exception as exc:
            self._ws_url, self._context = None, None
            closed = BR.cdp_port_closed(exc)
            self.last_error = (BR.CDP_CLOSED_NOTE if closed
                               else "Không đọc được bàn (%s)." % type(exc).__name__)
            return {"ok": False, "error": self.last_error}
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def read_balance(self):
        """Chỉ lấy số dư VN2 (None nếu không đọc được)."""
        state = self.read()
        return state.get("balance_vn2") if state.get("ok") else None

    # --------------------------------------------------------------- đặt chip
    @staticmethod
    def _place_js(door=STRATEGY_DOOR):
        return PLACE_READ_JS.replace("__DOOR__", door)

    def _click(self, ws, box, pause=0.14):
        """Bấm chuột thật qua CDP tại tâm phần tử (không dùng el.click())."""
        cx = box["x"] + box["w"] / 2.0
        cy = box["y"] + box["h"] / 2.0
        for kind in ("mousePressed", "mouseReleased"):
            self._send(ws, "Input.dispatchMouseEvent",
                       {"type": kind, "x": cx, "y": cy, "button": "left",
                        "buttons": 1 if kind == "mousePressed" else 0, "clickCount": 1},
                       request_id=11)
        time.sleep(pause)

    def _undo(self, ws, times, state):
        """Bấm HOÀN TÁC để huỷ số chip vừa đặt (dùng khi xác minh lệch)."""
        box = (state or {}).get("undo")
        for _ in range(max(0, int(times))):
            if not clickable(box):
                break
            self._click(ws, box)

    def place(self, amount, door=STRATEGY_DOOR, max_chips=40):
        """Đặt ``amount`` VN2 vào ô ``door`` bằng chip trên khay.

        Quy trình an toàn: chia tiền thành mệnh giá chip đang có -> bấm chip -> bấm ô ->
        đọc lại TỔNG ĐẶT CƯỢC của chính mình và so với số tiền định đặt; lệch là bấm
        HOÀN TÁC toàn bộ. Trả dict {"ok", "placed", "total", "error"}.
        """
        ws_url, context_id = self._discover()
        if ws_url is None:
            return {"ok": False, "error": self.last_error or "Chưa mở bàn LONG-LONG."}
        js = self._place_js(door)
        ws = None
        placed = []
        try:
            ws = self._open(ws_url)
            self._send(ws, "Runtime.enable", request_id=0)
            state = self._evaluate(ws, js, context_id)
            if not isinstance(state, dict) or not state.get("door"):
                return {"ok": False, "error": "Chưa thấy ô cược trên bàn."}
            if state.get("winner_present"):
                return {"ok": False, "error": "Ván đã có kết quả; chờ ván mới."}
            before = parse_vn2(state.get("total_bet")) or 0.0
            denominations = [int(value) for value in (state.get("chips") or {})
                             if str(value).isdigit()]
            chips = decompose_amount(amount, denominations)
            if not chips:
                return {"ok": False,
                        "error": "Không chia được %s VN2 theo mệnh giá %s." % (amount, denominations)}
            if len(chips) > max_chips:
                return {"ok": False,
                        "error": "Cần %d chip (> %d) — dừng cho an toàn." % (len(chips), max_chips)}
            door_box = state.get("door")
            for value in chips:
                chip_box = (state.get("chips") or {}).get(str(value))
                if not clickable(chip_box) or not clickable(door_box):
                    raise RuntimeError("Không bấm được chip %s/ô cược." % value)
                self._click(ws, chip_box)
                self._click(ws, door_box)
                placed.append(value)
            after_state = self._evaluate(ws, js, context_id) or {}
            after = parse_vn2(after_state.get("total_bet")) or 0.0
            delta = after - before
            if abs(delta - float(amount)) < 0.01:
                return {"ok": True, "placed": chips, "total": after, "delta": delta}
            self._undo(ws, len(placed), after_state)
            return {"ok": False, "placed": chips, "delta": delta,
                    "error": "Đặt lệch: cần %s VN2, bàn ghi %s — đã hoàn tác." % (amount, delta)}
        except Exception as exc:
            self._ws_url, self._context = None, None
            return {"ok": False, "placed": placed,
                    "error": "Lỗi khi đặt chip (%s)." % type(exc).__name__}
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def close(self):
        self._ws_url = self._context = self._ws = None
        self.last_error = ""
