# -*- coding: utf-8 -*-
"""Dò DOM bàn LONG-LONG thật qua Chrome CDP 9222 rồi ghi ra longlong-probe.json.

Chẩn đoán, KHÔNG bấm cược: chỉ đọc trang đang mở để lấy đúng selector cho
``bcr/adapters/table_bets.py`` và ``bcr/adapters/lobby_stream.py``.

Sàn có nhiều lớp lồng nhau (portal -> iframe EVO -> iframe game), và bàn LONG-LONG
("DragonDragon") render bên trong iframe con. Vì vậy script:

    1. liệt kê mọi target CDP (page + iframe/OOPIF),
    2. bật Runtime và gom MỌI execution context của từng target,
    3. chấm điểm từng context (ô cược / khay chip / ô lịch sử),
    4. chụp chi tiết context thắng + frame tree để viết adapter.

Cách dùng:
    1. App -> "Mở Chrome" -> đăng nhập -> vào bàn LONG-LONG (đợi hết loading).
    2. Chạy:  python probe_longlong.py
"""

import json
import sys
import time
from pathlib import Path

from bcr.adapters import browser as BR

OUT = Path(__file__).resolve().parent / "longlong-probe.json"
EVENTS = []

SUMMARY_JS = r"""(() => {
  const count = (sel) => document.querySelectorAll(sel).length;
  const roles = {};
  document.querySelectorAll('[data-role]').forEach((el) => {
    const role = el.getAttribute('data-role');
    roles[role] = (roles[role] || 0) + 1;
  });
  return {
    url: location.href.slice(0, 300),
    title: (document.title || '').slice(0, 120),
    frames: window.frames.length,
    roles: roles,
    spots: count('[data-role*="bet" i], [class*="bet-spot" i], [class*="betspot" i]'),
    chips: count('[data-role*="chip" i], [class*="chip" i]'),
    grids: count('[data-role*="history" i], [class*="history" i]'),
    tiles: count('[data-role="table-tile"]'),
    canvas: count('canvas'),
    imgs: count('img'),
    body_len: (document.body ? document.body.innerText.length : 0),
  };
})()"""

PROBE_JS = r"""(() => {
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
  };
  const text = (el) => ((el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).slice(0, 120);
  const style = (el) => {
    const st = getComputedStyle(el);
    const media = el.querySelector('img,svg,canvas,picture');
    return {bg: st.backgroundColor || '', img: (st.backgroundImage || '').slice(0, 200),
            color: st.color || '',
            media: media ? (media.tagName + ':' + (media.getAttribute('src') || media.getAttribute('style') || '').slice(0, 160)) : ''};
  };
  const roles = {};
  document.querySelectorAll('[data-role]').forEach((el) => {
    const role = el.getAttribute('data-role');
    roles[role] = (roles[role] || 0) + 1;
  });
  const spots = [...document.querySelectorAll('[data-role*="bet" i], [class*="bet-spot" i], [class*="betspot" i], [class*="spot" i]')]
    .slice(0, 80)
    .map((el) => ({role: el.getAttribute('data-role'), cls: (el.className || '').toString().slice(0, 100),
                   text: text(el), box: box(el), style: style(el)}));
  const chips = [...document.querySelectorAll('[data-role*="chip" i], [class*="chip" i]')]
    .slice(0, 80)
    .map((el) => ({role: el.getAttribute('data-role'), value: el.getAttribute('data-value'),
                   selected: el.getAttribute('data-selected'),
                   cls: (el.className || '').toString().slice(0, 100), text: text(el),
                   box: box(el), style: style(el)}));
  const grids = [...document.querySelectorAll('[data-role*="history" i], [class*="history" i]')]
    .slice(0, 12)
    .map((grid) => ({
      role: grid.getAttribute('data-role'),
      cls: (grid.className || '').toString().slice(0, 100),
      box: box(grid),
      cells: [...grid.querySelectorAll('*')].filter((n) => n.children.length === 0).slice(0, 150)
        .map((n) => ({tag: n.tagName, text: text(n), aria: n.getAttribute('aria-label'),
                      title: n.getAttribute('title'), cls: (n.className || '').toString().slice(0, 80),
                      style: style(n)})),
    }));
  const buttons = [...document.querySelectorAll('button, [role="button"], [class*="button" i]')]
    .slice(0, 60)
    .map((el) => ({text: text(el).slice(0, 40), cls: (el.className || '').toString().slice(0, 60),
                   role: el.getAttribute('data-role'), disabled: !!el.disabled, box: box(el)}));
  return {url: location.href.slice(0, 300), title: (document.title || '').slice(0, 120),
          roles: roles, spots: spots, chips: chips, grids: grids, buttons: buttons};
})()"""


