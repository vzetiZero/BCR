# -*- coding: utf-8 -*-
"""Quét sảnh Baccarat qua luồng WebSocket công khai của chính trang.

Chỉ đọc các message lobby.histories / lobby.historyUpdated (mã bàn + lịch sử cầu),
cuộn danh sách để sảnh tự gửi dữ liệu cho các bàn ngoài màn hình, rồi khôi phục vị trí cuộn.
Không gọi API nội bộ, không đọc cookie/token, không bấm nút cược.
"""

import json
import re
import time

import browser as BR

HISTORY_TYPES = ("lobby.histories", "lobby.historyUpdated")
SIDE_BY_COLOR = {"R": "B", "B": "P"}
SPECIAL_IDS = frozenset((
    "LightningBac0001", "XXXtremeLB000001", "NoCommBac0000001", "gwbaccarat000001",
    "Empgwbaccarat001", "PTBaccarat000001", "InsuranceBac0001",
))
MAX_RESULTS = 400
MAX_TIES = 50
SCROLL_STEP = 500
SCROLL_SETTLE = 1.2
MAX_SCROLL_STEPS = 40


def decode_results(results):
    """Trả về (letters, read_error); không suy đoán khi định dạng lạ."""
    if not isinstance(results, list):
        return "", "Không nhận được lịch sử bàn qua WebSocket."
    letters = ""
    for item in results:
        side = SIDE_BY_COLOR.get(item.get("c")) if isinstance(item, dict) else None
        ties = (item.get("ties") or 0) if isinstance(item, dict) else 0
        pos = item.get("pos") if isinstance(item, dict) else None
        if (not side or not isinstance(ties, int) or isinstance(ties, bool)
                or not 0 <= ties <= MAX_TIES or not isinstance(pos, list) or len(pos) != 2
                or not all(isinstance(p, int) and not isinstance(p, bool) and p >= 0 for p in pos)):
            return "", "Dữ liệu lịch sử không đúng định dạng; không suy đoán kết quả."
        letters += side + "T" * ties
        if len(letters) > MAX_RESULTS:
            return "", "Lịch sử vượt giới hạn %d tay." % MAX_RESULTS
    if results:
        first = results[0]
        head = first.get("pos") if isinstance(first, dict) else None
        if isinstance(head, list) and len(head) == 2 and head != [0, 0]:
            return letters, "Big Road bị cắt đầu; không đủ lịch sử để chọn bàn."
        if isinstance(first, dict) and first.get("ties"):
            return letters, "Ô đầu có hòa: Big Road không cho biết hòa trước hay sau tay đầu."
    return letters, ""


def is_special(table):
    details = (table or {}).get("gameDetails") or {}
    return (bool(((details.get("gameConfig") or {}).get("lightning")))
            or (table or {}).get("id") in SPECIAL_IDS
            or details.get("gameType") not in (None, "baccarat"))


TILES_JS = """(() => {
    const pick = () => {
        let n = document.querySelector('[data-role="table-tile"]');
        while (n) {
            const st = getComputedStyle(n);
            if (/(auto|scroll)/.test(st.overflowY) && n.scrollHeight > n.clientHeight + 20) return n;
            n = n.parentElement;
        }
        return document.scrollingElement;
    };
    const tableOf = (tile) => {
        const key = Object.keys(tile).find((n) => n.startsWith('__reactFiber$'));
        let f = key ? tile[key] : null;
        while (f && !(f.memoizedProps && f.memoizedProps.table && f.memoizedProps.table.gameDetails)) f = f.return;
        return f || null;
    };
    const historyOf = (fiber) => {
        const stack = [fiber];
        for (let seen = 0; stack.length && seen < 4000; seen++) {
            const f = stack.pop();
            const p = f.memoizedProps || {};
            if (Array.isArray(p.history) && p.type === 'BaccaratHistoryRoadTypeBig') return p.history;
            if (f.child) stack.push(f.child);
            if (f.sibling) stack.push(f.sibling);
        }
        return null;
    };
    return {tiles: [...document.querySelectorAll('[data-role="table-tile"]')].map((tile) => {
        const fiber = tableOf(tile);
        const table = (fiber && fiber.memoizedProps.table) || {};
        return {id: typeof table.id === 'string' ? table.id : '', title: typeof table.title === 'string' ? table.title : '',
                game_type: (table.gameDetails || {}).gameType || '',
                lightning: !!(table.gameDetails || {}).gameConfig?.lightning,
                road: fiber ? historyOf(fiber) : null,
                top: Math.round(tile.getBoundingClientRect().top)};
    }).filter((r) => r.id), scroll: (() => { const sc = pick(); return {top: sc.scrollTop, max: sc.scrollHeight, client: sc.clientHeight}; })()};
})()"""

SCROLL_JS = """(() => {
    let n = document.querySelector('[data-role="table-tile"]');
    while (n) {
        const st = getComputedStyle(n);
        if (/(auto|scroll)/.test(st.overflowY) && n.scrollHeight > n.clientHeight + 20) {
            n.scrollTop = %d;
            return Math.round(n.scrollTop);
        }
        n = n.parentElement;
    }
    return -1;
})()"""

