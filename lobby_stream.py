# -*- coding: utf-8 -*-
"""Quét sảnh Baccarat qua luồng WebSocket công khai của chính trang.

Chỉ đọc các message lobby.histories / lobby.historyUpdated (mã bàn + lịch sử cầu),
cuộn danh sách để sảnh tự gửi dữ liệu cho các bàn ngoài màn hình, rồi khôi phục vị trí cuộn.
Khi cần, bấm ô lọc “Ưa thích” và ô bàn bằng chuột người thật (human_mouse.py) để chỉ quét
những bàn đã thích. Không gọi API nội bộ, không đọc cookie/token, không bấm nút cược.
"""

import json
import re
import time
from urllib.parse import urlsplit

import browser as BR
import human_mouse as HM

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
    // Sảnh bản mới đánh dấu bàn đã thích ngay trong ô bàn (svg data-role favoriteFilled/
    // favoriteEmpty, hoặc data-active trên nút sao), nên đọc ở đây là đủ, không cần bấm
    // ô lọc nào. Chỉ dùng selector CSS thường: thuộc tính có dấu ':' như xlink:href làm
    // querySelectorAll ném SyntaxError và cả đoạn script đọc bàn sẽ hỏng.
    const favOf = (tile) => {
        const star = tile.querySelector('[data-role="table-favorite-icon"], [data-role="hero-favorite-button"]');
        if (!star) return null;
        if (star.hasAttribute('data-active')) return star.getAttribute('data-active') === 'true';
        const marks = [...star.querySelectorAll('svg')].map((s) => s.getAttribute('data-role'));
        if (marks.includes('favoriteFilled')) return true;
        if (marks.includes('favoriteEmpty')) return false;
        return null;
    };
    return {tiles: [...document.querySelectorAll('[data-role="table-tile"]')].map((tile) => {
        const fiber = tableOf(tile);
        const table = (fiber && fiber.memoizedProps.table) || {};
        return {id: typeof table.id === 'string' ? table.id : '', title: typeof table.title === 'string' ? table.title : '',
                game_type: (table.gameDetails || {}).gameType || '',
                lightning: !!(table.gameDetails || {}).gameConfig?.lightning,
                favorite: favOf(tile),
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


EVO_ENTRY_JS = """(() => {
    if (window !== window.top) return {reason: 'not_top_frame'};
    // Trang vào đổi markup theo thời gian: bản cũ đánh dấu EVO bằng data-subprovider,
    // bản hiện tại bằng data-gp="evo" / data-id="LIVE-EVO" và nút "Bắt đầu" không có class.
    const items = [...document.querySelectorAll(
        '.game-item[data-subprovider="EVO"], .game-item[data-gp="evo"], .game-item[data-id="LIVE-EVO"]')];
    if (!items.length) return {reason: 'evo_item_not_found'};
    const score = (item) => {
        const img = item.querySelector('img');
        const alt = (((img && img.alt) || '')).trim().toLowerCase();
        if (alt === 'evolution') return 0;
        const tags = [item.getAttribute('data-subprovider'), item.getAttribute('data-gp'),
                      item.getAttribute('data-id')].join(' ').toLowerCase();
        return tags.includes('evo') ? 1 : 2;
    };
    const ranked = items.map((item) => ({item, s: score(item)})).sort((a, b) => a.s - b.s);
    if (ranked.length > 1 && ranked[0].s === ranked[1].s) {
        return {reason: 'evo_button_ambiguous', count: ranked.length};
    }
    const item = ranked[0].item;
    // Thẻ ở cuối danh sách chỉ render sau khi cuộn tới, nên phải cuộn rồi mới đo.
    item.scrollIntoView({block: 'center'});
    const inside = (r) => r.width >= 8 && r.height >= 8 && r.x >= 0 && r.y >= 0
                       && r.bottom <= innerHeight && r.right <= innerWidth;
    const center = (r) => ({x: r.x + r.width / 2, y: r.y + r.height / 2});
    const btn = item.querySelector('button.btn-main') || item.querySelector('button');
    if (btn && btn.disabled) return {reason: 'evo_button_not_found'};
    if (btn) {
        btn.scrollIntoView({block: 'center'});
        const rect = btn.getBoundingClientRect();
        // Bản markup cũ để nút "Bắt đầu" hiển thị thì bấm thẳng vào nút.
        if (inside(rect)) return {...center(rect), via: 'button',
                label: (btn.innerText || btn.getAttribute('title') || '').trim().slice(0, 40)};
    }
    const rect = item.getBoundingClientRect();
    // Cửa sổ bị kéo nhỏ hơn thẻ: bấm vào phần thẻ còn nhìn thấy thay vì bỏ cuộc.
    const left = Math.max(rect.x, 0), right = Math.min(rect.x + rect.width, innerWidth);
    const top = Math.max(rect.y, 0), bottom = Math.min(rect.y + rect.height, innerHeight);
    if (right - left < 24 || bottom - top < 24) return {reason: 'evo_button_pending'};
    // Bản markup hiện tại ẩn nút (display:none) nên đích bấm là cả thẻ EVO;
    // góc trên bên phải là ngôi sao yêu thích có onclick riêng nên điểm bấm lệch xuống dưới trái.
    const point = {x: Math.round(left + (right - left) * 0.4), y: Math.round(top + (bottom - top) * 0.62)};
    const hit = document.elementFromPoint(point.x, point.y);
    if (!hit || !item.contains(hit) || hit.closest('.favorite-game-icon-wrapper')) {
        return {reason: 'evo_card_blocked'};
    }
    return {...point, via: 'card', label: item.getAttribute('data-id') || 'EVO'};
})()"""

FAVORITE_JS = """(() => {
    // Ô lọc "Ưa thích" có hai đời markup: bản cũ <li id="favorite"><input id="favorite"
    // type="checkbox"><label>, bản mới là <button data-role="favourites-mobile-button"
    // data-active="false"><span>Yêu thích</span></button> trên thanh đầu trang.
    // Lớp CSS đổi liên tục nên chỉ bám vào id/data-role/label; không bấm vào ngôi sao
    // yêu thích nằm trên từng ô bàn.
    //
    // Sảnh hiện tại render CẢ HAI: li#favorite (desktop, có label bấm được) và nút
    // favourites-mobile-button. Nút mobile không chứa label nên target rơi xuống
    // input#favorite 1×1 px và bị coi là ẩn — vì vậy phải ưu tiên li#favorite trước.
    const usable = (node) => {
        if (!node) return false;
        const r = node.getBoundingClientRect();
        return r.width >= 8 && r.height >= 8;
    };
    let node = document.querySelector('li#favorite');
    if (!usable(node)) {
        const box = document.querySelector('input#favorite[type="checkbox"]');
        if (box && box.closest('li')) node = box.closest('li');
    }
    if (!usable(node)) {
        node = document.querySelector('button[data-role="favourites-mobile-button"], [data-role="favourites-button"]');
    }
    if (!node) {
        node = [...document.querySelectorAll('li,button')].find((candidate) =>
            /^(Ưa thích|Yêu thích|Favorites?)$/i.test((candidate.innerText || '').replace(/\\s+/g, ' ').trim())) || null;
    }
    if (!node) return {reason: 'favorite_filter_not_found'};
    const box = node.querySelector('input[type="checkbox"]') || document.querySelector('input#favorite[type="checkbox"]');
    const label = (box && box.id && node.querySelector('label[for="' + box.id + '"]')) || node.querySelector('label');
    const flag = node.querySelector('[data-selected]') || node.closest('[data-selected]') || node;
    const active = (flag && flag.getAttribute('data-selected') === 'true')
        || (flag && flag.getAttribute('aria-selected') === 'true')
        || (node.getAttribute('data-active') === 'true')
        || (node.getAttribute('aria-pressed') === 'true')
        || !!(box && box.checked);
    // Bấm vào label/nút hiển thị, không bao giờ bấm vào input ẩn 1×1.
    const target = (label && usable(label) ? label : null) || node;
    target.scrollIntoView({block: 'nearest', inline: 'nearest'});
    const rect = target.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) return {reason: 'favorite_filter_hidden', active};
    const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) {
        return {reason: 'favorite_filter_offscreen', active};
    }
    const hit = document.elementFromPoint(x, y);
    if (!hit || !node.contains(hit) || hit.closest('.favorite-game-icon-wrapper')) {
        return {reason: 'favorite_filter_blocked', active};
    }
    return {x, y, active, label: (node.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40)};
})()"""

LOBBY_HEALTH_JS = """(() => {
    const text = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim();
    return {
        href: location.href,
        title: document.title,
        ready: document.readyState,
        tiles: document.querySelectorAll('[data-role="table-tile"]').length,
        roads: document.querySelectorAll('[data-role="history-roads"] canvas').length,
        loading: !!document.querySelector('[data-role="loading-screen"], [data-role="loading-bar"]'),
        auth: /User authentication failed|your session may be expired|Error Code:\\s*EV\\.7|Bạn đã đăng xuất|You have been logged out/i.test(text),
        launch_failed: /Mở game không thành công|launch game fail/i.test(text),
        body: text.slice(0, 180)
    };
})()"""


class LobbyStream:
    """Đọc luồng message sảnh qua CDP chỉ đọc trên Chrome đang mở."""

    def __init__(self, session=None, human_mouse=True):
        self.session = session or BR.BrowserSession()
        self.note = ""
        self.last_error = ""
        self.diag = {}
        self.frames = 0
        self.contexts = []
        self.histories = {}
        self._seq = 0
        self.human_mouse = bool(human_mouse)
        self._cursor = None
        self.session_lost = False
        self.favorite_mode = ""

    def _reset(self):
        self.contexts, self.histories, self._seq = [], {}, 0
        self.frames = 0
        self.diag = {}
        self.last_error = ""
        self.session_lost = False
        self.favorite_mode = ""

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
            if message.get("method") == "Runtime.executionContextDestroyed":
                dead = (message.get("params") or {}).get("executionContextId")
                if dead in self.contexts:
                    self.contexts.remove(dead)
            if message.get("method") == "Runtime.executionContextsCleared":
                self.contexts = []
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

    def _try_value(self, ws, expression, context_id):
        """Đọc một khung; iframe đóng giữa chừng không được làm hỏng cả lượt quét."""
        try:
            return self._value(ws, expression, context_id)
        except Exception:
            if context_id in self.contexts:
                self.contexts.remove(context_id)
            return None

    def _dispatch_click(self, ws, x, y):
        """Bấm tối thiểu 3 sự kiện: đường dự phòng khi chuột người thật lỗi."""
        for event in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": event, "x": x, "y": y}
            if event != "mouseMoved":
                params.update(button="left", clickCount=1)
            self._command(ws, "Input.dispatchMouseEvent", params)

    def _click_at(self, ws, x, y):
        """Bấm một điểm bằng chuột người thật; lỗi giữa chừng thì vẫn bấm đúng đích."""
        # Tab ẩn (cửa sổ bị thu nhỏ/che) làm mỗi sự kiện chuột chờ ~5 giây mới trả lời.
        # Đưa tab lên trước để chuột chạy đúng nhịp; lỗi quyền/không hỗ trợ thì bỏ qua.
        try:
            self._command(ws, "Page.bringToFront")
        except Exception:
            pass
        if not self.human_mouse:
            self._dispatch_click(ws, x, y)
            self._cursor = (float(x), float(y))
            return
        try:
            mouse = HM.HumanMouse(lambda method, params: self._command(ws, method, params))
            if self._cursor is not None:
                mouse.last = self._cursor
            mouse.click(x, y)
            self._cursor = (float(x), float(y))
        except Exception:
            # Không để một cú lướt chuột hỏng chặn việc vào bàn.
            self._dispatch_click(ws, x, y)
            self._cursor = (float(x), float(y))

    @staticmethod
    def _star_favorites(snapshot):
        """id các bàn có sao "đã thích", hoặc None nếu markup ô bàn không lộ trạng thái.

        Trả None là tín hiệu để quay về cách bấm ô lọc cũ; trả tập rỗng nghĩa là sảnh
        đang hiện bàn mà chưa bàn nào được thích, một tình huống có thật.
        """
        rows = (snapshot or {}).get("tiles") or []
        marks = [(row.get("id"), row.get("favorite")) for row in rows if row.get("id")]
        if not marks or all(favorite is None for _, favorite in marks):
            return None
        return {table_id for table_id, favorite in marks if favorite is True}

    def _favorite_state(self, ws, context_id):
        value = self._try_value(ws, FAVORITE_JS, context_id)
        return value if isinstance(value, dict) else {"reason": "favorite_filter_not_found"}

    def _set_favorite_filter(self, ws, context_id, active, wait_seconds=8):
        """Bật/tắt ô lọc “Ưa thích” đúng một lần. Trả (ok, supported, note).

        Sảnh đã ở đúng trạng thái thì không bấm, nhờ vậy quét lại nhiều lượt
        không vô tình tắt bộ lọc của người dùng.
        """
        active = bool(active)
        state = self._favorite_state(ws, context_id)
        reason = state.get("reason")
        if reason == "favorite_filter_not_found":
            return False, False, "Không thấy ô lọc “Ưa thích” trên sảnh."
        # Đã ở đúng trạng thái thì không cần bấm, kể cả khi nút đang bị ẩn.
        if bool(state.get("active")) == active:
            return True, True, ""
        if reason:
            return False, True, "Không bấm được ô lọc “Ưa thích” (%s)." % reason
        try:
            self._click_at(ws, float(state["x"]), float(state["y"]))
        except Exception:
            return False, True, "Không bấm được ô lọc “Ưa thích” trên sảnh."
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            time.sleep(0.4)
            state = self._favorite_state(ws, context_id)
            if not state.get("reason") and bool(state.get("active")) == active:
                # Danh sách bàn cần một nhịp render lại sau khi đổi bộ lọc.
                time.sleep(0.8)
                return True, True, ""
        return (False, True, "Đã bấm “Ưa thích” nhưng sảnh chưa %s bộ lọc sau %d giây." % (
            "bật" if active else "tắt", wait_seconds))

    def _lobby_context(self, ws):
        for context_id in list(self.contexts):
            value = self._try_value(ws, TILES_JS, context_id)
            if isinstance(value, dict) and value.get("tiles"):
                self.frames += 1
                return context_id, value
        return None, None

    def _wait_lobby_context(self, ws, seconds=25.0):
        """Vừa mở tab là SPA còn đang tải danh sách bàn; chỉ bỏ cuộc khi hết thời gian chờ."""
        deadline = time.monotonic() + seconds
        while True:
            context_id, snapshot = self._lobby_context(ws)
            if context_id is not None or time.monotonic() >= deadline:
                return context_id, snapshot
            time.sleep(1.5)

    def sweep(self, max_seconds=75, favorites_only=False):
        """Cuộn hết danh sách để sảnh gửi lịch sử mọi bàn, rồi trả về danh sách bàn.

        favorites_only=True: ưu tiên đọc dấu sao "đã thích" trong từng ô bàn và chỉ giữ
        các bàn có sao, không cần bấm gì trên sảnh. Đời markup không có dấu sao trong ô
        bàn thì mới bấm ô lọc “Ưa thích”. Tắt cờ này thì app bấm bỏ lọc (nếu đang bật)
        để không lẫn bàn ngoài danh sách.
        """
        self._reset()
        self.note = ""
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
            context_id, snapshot = self._wait_lobby_context(ws)
            if context_id is None:
                if self._lost_session(ws):
                    return []
                self.last_error = ("Sảnh chưa hiển thị danh sách bàn sau 25 giây chờ; "
                                   "hãy mở lại trang Baccarat.")
                return []
            star_ids = self._star_favorites(snapshot) if favorites_only else None
            if favorites_only and star_ids is not None:
                self.favorite_mode = "stars"
                if not star_ids:
                    self.last_error = ("Không có bàn nào được đánh dấu Ưa thích trong sảnh; "
                                       "tắt ô “Chỉ bàn Ưa thích” để quét toàn bộ.")
                    return []
            else:
                ok, supported, why = self._set_favorite_filter(ws, context_id, favorites_only)
                if not ok and (favorites_only or supported):
                    self.last_error = why
                    return []
                # Đọc lại sau khi đổi bộ lọc: danh sách và chiều cao cuộn đã khác.
                fresh = self._try_value(ws, TILES_JS, context_id)
                if isinstance(fresh, dict) and fresh.get("tiles"):
                    snapshot = fresh
            deadline = time.monotonic() + max_seconds
            start_top = snapshot["scroll"]["top"]
            steps = 0
            seen = set()
            tiles = {}
            stagnant = 0
            position = 0
            while time.monotonic() < deadline and steps < MAX_SCROLL_STEPS:
                self._evaluate(ws, SCROLL_JS % position, context_id)
                time.sleep(SCROLL_SETTLE)
                result = self._evaluate(ws, TILES_JS, context_id)
                snapshot = result.get("result", {}).get("value") or snapshot
                # Gom qua mọi vị trí cuộn: sảnh chỉ render ô bàn đang nhìn thấy,
                # nên chỉ nhìn khung cuối sẽ mất các bàn ưa thích ở trên.
                for tile in snapshot.get("tiles", []):
                    if tile.get("id"):
                        tiles[tile["id"]] = tile
                ids = {tile["id"] for tile in snapshot["tiles"]}
                bottom = max(snapshot["scroll"]["max"] - snapshot["scroll"]["client"], 0)
                at_bottom = position >= bottom
                # Sảnh bản mới render hết ô bàn ra DOM nên "hết bàn mới" rất nhanh, còn
                # lobby.histories tới qua WebSocket chậm hơn nhiều. Chưa có lịch sử thì
                # chưa được nghỉ sớm, nếu không lượt quét kết thúc sau ~4 giây với 0 bàn.
                stagnant = 0 if (ids - seen or not at_bottom or not self.histories) else stagnant + 1
                seen |= ids
                if stagnant >= 2:
                    break
                position = min(position + SCROLL_STEP, bottom)
                steps += 1
            self._evaluate(ws, SCROLL_JS % start_top, context_id)
            favorite_ids = None
            if favorites_only:
                if self.favorite_mode == "stars":
                    favorite_ids = set(star_ids) | {tid for tid, tile in tiles.items()
                                                    if tile.get("favorite")}
                    if not favorite_ids:
                        self.last_error = ("Không có bàn nào được đánh dấu Ưa thích trong sảnh; "
                                           "tắt ô “Chỉ bàn Ưa thích” để quét toàn bộ.")
                        return []
                else:
                    state = self._favorite_state(ws, context_id)
                    if state.get("reason") or not state.get("active"):
                        self.last_error = ("Bộ lọc “Ưa thích” bị tắt giữa lúc quét; bỏ kết quả để "
                                           "tránh lẫn bàn ngoài danh sách.")
                        return []
                    favorite_ids = set(tiles)
            if not self.histories:
                # Sảnh bị site đá ra hộp “Bạn đã đăng xuất”/EV.7 cũng im lặng y hệt trường
                # hợp WebSocket chưa kịp gửi. Phải phân biệt ở đây, nếu không vòng 15 giây
                # cứ cuộn tiếp trên trang chết (đã thấy trong bcr_trace.jsonl).
                if self._lost_session(ws):
                    return []
                self.diag = {"tiles_in_dom": len(tiles), "ws_tables": 0, "scroll_steps": steps,
                             "favorites_only": bool(favorites_only), "favorite_mode": self.favorite_mode,
                             "favorite_tiles": len(favorite_ids or tiles), "frames": self.frames}
                self.last_error = ("Sảnh không gửi lịch sử bàn qua WebSocket trong %d giây "
                                   "(nhận %d khung, thấy %d ô bàn). Bấm “Nhận tab sảnh” rồi quét lại."
                                   % (int(max_seconds), self.frames, len(tiles)))
                return []
            rows = []
            for table_id, entry in self.histories.items():
                if favorite_ids is not None and table_id not in favorite_ids:
                    continue
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
                    "favorite": table_id in favorite_ids if favorite_ids is not None else False,
                })
            rows.sort(key=lambda r: r["label"].casefold())
            self.diag = {"tiles_in_dom": len(tiles), "ws_tables": len(rows),
                         "scroll_steps": steps, "matched_road": sum(1 for r in rows if r["road_match"]),
                         "favorites_only": bool(favorites_only), "favorite_mode": self.favorite_mode,
                         "favorite_tiles": len(favorite_ids or tiles)}
            self.note = "Quét %d bước cuộn · %d bàn qua WebSocket · %d bàn khớp cầu đang vẽ%s." % (
                steps, len(rows), self.diag["matched_road"],
                " · %d bàn Ưa thích (%s)" % (self.diag["favorite_tiles"], self.favorite_mode)
                if favorites_only else "")
            return rows
        except Exception as exc:
            # Không đưa chi tiết CDP/socket thô vào UI.
            self.last_error = (BR.CDP_CLOSED_NOTE if BR.cdp_port_closed(exc) else
                               "Không quét được luồng sảnh (%s)." % type(exc).__name__)
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
                if self._lost_session(ws):
                    return False, self.last_error
                return False, "Sảnh chưa hiển thị danh sách bàn; hãy mở lại trang Baccarat."
            box = self._value(ws, TILE_BOX_JS % json.dumps(table_id), context_id)
            if not isinstance(box, dict) or "x" not in box:
                return False, "Không tìm thấy ô bàn để mở (%s)." % (box or {}).get("reason", "tile_missing")
            self._click_at(ws, box["x"], box["y"])
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

    def back_to_lobby(self, wait_seconds=25):
        """Rời màn hình bàn bằng history/site UI, tuyệt đối không sửa hash của EVO."""
        import websocket

        ws = None
        try:
            page = self.session._cdp_page()
            url = (page or {}).get("url", "")
            if not BR.is_lobby_url(url):
                return False, "Không có tab sảnh Evolution trong Chrome CDP 9222."
            ws = websocket.create_connection(BR.validated_endpoint(page), suppress_origin=True,
                                             timeout=BR.SCRIPT_TIMEOUT, redirect_limit=0,
                                             http_no_proxy=["127.0.0.1", "localhost", "::1"])
            self._command(ws, "Runtime.enable")
            if not self._wait_context(ws):
                return False, "Không lấy được ngữ cảnh trang sảnh để trở về danh sách bàn."
            # history.back() chỉ dùng khi thật sự đang ở màn hình bàn. Tab sảnh mở từ launch
            # có mục lịch sử đầu là /entry dùng một lần: quay lại mục đó là EVO trả
            # "User authentication failed (EV.7)" dù tài khoản không đăng nhập nơi nào khác.
            fragment = urlsplit(url).fragment
            if "table_id" not in fragment and "table_id=" not in url:
                self.note = "Đã ở danh sách bàn Baccarat sẵn."
                return True, "Đã ở danh sách bàn · không cần rời bàn."
            # EVO xử lý hash nội bộ; history.back() đi theo lịch sử thật của site và
            # không buộc /entry tải lại (nguyên nhân làm mất phiên launch).
            self._evaluate(ws, "history.back()", self.contexts[0])
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                time.sleep(1.5)
                page = self.session._cdp_page() or {}
                fragment = urlsplit(page.get("url", "")).fragment
                if "table_id" not in fragment and self._lobby_context(ws)[0]:
                    self.note = "Đã về danh sách bàn Baccarat."
                    return True, "Đã rời bàn · danh sách bàn Baccarat hiển thị trở lại."
            state = self._health(ws)
            if state.get("status") == "auth":
                self.session_lost = True
            return False, (self._health_note(state) if state.get("status") == "auth" else
                           "Đã bấm quay lại nhưng %d giây chưa thấy lại danh sách bàn; "
                           "bấm nút quay lại sảnh của EVO hoặc “Nhận tab sảnh” để thử lại."
                           % wait_seconds)
        except Exception as exc:
            return False, "Không về được danh sách bàn (%s)." % type(exc).__name__
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def _wait_lobby_page(self, portal_socket, wait_seconds):
        """Chờ tab sảnh EVO; nếu sảnh nằm trong iframe của trang vào thì nhận ra ở đó.

        Đồng thời đọc trang vào để phát hiện sớm “Mở game không thành công”/EV.7 thay vì
        chờ hết thời gian rồi báo chung chung.
        """
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            time.sleep(1.5)
            try:
                pages = self.session._cdp_pages()
            except Exception:
                continue
            lobby = next((page for page in pages if BR.is_lobby_url(page.get("url"))), None)
            if lobby is not None:
                return lobby, False, {}
            if any(isinstance(page, dict) and page.get("type") == "page"
                   and BR.is_evo_entry_url(page.get("url"))
                   for page in (self.session._last_cdp_targets
                                if isinstance(self.session._last_cdp_targets, list) else [])):
                return None, False, {"status": "auth", "entry": True}
            if not any(BR.is_portal_url(page.get("url")) for page in pages):
                continue  # trang vào đã tự chuyển sang hướng khác; chờ lượt sau.
            try:
                if self._lobby_context(portal_socket)[0] is not None:
                    return None, True, {}
                state = self._health(portal_socket)
                if state.get("status") in ("auth", "launch_failed"):
                    return None, False, state
            except Exception:
                continue
        return None, False, {}

    def _wait_context(self, ws, seconds=6.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.contexts:
            try:
                self._command(ws, "Runtime.evaluate", {"expression": "1", "returnByValue": True})
            except Exception:
                break
        return bool(self.contexts)

    def _health(self, ws):
        """Đọc trạng thái tốt nhất từ các context đang mở của một tab EVO."""
        fallback = {}
        for context_id in list(self.contexts):
            value = self._try_value(ws, LOBBY_HEALTH_JS, context_id)
            if not isinstance(value, dict):
                continue
            if value.get("auth"):
                return dict(value, status="auth")
            if value.get("launch_failed"):
                return dict(value, status="launch_failed")
            if value.get("tiles") or value.get("roads"):
                return dict(value, status="ok")
            if value.get("loading") or not fallback:
                fallback = value
        return dict(fallback, status="loading" if fallback.get("loading") else "unknown")

    def _lobby_status(self, page, wait_seconds=0.0):
        """Một lần đọc ngắn trạng thái tab sảnh: ok/loading/auth/launch_failed/unknown."""
        import websocket

        ws = None
        # Context của tab trước không dùng được trên kết nối mới.
        self.contexts = []
        try:
            ws = websocket.create_connection(
                BR.validated_endpoint(page), suppress_origin=True, timeout=BR.SCRIPT_TIMEOUT,
                redirect_limit=0, http_no_proxy=["127.0.0.1", "localhost", "::1"])
            self._command(ws, "Runtime.enable")
            if not self._wait_context(ws):
                return {}
            deadline = time.monotonic() + wait_seconds
            while True:
                state = self._health(ws)
                if (state.get("status") in ("ok", "auth", "launch_failed")
                        or time.monotonic() >= deadline):
                    return state
                time.sleep(0.8)
        except Exception:
            return {}
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    @staticmethod
    def _health_note(state):
        status = (state or {}).get("status")
        if status == "auth":
            if state.get("entry"):
                return ("Evolution returned /entry: the launch token was rejected or expired "
                        "(EV.3/EV.7). Sign in again at the portal and open EVO from its game button; "
                        "do not reload or paste /entry.")
            return ("Tab sảnh báo phiên Evolution không còn hiệu lực (EV.7 hay hộp “Bạn đã "
                    "đăng xuất”). Hãy đăng nhập lại ở trang gốc rồi mở lại EVO; app không đổi link sảnh.")
        if status == "launch_failed":
            return ("Trang vào báo “Mở game không thành công”. Site có thể đang bảo trì hoặc tài "
                    "khoản chưa được phép mở EVO; thử lại sau hoặc dùng tài khoản khác.")
        if status == "loading":
            return "Tab sảnh Evolution còn đang tải; chờ vài giây rồi bấm “Quét toàn sảnh”."
        return ""

    def _lost_session(self, ws):
        """Phân biệt "mất phiên thật" với "trang còn đang tải" trước khi bấm tiếp.

        Sảnh đã EV.7 thì mọi cú bấm/ cuộn tiếp theo chỉ ăn nhầm trên trang lỗi, nên
        cờ session_lost bật lên cho app dừng vòng tự động.
        """
        state = self._health(ws)
        if state.get("status") != "auth":
            return False
        self.session_lost = True
        self.last_error = self._health_note(state)
        return True

    def find_lobby_tab(self):
        """Nhận tab sảnh do người dùng tự mở; app không bấm vào sảnh hộ, không sửa URL.

        Tab mở từ launch của site gắn với một phiên EVO dùng một lần. Đổi hash/URL có
        thể làm EVO tải lại /entry và trả 403/EV.7, nên chỉ đọc và báo trạng thái.
        """
        self._reset()
        try:
            pages = self.session._cdp_pages()
        except Exception as exc:
            if BR.cdp_port_closed(exc):
                return False, BR.CDP_CLOSED_NOTE
            return False, "Không đọc được danh sách tab Chrome CDP 9222 (%s)." % type(exc).__name__
        lobbies = [page for page in pages if BR.is_lobby_url(page.get("url"))]
        if not lobbies:
            if any(isinstance(page, dict) and page.get("type") == "page"
                   and BR.is_evo_entry_url(page.get("url"))
                   for page in (self.session._last_cdp_targets
                                if isinstance(self.session._last_cdp_targets, list) else [])):
                return False, self._health_note({"status": "auth", "entry": True})
            return False, ("Chưa thấy tab sảnh Baccarat. Hãy tự mở %s → EVO → Baccarat trong cửa sổ "
                           "Chrome rồi bấm lại nút này." % BR.PORTAL_HOST)
        expired = ""
        # Tab vừa launch thường nằm cuối danh sách; thử tab mới trước, bỏ qua tab cũ đã EV.7.
        for lobby in reversed(lobbies):
            state = self._lobby_status(lobby)
            status = state.get("status")
            if status == "auth":
                self.session_lost = True
                expired = self._health_note(state)
                continue
            if status == "launch_failed":
                return False, self._health_note(state)
            url = lobby.get("url", "")
            self.note = "Đã nhận tab sảnh Baccarat."
            if "table_id" in urlsplit(url).fragment or "table_id=" in urlsplit(url).query:
                return True, ("Đã nhận tab sảnh Baccarat · đang ở màn hình bàn; bấm nút quay lại "
                              "sảnh của EVO nếu muốn quét toàn sảnh.")
            if status == "loading":
                return True, ("Đã nhận tab sảnh Baccarat · sảnh còn đang tải; chờ vài giây rồi "
                              "bấm “Quét toàn sảnh”.")
            return True, "Đã nhận tab sảnh Baccarat · %s" % BR._safe_url(url)
        return False, expired or ("Tab sảnh báo phiên Evolution không còn hợp lệ. Hãy đăng nhập lại "
                                  "ở trang gốc rồi mở lại EVO; app không đổi link sảnh.")

    def open_lobby_from_portal(self, wait_seconds=30):
        """Bấm “Chơi game” của Evolution trên trang vào và nhận tab sảnh vừa mở.

        Không đổi URL/hash của sảnh sau khi launch: phiên EVO gắn với đúng ngữ cảnh
        mà site vừa mở. Chỉ điều hướng giữa trang vào và sảnh, không chạm khu vực cược.
        """
        import websocket

        def connect(page):
            return websocket.create_connection(
                BR.validated_endpoint(page), suppress_origin=True, timeout=BR.SCRIPT_TIMEOUT,
                redirect_limit=0, http_no_proxy=["127.0.0.1", "localhost", "::1"])

        ws = None
        self._reset()
        try:
            portal = next((page for page in self.session._cdp_pages()
                           if BR.is_portal_url(page.get("url"))), None)
            if portal is None:
                return False, ("Không có tab trang vào %s trong Chrome CDP 9222; hãy mở %s "
                               "và đăng nhập." % (BR.PORTAL_HOST, BR.START_URL))
            ws = connect(portal)
            self._command(ws, "Runtime.enable")
            box, reason, deadline = None, "evo_item_not_found", time.monotonic() + wait_seconds
            while box is None:
                # iframe luôn trả not_top_frame; chỉ lý do từ khung chính đáng tin nên vòng
                # retry phải chạy tiếp tới hạn chót, không bỏ cuộc vì một iframe.
                for context_id in list(self.contexts):
                    value = self._try_value(ws, EVO_ENTRY_JS, context_id)
                    if not isinstance(value, dict):
                        continue
                    if "x" in value:
                        box = value
                        break
                    if value.get("reason") != "not_top_frame":
                        reason = value.get("reason", reason)
                if box is not None or time.monotonic() >= deadline:
                    break
                # Trang vào render trễ: thẻ EVO nằm ngoài màn hình nên cần cuộn rồi chờ thêm lượt.
                time.sleep(1.2)
            if box is None:
                return False, "Không bấm được nút “Chơi game” của Evolution (%s)." % reason
            self.diag["evo_click"] = box
            self._click_at(ws, box["x"], box["y"])
            lobby, embedded, failure = self._wait_lobby_page(ws, wait_seconds)
            ws.close()
            ws = None
            if failure:
                return False, self._health_note(failure)
            if embedded:
                self.note = "Sảnh Evolution hiện trong trang vào."
                return True, "Đã vào sảnh qua iframe · bấm “Quét toàn sảnh” để lấy lịch sử bàn."
            if lobby is None:
                return False, ("Đã bấm nhưng %d giây chưa thấy tab sảnh Evolution; "
                               "trang vào có thể đòi đăng nhập trước." % wait_seconds)
            url = lobby.get("url", "")
            self.note = "Đã vào sảnh Evolution."
            if "table_id" in urlsplit(url).fragment or "table_id=" in urlsplit(url).query:
                return True, ("Đã vào sảnh Evolution · đang ở màn hình bàn; bấm nút quay lại "
                              "sảnh của EVO nếu muốn quét toàn sảnh.")
            return True, "Đã vào sảnh Evolution · %s" % BR._safe_url(url)
        except Exception as exc:
            return False, "Không vào được sảnh Evolution (%s)." % type(exc).__name__
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
