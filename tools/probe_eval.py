# -*- coding: utf-8 -*-
"""Chạy một đoạn JS tuỳ ý trong đúng context của bàn LONG-LONG (chỉ đọc).

Dùng để dò tiếp DOM mà không phải viết lại script:
    python probe_eval.py doan_js.js
    python probe_eval.py doan_js.js --raw     # in nguyên JSON trả về
"""

import json
import sys
from pathlib import Path

import probe_longlong as P


def find_best():
    for item in P.targets():
        if item.get("type") not in ("page", "iframe"):
            continue
        url = item.get("url") or ""
        if "/frontend/evo/" not in url and "evo" not in url:
            continue
        ws_url = item.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        ws = None
        try:
            ws = P.open_ws(ws_url)
            ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
            P.pump(ws, 1.2)
            for context_id in sorted(P.contexts_from_events()):
                value = P.evaluate(ws, P.SUMMARY_JS, context_id)
                if isinstance(value, dict) and value.get("spots"):
                    return item, ws_url, context_id, value
        except Exception:
            pass
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
    return None, None, None, None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    script = Path(sys.argv[1])
    source = script.read_text(encoding="utf-8")
    item, ws_url, context_id, summary = find_best()
    if ws_url is None:
        print("Không thấy context của bàn (đã vào bàn chưa?).")
        return 1
    print("context", context_id, "->", summary.get("url"))
    ws = None
    try:
        ws = P.open_ws(ws_url)
        ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
        P.pump(ws, 0.5)
        value = P.evaluate(ws, source, context_id, timeout=20)
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
