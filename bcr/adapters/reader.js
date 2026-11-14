/* BCR reader — logic ĐỌC bảng cầu cho Chrome driver.
 * browser.py nạp file này vào từng frame bằng execute_script.
 * Chỉ đọc DOM, không bấm, không đổi trang, không gửi đi đâu. */
window.__BCR_SCAN = (function () {
  "use strict";

  const MIN_TEXT_GROUP = 6;
  const MIN_SVG_GROUP = 12;

  /* ---------- màu -> cửa ---------- */
  function hueOf(color) {
    const m = /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/.exec(color || "");
    if (!m) return null;
    const r = +m[1] / 255, g = +m[2] / 255, b = +m[3] / 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    if (d < 0.15) return null;
    let h;
    if (mx === r) h = ((g - b) / d) % 6;
    else if (mx === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    return (h * 60 + 360) % 360;
  }
  function sideOf(color) {
    const h = hueOf(color);
    if (h === null) return null;
    if (h >= 335 || h <= 22) return "B";
    if (h >= 190 && h <= 275) return "P";
    if (h >= 75 && h <= 170) return "T";
    return null;
  }

  /* ---------- gom node theo container chung gần nhất ---------- */
  function ancestorAt(el, levels) {
    let node = el;
    for (let i = 0; i < levels && node.parentElement; i++) node = node.parentElement;
    return node;
  }
  function bestGrouping(nodes, from, to, minSize) {
    let best = null;
    for (let levels = from; levels <= to; levels++) {
      const map = new Map();
      for (const n of nodes) {
        const key = ancestorAt(n, levels);
        let arr = map.get(key);
        if (!arr) map.set(key, (arr = []));
        arr.push(n);
      }
      const groups = [...map.values()].filter((g) => g.length >= minSize);
      if (!groups.length) continue;
      // Ưu tiên cách gom tạo được NHIỀU bàn riêng nhất; bằng nhau thì lấy cầu dài hơn.
      const score = groups.length * 100000 + groups.reduce((s, g) => s + g.length, 0);
      if (!best || score > best.score) best = { score, groups, levels };
    }
    return best ? best.groups : [];
  }

  /* ---------- thứ tự tay: lưới cột-trước (đọc dọc hết cột rồi sang cột) ---------- */
  function gridOrder(items) {
    const uniq = (arr) => arr.slice().sort((a, b) => a - b).filter((v, i, a) => i === 0 || v - a[i - 1] > 6);
    const xLines = uniq(items.map((i) => i.x));
    const yLines = uniq(items.map((i) => i.y));
    const near = (v, lines) => {
      let bi = 0, bd = Infinity;
      for (let i = 0; i < lines.length; i++) {
        const d = Math.abs(lines[i] - v);
        if (d < bd) { bd = d; bi = i; }
      }
      return bi;
    };
    const pos = items.map((it, idx) => ({ idx, c: near(it.x, xLines), r: near(it.y, yLines) }));
    const geo = pos.slice().sort((a, b) => (a.c - b.c) || (a.r - b.r)).map((p) => p.idx);
    return { geo, dom: pos.map((p) => p.idx), cols: xLines.length, rows: yLines.length };
  }
  function orderedLetters(items) {
    const g = gridOrder(items);
    const useGeo = g.cols >= 2 && g.rows >= 2;
    const order = useGeo ? g.geo : g.dom;
    return { letters: order.map((i) => items[i].side).join(""), order: useGeo ? "geo" : "dom", cols: g.cols, rows: g.rows };
  }

  function nameFromText(txt) {
    /* "Lucky Baccarat B04", "Baccarat Dragon 7" -> lấy nguyên tên; "1,5tr" -> bỏ. */
    const words = (txt || "").split(" ");
    let acc = "";
    for (let i = 0; i < words.length && i < 5; i++) {
      acc = acc ? acc + " " + words[i] : words[i];
      if (/[A-Za-z][\w'-]*\d{1,3}$/.test(words[i])) break;
    }
    return /^[A-Za-z][A-Za-z0-9 .&'-]{0,30}\d{1,3}$/.test(acc) ? acc : "";
  }

  function labelFor(container) {
    let node = container;
    for (let up = 0; up < 9 && node; up++) {
      for (const attr of node.attributes || []) {
        if (/^(data-|aria-|title)/i.test(attr.name) && /\d/.test(attr.value) && attr.value.length <= 40) {
          return attr.value.trim();
        }
      }
      const txt = (node.innerText || "").replace(/\s+/g, " ").trim();
      const nm = nameFromText(txt);
      if (nm) return nm;
      const short = /([A-Za-z]\d{1,3}(?![\d]))/.exec(txt);
      if (short) return short[1];
      node = node.parentElement;
    }
    return "";
  }

  function boxOf(items) {
    const xs = items.map((i) => i.x), ys = items.map((i) => i.y);
    return { w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys),
             x: Math.round(Math.min(...xs)), y: Math.round(Math.min(...ys)) };
  }
  function dedupe(roads) {
    const seen = new Map();
    for (const r of roads) {
      const key = (r.label || "?") + "|" + r.letters;
      if (!seen.has(key)) seen.set(key, r);
    }
    return [...seen.values()];
  }

  /* ---------- chiến lược 1: chữ B/P/T trong DOM ---------- */
  function textRoads() {
    const leaves = [];
    document.querySelectorAll("*").forEach((el) => {
      if (el.childElementCount !== 0) return;
      const t = (el.textContent || "").trim().toUpperCase();
      if (t === "B" || t === "P" || t === "T") {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) leaves.push({ el, x: r.x + r.width / 2, y: r.y + r.height / 2, side: t });
      }
    });
    if (leaves.length < MIN_TEXT_GROUP) return { roads: [], scanned: leaves.length };
    const groups = bestGrouping(leaves.map((l) => l.el), 1, 9, MIN_TEXT_GROUP);
    const byEl = new Map(leaves.map((l) => [l.el, l]));
    const roads = groups.map((group) => {
      const items = group.map((el) => byEl.get(el));
      const res = orderedLetters(items);
      const box = boxOf(items);
      return { letters: res.letters, label: labelFor(group[0]),
               source: "text-" + res.order, box };
    }).filter((r) => r.letters.length >= MIN_TEXT_GROUP && r.box.w > 40);
    return { roads: dedupe(roads), scanned: leaves.length };
  }

  /* ---------- chiến lược 2: hình vẽ SVG theo màu ---------- */
  function svgRoads() {
    const shapes = [];
    document.querySelectorAll("svg circle, svg ellipse, svg rect, svg path, svg use").forEach((el) => {
      const st = getComputedStyle(el);
      const side = sideOf(st.fill) || sideOf(st.stroke);
      if (!side) return;
      const r = el.getBoundingClientRect();
      if (r.width < 5 || r.height < 5 || r.width > 70 || r.height > 70) return;
      if (el.closest("button,[role=button]")) return;
      shapes.push({ el, x: r.x + r.width / 2, y: r.y + r.height / 2, side });
    });
    if (shapes.length < MIN_SVG_GROUP) return { roads: [], scanned: shapes.length };
    const groups = bestGrouping(shapes.map((s) => s.el), 1, 9, MIN_SVG_GROUP);
    const byEl = new Map(shapes.map((s) => [s.el, s]));
    const roads = groups.map((group) => {
      const items = group.map((el) => byEl.get(el));
      const res = orderedLetters(items);
      const box = boxOf(items);
      const sides = new Set(res.letters.replace(/T/g, ""));
      return { letters: res.letters, label: labelFor(group[0]),
               source: "svg-" + res.order, box,
               ok: res.cols >= 3 && res.rows >= 2 && box.w > 60 && sides.size >= 1 && box.h / Math.max(1, res.rows) < 60 };
    }).filter((r) => r.ok);
    return { roads: dedupe(roads), scanned: shapes.length };
  }

  function evolutionRoads() {
    const canvases = [...document.querySelectorAll('[data-role="history-roads"] canvas')];
    const roads = [];
    const roots = new Set(), wanted = new Set(canvases), paths = new Map();
    for (const canvas of canvases) {
      const key = Object.keys(canvas).find((name) => name.startsWith('__reactFiber$'));
      let root = key ? canvas[key] : null;
      for (let i = 0; root?.return && i < 100; i++) root = root.return;
      if (root?.stateNode?.current) roots.add(root.stateNode.current);
    }
    // Follow the committed tree: DOM fiber pointers and return links can be stale.
    for (const root of roots) {
      const stack = [{fiber: root, parents: []}];
      for (let visited = 0; stack.length && visited < 50000; visited++) {
        const {fiber, parents} = stack.pop();
        if (wanted.has(fiber.stateNode)) paths.set(fiber.stateNode, [fiber, ...parents]);
        if (fiber.sibling) stack.push({fiber: fiber.sibling, parents});
        if (fiber.child) stack.push({fiber: fiber.child, parents: [fiber, ...parents].slice(0, 16)});
      }
    }
    for (const canvas of canvases) {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) continue;
      let history = null, table = null, gameType = '';
      for (const fiber of paths.get(canvas) || []) {
        const props = fiber.memoizedProps || {};
        if (props.type === 'BaccaratHistoryRoadTypeBig' && Array.isArray(props.history)) history = props.history;
        if (typeof props.gameType === 'string') gameType = props.gameType;
        if (props.table) { table = props.table; break; }
      }
      if (!table || typeof table.id !== 'string' || !/^[A-Za-z0-9_-]{1,80}$/.test(table.id)) continue;
      if (!['baccarat', 'luckybaccarat'].includes(gameType)) continue;
      let letters = '', readError = '';
      if (!history) readError = 'Chưa đọc được lịch sử Canvas của bàn.';
      else {
        for (const item of history) {
          const side = item?.c === 'R' ? 'B' : item?.c === 'B' ? 'P' : null;
          const ties = item?.ties ?? 0;
          if (!side || !Number.isInteger(ties) || ties < 0 || ties > 50 ||
              !Array.isArray(item.pos) || item.pos.length !== 2 ||
              !item.pos.every((p) => Number.isInteger(p) && p >= 0)) {
            readError = 'Dữ liệu Big Road không đúng định dạng; không suy đoán kết quả.';
            letters = '';
            break;
          }
          letters += side + 'T'.repeat(ties);
          if (letters.length > 400) { letters = ''; readError = 'Lịch sử vượt giới hạn 400 tay.'; break; }
        }
        if (history.length && (history[0].pos?.[0] !== 0 || history[0].pos?.[1] !== 0)) {
          readError = 'Big Road bị cắt đầu; không đủ lịch sử để chọn bàn.';
        } else if (history[0]?.ties) {
          readError = 'Ô đầu có hòa: Big Road không cho biết hòa trước hay sau tay đầu.';
        }
      }
      if (table.gameDetails?.gameConfig?.lightning || [
        'LightningBac0001', 'XXXtremeLB000001', 'NoCommBac0000001',
        'gwbaccarat000001', 'Empgwbaccarat001', 'PTBaccarat000001', 'InsuranceBac0001'
      ].includes(table.id)) {
        readError = 'Bàn có luật trả thưởng đặc biệt; chỉ xem cầu, không giả lập theo bàn chuẩn.';
      }
      roads.push({letters, label: typeof table.title === 'string' ? table.title : table.id,
                  table_id: table.id, game_type: gameType, read_error: readError,
                  source: 'evo-canvas-big', box: {x: Math.round(rect.x), y: Math.round(rect.y), w: rect.width, h: rect.height}});
    }
    return {roads, scanned: canvases.length};
  }

  /* ---------- chiến lược 3: màn hình bàn đã mở (Bead-road + thuộc tính name) ---------- */
  function beadRoads() {
    const roads = [];
    document.querySelectorAll('[data-role="Bead-road"]').forEach((bead) => {
      const items = bead.querySelectorAll('[data-role="roadItem"][name]');
      if (!items.length) return;
      const names = {Banker: "B", Player: "P", Tie: "T"};
      let letters = "", readError = "";
      for (const item of items) {
        // "Banker Player" = Banker thắng kèm huy hiệu Pair -> chỉ token đầu là kết quả.
        const side = names[(item.getAttribute("name") || "").trim().split(/\s+/)[0]];
        if (!side) { letters = ""; readError = "Cầu bàn có ký hiệu lạ; không suy đoán kết quả."; break; }
        letters += side;
        if (letters.length > 400) { letters = ""; readError = "Lịch sử vượt giới hạn 400 tay."; break; }
      }
      const rect = bead.getBoundingClientRect();
      roads.push({letters, label: "", read_error: readError, source: "evo-bead",
                  box: {x: Math.round(rect.x), y: Math.round(rect.y), w: rect.width, h: rect.height}});
    });
    return {roads, scanned: roads.length};
  }

  /* Đọc lịch sử từ canvas bằng cách đi NGƯỢC lên từ chính fiber của canvas.
   * evolutionRoads() phải duyệt cả cây React (tới 50000 node) để tìm đường đi; trên trang
   * bàn đang phát video, duyệt cây lớn mỗi lượt là thứ làm nghẽn renderer. Đi ngược chỉ
   * tốn O(độ sâu), đủ để gặp props.history và props.table của đúng bàn. */
  function evolutionRoadsUp() {
    const canvases = [...document.querySelectorAll('[data-role="history-roads"] canvas')];
    const roads = [];
    for (const canvas of canvases) {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) continue;
      const key = Object.keys(canvas).find((name) => name.startsWith('__reactFiber$'));
      let fiber = key ? canvas[key] : null;
      let history = null, table = null, gameType = '';
      for (let i = 0; fiber && i < 200; i++, fiber = fiber.return) {
        const props = fiber.memoizedProps || {};
        if (!history && props.type === 'BaccaratHistoryRoadTypeBig' && Array.isArray(props.history)) history = props.history;
        if (!gameType && typeof props.gameType === 'string') gameType = props.gameType;
        if (!table && props.table && typeof props.table.id === 'string') table = props.table;
        if (history && table) break;
      }
      if (!table || !/^[A-Za-z0-9_-]{1,80}$/.test(table.id)) continue;
      if (!['baccarat', 'luckybaccarat'].includes(gameType)) continue;
      let letters = '', readError = '';
      if (!history) readError = 'Chưa đọc được lịch sử Canvas của bàn.';
      else {
        for (const item of history) {
          const side = item?.c === 'R' ? 'B' : item?.c === 'B' ? 'P' : null;
          const ties = item?.ties ?? 0;
          if (!side || !Number.isInteger(ties) || ties < 0 || ties > 50 ||
              !Array.isArray(item.pos) || item.pos.length !== 2 ||
              !item.pos.every((p) => Number.isInteger(p) && p >= 0)) {
            readError = 'Dữ liệu Big Road không đúng định dạng; không suy đoán kết quả.';
            letters = '';
            break;
          }
          letters += side + 'T'.repeat(ties);
          if (letters.length > 400) { letters = ''; readError = 'Lịch sử vượt giới hạn 400 tay.'; break; }
        }
        if (history.length && (history[0].pos?.[0] !== 0 || history[0].pos?.[1] !== 0)) {
          readError = 'Big Road bị cắt đầu; không đủ lịch sử để chọn bàn.';
        } else if (history[0]?.ties) {
          readError = 'Ô đầu có hòa: Big Road không cho biết hòa trước hay sau tay đầu.';
        }
      }
      if (table.gameDetails?.gameConfig?.lightning || [
        'LightningBac0001', 'XXXtremeLB000001', 'NoCommBac0000001',
        'gwbaccarat000001', 'Empgwbaccarat001', 'PTBaccarat000001', 'InsuranceBac0001'
      ].includes(table.id)) {
        readError = 'Bàn có luật trả thưởng đặc biệt; chỉ xem cầu, không giả lập theo bàn chuẩn.';
      }
      roads.push({letters, label: typeof table.title === 'string' ? table.title : table.id,
                  table_id: table.id, game_type: gameType, read_error: readError,
                  source: 'evo-canvas-big', box: {x: Math.round(rect.x), y: Math.round(rect.y), w: rect.width, h: rect.height}});
    }
    return {roads, scanned: canvases.length};
  }

  /* ---------- LONG-LONG: lịch sử ván trong [data-role="history-grid"] ---------- */
  function longlongGrids() {
    const out = [];
    /* Ô kết quả có thể là ảnh/nhãn màu: đọc aria-label, title hoặc text và giữ token
       ngắn (số hoặc chữ vàng/đỏ/hỗn). Decoder Python quyết định nghĩa cuối cùng. */
    const token = (n) => ((n.getAttribute && (n.getAttribute('aria-label') || n.getAttribute('title')))
      || n.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 24);
    const isResult = (t) => /^[0-9]{1,2}$/.test(t)
      || /(vàng|vang|gold|yellow|đỏ|\bred\b|hỗn|mix)/i.test(t);
    document.querySelectorAll('[data-role="history-grid"]').forEach((grid) => {
      const tile = grid.closest('[data-role="table-tile"]');
      let tableId = "";
      if (tile) {
        const key = Object.keys(tile).find((n) => n.startsWith('__reactFiber$'));
        let f = key ? tile[key] : null;
        while (f && !(f.memoizedProps && f.memoizedProps.table && f.memoizedProps.table.id)) f = f.return;
        if (f) tableId = f.memoizedProps.table.id;
      }
      const values = [...grid.querySelectorAll('*')]
        .filter((n) => n.children.length === 0)
        .map(token)
        .filter((t) => t && isResult(t));
      if (values.length) out.push({table_id: tableId, results: values});
    });
    return out;
  }

  return function scan() {
    // Chỉ đọc ví ở sảnh danh sách Baccarat; hash-route độc lập với domain/mirror.
    // Số dư hiển thị ở header khi cửa sổ đủ rộng và chuyển xuống footer khi thu nhỏ,
    // nên đọc cả hai role thay vì chỉ footer (trước đây bỏ trống khi header hiện).
    const isBaccaratLobby = /(?:^|[&#])category=baccarat(?:&|$)/.test(location.hash || "");
    const walletEl = document.querySelector('[data-role="header-balance"]')
      || document.querySelector('[data-role="footer-balance"]');
    const walletBalance = isBaccaratLobby
      ? ((walletEl || {}).textContent || "").replace(/\s+/g, " ").trim()
      : "";
    const tid = (() => {
      const m = /table_id=([^&]+)/.exec(location.href);
      return m ? decodeURIComponent(m[1]) : "";
    })();
    const evolution = !!document.querySelector('[data-role="table-tile"]');
    let out = null, kind = "";
    /* Đang ở TRONG bàn (URL có table_id, không phải sảnh danh sách): chỉ đọc nguồn hẹp —
     * canvas lịch sử và Bead-road — tuyệt đối không quét toàn bộ DOM/SVG. Trang bàn đang
     * phát video; quét nặng (querySelectorAll("*") + getComputedStyle từng phần tử) làm
     * nghẽn renderer, nhịp tim WebSocket tắt và EVO đá phiên “Bạn đã đăng xuất” (EV.7)
     * ngay giữa ván. */
    if (!evolution && tid) {
      const evo = evolutionRoadsUp();
      if (evo.roads.length) { out = evo; kind = "evo-canvas"; }
      else {
        const bead = beadRoads();
        if (bead.roads.length) { out = bead; kind = "evo-bead"; }
        else { out = evo; kind = "evo-canvas"; }
      }
    }
    if (out === null) {
      out = evolution ? evolutionRoads() : textRoads();
      kind = evolution ? "evo-canvas" : "text";
      if (!evolution && !out.roads.length) {
        const svg = svgRoads();
        const scanned = Math.max(out.scanned, svg.scanned);
        out = { roads: svg.roads, scanned };
        kind = "svg";
      }
      if (!evolution && !out.roads.length) {
        const bead = beadRoads();
        if (bead.roads.length) { out = bead; kind = "evo-bead"; }
      }
    }
    return {
      page: { url: location.origin + location.pathname, table_id: tid, title: (document.title || "").slice(0, 80), wallet_balance: walletBalance },
      tables: out.roads.map((r) => ({
        label: r.label || (out.roads.length === 1 && tid ? tid : "ban " + r.box.w),
        letters: r.letters, source: r.source, x: r.box.x, y: r.box.y,
        table_id: r.table_id || '', game_type: r.game_type || '', read_error: r.read_error || ''
      })),
      diag: { kind, scanned: out.scanned, roads: out.roads.length, frame: location.origin + location.pathname },
      longlong: longlongGrids()
    };
  };
})();