OUTLINE_JS = r"""(() => {
  const out = [];
  const walk = (el, depth) => {
    if (out.length > 400 || depth > 14) return;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const own = [...el.childNodes].filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim()).join(' ').replace(/\s+/g, ' ').slice(0, 40);
    if (r.width > 2 && r.height > 2) {
      out.push({d: depth, tag: el.tagName, role: el.getAttribute('data-role'),
                cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60),
                text: own, bg: st.backgroundColor, color: st.color,
                bgimg: (st.backgroundImage || '').slice(0, 120),
                box: {x: Math.round(r.x), y: Math.round(r.y),
                      w: Math.round(r.width), h: Math.round(r.height)}});
    }
    for (const child of el.children) walk(child, depth + 1);
  };
  walk(document.body, 0);
  return {nodes: out};
})()"""


def evaluate(ws, expression, context_id=None, timeout=8.0):
    request_id = int(time.time() * 1000) % 1000000
    params = {"expression": expression, "returnByValue": True, "awaitPromise": False}
    if context_id:
        params["contextId"] = context_id
    ws.send(json.dumps({"id": request_id, "method": "Runtime.evaluate", "params": params}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reply = json.loads(ws.recv())
        except Exception:
            return None
        if reply.get("method"):
            EVENTS.append(reply)
            continue
        if reply.get("id") != request_id:
            continue
        return (reply.get("result") or {}).get("result", {}).get("value")
    return None


def pump(ws, seconds=2.0):
    """Đọc sự kiện (executionContextCreated, frameNavigated...) trong một khoảng ngắn."""
    deadline = time.monotonic() + seconds
    ws.settimeout(0.4)
    while time.monotonic() < deadline:
        try:
            reply = json.loads(ws.recv())
        except Exception:
            continue
        if reply.get("method"):
            EVENTS.append(reply)


def contexts_from_events():
    """[(contextId, origin, frameId, isDefault)] từ Runtime.executionContextCreated."""
    found = {}
    for event in EVENTS:
        if event.get("method") != "Runtime.executionContextCreated":
            continue
        context = (event.get("params") or {}).get("context") or {}
        aux = context.get("auxData") or {}
        found[context.get("id")] = (context.get("origin", ""), aux.get("frameId", ""),
                                    bool(aux.get("isDefault")))
    return found


def frame_tree_urls():
    urls = []
    for event in EVENTS:
        if event.get("method") != "Page.frameNavigated":
            continue
        frame = (event.get("params") or {}).get("frame") or {}
        urls.append((frame.get("id", "")[:8], frame.get("url", "")[:160]))
    return urls


def targets():
    try:
        return BR.BrowserSession._cdp_json("list")
    except Exception as exc:
        print("Không đọc được danh sách tab CDP 9222:", type(exc).__name__)
        return []


def open_ws(url):
    import websocket

    return websocket.create_connection(url, suppress_origin=True, timeout=BR.SCRIPT_TIMEOUT,
                                       redirect_limit=0,
                                       http_no_proxy=["127.0.0.1", "localhost", "::1"])


SNAPSHOT_JS = r"""(() => {
  const text = (el) => ((el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).slice(0, 60);
  const cls = (el) => (typeof el.className === 'string' ? el.className : '');
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return Math.round(r.x) + ',' + Math.round(r.y) + ',' + Math.round(r.width) + 'x' + Math.round(r.height);
  };
  const spot = (role) => {
    const el = document.querySelector('[data-role="' + role + '"]');
    if (!el) return null;
    return {role: role, aria: el.getAttribute('aria-label'), cls: cls(el),
            text: text(el), box: box(el), hidden: el.getAttribute('data-hidden')};
  };
  const result = document.querySelector('[data-role="result"]');
  const status = document.querySelector('[data-role="status-text"]');
  const timer = document.querySelector('[data-role="linear-timer"]');
  const spots = ['JNS_A', 'JNS_B', 'JNS_C'].map(spot);
  // chip của tôi trên từng ô
  const mine = {};
  ['JNS_A', 'JNS_B', 'JNS_C'].forEach((role) => {
    const holder = document.querySelector('[data-role="betting-spot-' + role + '-chip-container"]');
    mine[role] = holder ? holder.querySelectorAll('[data-role]').length : 0;
    if (holder) mine[role] = holder.querySelectorAll('*').length;
  });
  const round_roles = () => {
    const out = {};
    document.querySelectorAll('[data-role]').forEach((el) => {
      const role = el.getAttribute('data-role') || '';
      if (/round|hand|shoe|phase|timer|status|clock|countdown/i.test(role)) {
        out[role] = text(el, 40);
      }
    });
    return out;
  };
  const round_text = () => {
    const all = [...document.querySelectorAll('div,span')];
    for (const el of all) {
      const value = text(el, 24);
      if (/^#\s*\d/.test(value)) return value;
    }
    return null;
  };
  return {
    t: Date.now(),
    status: status ? text(status) : null,
    timer: timer ? box(timer) : null,
    result_html: result ? (result.outerHTML || '').replace(/\s+/g, ' ').slice(0, 400) : null,
    result_cls: result ? cls(result) : null,
    spots: spots,
    mine_chips: mine,
    balance: (document.querySelector('[data-role="balance-label-value"]') || {}).innerText || null,
    round_roles: round_roles(),
    round_text: round_text(),
  };
})()"""


def game_context():
    """Tìm (ws_url, context_id) của context có ô cược LONG-LONG."""
    for item in targets():
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
            ws = open_ws(ws_url)
            ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
            pump(ws, 1.2)
            for context_id in sorted(contexts_from_events()):
                value = evaluate(ws, SUMMARY_JS, context_id)
                if isinstance(value, dict) and value.get("spots"):
                    return ws_url, context_id
        except Exception:
            pass
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
    return None, None


def watch(seconds=120.0, out_name="longlong-probe-live.json"):
    """Ghi snapshot trạng thái bàn mỗi giây để bắt đúng lúc ván trả kết quả."""
    ws_url, context_id = game_context()
    if ws_url is None:
        print("Không thấy context bàn LONG-LONG (đã vào bàn chưa?).")
        return 1
    print("context", context_id, "— ghi snapshot %.0fs…" % seconds)
    ws = None
    shots = []
    try:
        ws = open_ws(ws_url)
        ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
        pump(ws, 0.6)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = evaluate(ws, SNAPSHOT_JS, context_id, timeout=5)
            if isinstance(value, dict):
                shots.append(value)
                status = (value.get("status") or "")[:28]
                changed = ""
                if len(shots) > 1 and value.get("result_html") != shots[-2].get("result_html"):
                    changed = "  <-- result DOI"
                elif len(shots) > 1 and [s.get("cls") for s in value.get("spots") or []] != [
                        s.get("cls") for s in shots[-2].get("spots") or []]:
                    changed = "  <-- spot DOI class"
                print("  %-28s result=%-22s%s" % (status,
                      (value.get("result_html") or "")[:22], changed))
            time.sleep(1.0)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    path = Path(__file__).resolve().parent / out_name
    path.write_text(json.dumps(shots, ensure_ascii=False, indent=1), encoding="utf-8")
    print("Đã ghi %s (%d snapshot)" % (out_name, len(shots)))
    return 0


def ws_capture(seconds=45.0, out_name="longlong-probe-ws.json"):
    """Bắt frame WebSocket của bàn (chỉ đọc) để tìm chỗ sàn công bố kết quả.

    EVO đẩy kết quả ván qua WebSocket; bắt được frame là biết ngay key nào chứa
    A/B/C. Chạy khi đang ở trong bàn và để yên cho ván kết thúc.
    """
    items = targets()
    best = None
    # Ưu tiên iframe game (/frontend/evo/...): đó là nơi phát sinh traffic WebSocket.
    for item in items:
        url = item.get("url") or ""
        if item.get("type") == "iframe" and "/frontend/evo/" in url \
                and item.get("webSocketDebuggerUrl"):
            best = item
            break
    if best is None:
        for item in items:
            url = item.get("url") or ""
            if item.get("type") in ("page", "iframe") and "evo" in url \
                    and item.get("webSocketDebuggerUrl"):
                best = item
                break
    if best is None:
        print("Không thấy tab game để bắt WebSocket.")
        return 1
    print("Bắt trên target:", best.get("type"), (best.get("url") or "")[:90])
    ws = None
    frames = []
    try:
        ws = open_ws(best["webSocketDebuggerUrl"])
        ws.send(json.dumps({"id": 0, "method": "Network.enable"}))
        print("Đang bắt WebSocket %.0fs — để yên cho ván chạy…" % seconds)
        deadline = time.monotonic() + seconds
        ws.settimeout(1.0)
        while time.monotonic() < deadline:
            try:
                reply = json.loads(ws.recv())
            except Exception:
                continue
            if reply.get("method") != "Network.webSocketFrameReceived":
                continue
            payload = ((reply.get("params") or {}).get("response") or {}).get("payloadData") or ""
            if payload:
                frames.append(payload[:20000])
                print("  + frame %d ký tự" % len(payload))
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    path = Path(__file__).resolve().parent / out_name
    path.write_text(json.dumps(frames, ensure_ascii=False, indent=1)[:20_000_000],
                    encoding="utf-8")
    print("Đã ghi %s (%d frame)" % (out_name, len(frames)))
    return 0


def main():
    if "--ws" in sys.argv:
        index = sys.argv.index("--ws")
        seconds = float(sys.argv[index + 1]) if len(sys.argv) > index + 1 else 45.0
        return ws_capture(seconds)
    if "--watch" in sys.argv:
        index = sys.argv.index("--watch")
        seconds = float(sys.argv[index + 1]) if len(sys.argv) > index + 1 else 120.0
        return watch(seconds)
    items = targets()
    if not items:
        print("Chưa thấy Chrome ở cổng 9222. Trong app bấm “Mở Chrome” rồi vào bàn LONG-LONG.")
        return 1
    pages = [item for item in items
             if item.get("type") in ("page", "iframe")
             and isinstance(item.get("url"), str) and "127.0.0.1" not in item["url"]]
    report = {"targets": [], "contexts": [], "best": None}
    best_score, best = -1, None
    print("Target: %d" % len(pages))
    for item in pages:
        ws_url = item.get("webSocketDebuggerUrl")
        info = {"type": item.get("type"), "url": (item.get("url") or "")[:200]}
        if not ws_url:
            info["error"] = "không có webSocketDebuggerUrl"
            report["targets"].append(info)
            continue
        ws = None
        try:
            ws = open_ws(ws_url)
            ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
            ws.send(json.dumps({"id": 0, "method": "Page.enable"}))
            pump(ws, 1.5)
            contexts = contexts_from_events()
            info["contexts"] = len(contexts)
            info["frames"] = frame_tree_urls()
            # context mặc định (id nhỏ nhất / isDefault) và toàn bộ context con
            for context_id, (origin, frame_id, is_default) in sorted(contexts.items()):
                summary = evaluate(ws, SUMMARY_JS, context_id)
                if not isinstance(summary, dict):
                    continue
                score = (summary.get("spots", 0) + summary.get("chips", 0)
                         + 3 * summary.get("grids", 0) + summary.get("tiles", 0))
                record = {"target": info["type"], "context": context_id, "origin": origin[:120],
                          "frame": frame_id[:8], "default": is_default, "score": score,
                          "url": summary.get("url"), "body_len": summary.get("body_len"),
                          "spots": summary.get("spots"), "chips": summary.get("chips"),
                          "grids": summary.get("grids"), "canvas": summary.get("canvas"),
                          "roles": summary.get("roles")}
                report["contexts"].append(record)
                print("  ctx %-3s %-28s body=%-6s spots=%-3s chips=%-3s grids=%-3s canvas=%-2s" % (
                    context_id, (summary.get("url") or summary.get("origin") or "")[:28],
                    summary.get("body_len"), summary.get("spots"), summary.get("chips"),
                    summary.get("grids"), summary.get("canvas")))
                if score > best_score:
                    best_score, best = score, (item, ws_url, context_id, summary.get("url"))
        except Exception as exc:
            info["error"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        report["targets"].append(info)

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if best is None:
        print("Không context nào đọc được tài liệu. Bàn đã tải xong chưa?")
        return 1

    item, ws_url, context_id, url = best
    print("\nContext tốt nhất:", context_id, "->", url)
    ws = None
    try:
        ws = open_ws(ws_url)
        ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
        pump(ws, 0.6)
        detail = evaluate(ws, PROBE_JS, context_id)
        outline = evaluate(ws, OUTLINE_JS, context_id)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    report["best"] = {"target": item.get("type"), "context": context_id,
                      "detail": detail, "outline": outline}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Đã ghi", OUT.name)
    if isinstance(detail, dict):
        print("data-role:", ", ".join("%s×%d" % (k, v) for k, v in sorted(detail["roles"].items()))[:400])
        print("ô cược:", len(detail.get("spots") or []), "· chip:", len(detail.get("chips") or []),
              "· khối lịch sử:", len(detail.get("grids") or []),
              "· nút:", len(detail.get("buttons") or []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