TILE_BOX_JS = """(() => {
    const id = %s;
    for (const tile of document.querySelectorAll('[data-role="table-tile"]')) {
        if (tile.getAttribute('data-open') !== 'true') continue;
        const key = Object.keys(tile).find((n) => n.startsWith('__reactFiber$'));
        let f = key ? tile[key] : null;
        while (f && !(f.memoizedProps && f.memoizedProps.table && f.memoizedProps.table.gameDetails)) f = f.return;
        if (!f || f.memoizedProps.table.id !== id) continue;
        tile.scrollIntoView({block: 'center'});
        const rect = tile.getBoundingClientRect();
        if (rect.width < 20 || rect.height < 20 || rect.top < 0 || rect.bottom > innerHeight
                || rect.left < 0 || rect.right > innerWidth) return {reason: 'tile_offscreen'};
        return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2,
                title: f.memoizedProps.table.title || id};
    }
    return {reason: 'tile_not_found'};
})()"""


class LobbyStream:
    """Đọc luồng message sảnh qua CDP chỉ đọc trên Chrome đang mở."""

    def __init__(self, session=None):
        self.session = session or BR.BrowserSession()
        self.note = ""
        self.last_error = ""
        self.diag = {}
        self.frames = 0
        self.contexts = []
        self.histories = {}
        self._seq = 0

    def _reset(self):
        self.contexts, self.histories, self._seq = [], {}, 0
        self.frames = 0
        self.diag = {}
        self.last_error = ""

    def _evaluate(self, ws, expression, context_id):
        return self._command(ws, "Runtime.evaluate", {
            "expression": expression, "contextId": context_id,
            "returnByValue": True, "silent": True, "timeout": BR.SCRIPT_TIMEOUT * 1000})

    def _command(self, ws, method, params=None):
        self._seq = getattr(self, "_seq", 0) + 1
        ws.send(json.dumps({"id": self._seq, "method": method, "params": params or {}}))
        deadline = time.monotonic() + BR.SCRIPT_TIMEOUT * 3
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("cdp_timeout")
            ws.settimeout(remaining)
            message = json.loads(ws.recv())
            if message.get("method") == "Runtime.executionContextCreated":
                context = message.get("params", {}).get("context", {})
                if (context.get("auxData") or {}).get("isDefault"):
                    self.contexts.append(context["id"])
            if message.get("method") == "Network.webSocketFrameReceived":
                self._absorb(message.get("params", {}).get("response", {}))
            if message.get("id") == self._seq:
                if "error" in message:
                    raise RuntimeError("cdp_command_failed")
                return message.get("result", {})

    def _absorb(self, frame):
        if not isinstance(frame, dict) or frame.get("opcode") != 1:
            return
        try:
            message = json.loads(frame.get("payloadData", ""))
        except (ValueError, TypeError):
            return
        if not isinstance(message, dict) or message.get("type") not in HISTORY_TYPES:
            return
        args = message.get("args")
        if not isinstance(args, dict):
            return
        rows = args.get("histories") if message["type"] == "lobby.histories" else args
        if not isinstance(rows, dict):
            return
        for table_id, row in rows.items():
            results = row.get("results") if isinstance(row, dict) else None
            if (not isinstance(table_id, str) or not BR.valid_table_id(table_id)
                    or not isinstance(results, list)):
                continue
            current = self.histories.get(table_id)
            if current is None or len(results) >= len(current["results"]):
                self.histories[table_id] = {"results": results, "source": message["type"],
                                            "ts": time.time(), "samples": 1}
            elif self.histories[table_id]["results"] == results:
                self.histories[table_id]["samples"] += 1
            self.histories[table_id]["messages"] = self.histories[table_id].get("messages", 0) + 1

    def _value(self, ws, expression, context_id):
        return self._evaluate(ws, expression, context_id).get("result", {}).get("value")

    def _lobby_context(self, ws):
        for context_id in list(self.contexts):
            result = self._evaluate(ws, TILES_JS, context_id)
            value = result.get("result", {}).get("value")
            if isinstance(value, dict) and value.get("tiles"):
                self.frames += 1
                return context_id, value
        return None, None

    def sweep(self, max_seconds=75):
        """Cuộn hết danh sách để sảnh gửi lịch sử mọi bàn, rồi trả về danh sách bàn."""
        self._reset()
        self.note = ""
        deadline = time.monotonic() + max_seconds
        ws = None
        try:
            page = self.session._cdp_page()
            if page is None:
                self.last_error = "Không có tab sảnh Baccarat trong Chrome CDP 9222."
                return []
            import websocket
            ws = websocket.create_connection(BR.validated_endpoint(page), suppress_origin=True,
                                             timeout=BR.SCRIPT_TIMEOUT, redirect_limit=0,
                                             http_no_proxy=["127.0.0.1", "localhost", "::1"])
            self._command(ws, "Network.enable")
            self._command(ws, "Runtime.enable")
            context_id, snapshot = self._lobby_context(ws)
            if context_id is None:
                self.last_error = "Sảnh chưa hiển thị danh sách bàn; hãy mở lại trang Baccarat."
                return []
            start_top = snapshot["scroll"]["top"]
            steps = 0
            seen = set()
            stagnant = 0
            position = 0
            while time.monotonic() < deadline and steps < MAX_SCROLL_STEPS:
                self._evaluate(ws, SCROLL_JS % position, context_id)
                time.sleep(SCROLL_SETTLE)
                result = self._evaluate(ws, TILES_JS, context_id)
                snapshot = result.get("result", {}).get("value") or snapshot
                ids = {tile["id"] for tile in snapshot["tiles"]}
                bottom = max(snapshot["scroll"]["max"] - snapshot["scroll"]["client"], 0)
                at_bottom = position >= bottom
                stagnant = 0 if (ids - seen or not at_bottom) else stagnant + 1
                seen |= ids
                if stagnant >= 2:
                    break
                position = min(position + SCROLL_STEP, bottom)
                steps += 1
            self._evaluate(ws, SCROLL_JS % start_top, context_id)
            tiles = {tile["id"]: tile for tile in snapshot["tiles"]}
            rows = []
            for table_id, entry in self.histories.items():
                letters, read_error = decode_results(entry["results"])
                tile = tiles.get(table_id)
                road = decode_results(tile["road"])[0] if tile and tile.get("road") is not None else None
                special = is_special({"id": table_id, "gameDetails": {"gameType": (tile or {}).get("game_type") or "baccarat",
                                                                      "gameConfig": {"lightning": (tile or {}).get("lightning")}}})
                if special and not read_error:
                    read_error = "Bàn có luật trả thưởng đặc biệt; chỉ xem cầu, không giả lập theo bàn chuẩn."
                rows.append({
                    "table_id": table_id,
                    "label": (tile or {}).get("title") or table_id,
                    "letters": letters,
                    "game_type": (tile or {}).get("game_type") or "",
                    "read_error": read_error,
                    "source": "evo-ws-" + entry["source"].split(".")[-1],
                    "ws_hands": len(entry["results"]),
                    "road_match": bool(road) and road == letters,
                    "observed_at": entry["ts"],
                    "messages": entry.get("messages", 1),
                })
            rows.sort(key=lambda r: r["label"].casefold())
            self.diag = {"tiles_in_dom": len(tiles), "ws_tables": len(rows),
                         "scroll_steps": steps, "matched_road": sum(1 for r in rows if r["road_match"])}
            self.note = "Quét %d bước cuộn · %d bàn qua WebSocket · %d bàn khớp cầu đang vẽ." % (
                steps, len(rows), self.diag["matched_road"])
            return rows
        except Exception as exc:
            # Không đưa chi tiết CDP/socket thô vào UI.
            self.last_error = "Không quét được luồng sảnh (%s)." % type(exc).__name__
            return []
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def open_table(self, table_id, wait_seconds=25):
        """Mở bàn bằng pointer event thật trên đúng ô data-open của sảnh; không chạm khu vực cược."""
        if not BR.valid_table_id(table_id):
            return False, "Mã bàn không hợp lệ."
        ws = None
        self._reset()
        try:
            page = self.session._cdp_page()
            if page is None:
                return False, "Không có tab sảnh Baccarat trong Chrome CDP 9222."
            import websocket
            ws = websocket.create_connection(BR.validated_endpoint(page), suppress_origin=True,
                                             timeout=BR.SCRIPT_TIMEOUT, redirect_limit=0,
                                             http_no_proxy=["127.0.0.1", "localhost", "::1"])
            self._command(ws, "Runtime.enable")
            context_id, _ = self._lobby_context(ws)
            if context_id is None:
                return False, "Sảnh chưa hiển thị danh sách bàn; hãy mở lại trang Baccarat."
            box = self._value(ws, TILE_BOX_JS % json.dumps(table_id), context_id)
            if not isinstance(box, dict) or "x" not in box:
                return False, "Không tìm thấy ô bàn để mở (%s)." % (box or {}).get("reason", "tile_missing")
            for event in ("mouseMoved", "mousePressed", "mouseReleased"):
                params = {"type": event, "x": box["x"], "y": box["y"]}
                if event != "mouseMoved":
                    params.update(button="left", clickCount=1)
                self._command(ws, "Input.dispatchMouseEvent", params)
            deadline = time.monotonic() + wait_seconds
            pattern = re.compile(r"[?&#]table_id=%s(?=&|$)" % re.escape(table_id))
            while time.monotonic() < deadline:
                time.sleep(1.5)
                if pattern.search((self.session._cdp_page() or {}).get("url", "")):
                    return True, "Đã mở bàn %s qua ô bàn của sảnh." % box.get("title", table_id)
            return False, "Đã bấm ô bàn nhưng chưa chuyển vào bàn %s." % table_id
        except Exception as exc:
            return False, "Không mở được bàn (%s)." % type(exc).__name__
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
