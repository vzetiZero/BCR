# -*- coding: utf-8 -*-
"""Điều khiển chuột trên Chrome thật của bạn qua ChromeDriver + python-ghost-cursor.

Script này mở (hoặc gắn vào) Chrome bằng Selenium/ChromeDriver rồi di chuyển chuột
theo quỹ đạo Bezier + Fitts's law của thư viện ``python-ghost-cursor``. Mọi sự kiện
được gửi qua CDP ``Input.dispatchMouseEvent`` vào đúng tab, không dùng pyautogui nên
không bấm lạc sang cửa sổ khác.

Cách dùng
---------
    # 1) Mở Chrome (profile riêng của app) và vào trang bạn muốn
    python mouse_chromedriver.py open --url https://www.neo179.com/gamelobby/live

    # 2) Xem thông tin phần tử để lấy toạ độ / hộp
    python mouse_chromedriver.py probe --selector '[data-role="table-tile"]'

    # 3) Bấm theo selector (quỹ đạo ghost-cursor, điểm rơi ngẫu nhiên trong hộp)
    python mouse_chromedriver.py click --selector '[data-role="table-tile"]'

    # 4) Di chuột tới toạ độ tuyệt đối, không bấm
    python mouse_chromedriver.py move --x 640 --y 380

    # 5) Bấm toạ độ tuyệt đối nhiều lần (đặt chip)
    python mouse_chromedriver.py click --x 800 --y 600 --repeat 3

    # 6) Gắn vào Chrome đang mở sẵn cổng 9222 thay vì tự mở
    python mouse_chromedriver.py click --selector 'button' --attach

    # 7) Kiểm tra thư viện và driver đã sẵn sàng
    python mouse_chromedriver.py doctor
"""

import argparse
import json
import sys
import time

import browser as BR
import ghost_mouse as GM
import human_mouse as HM


# ---------------------------------------------------------------------------
# Kết nối Chrome
# ---------------------------------------------------------------------------
def open_session(args):
    """Mở Chrome bằng ChromeDriver (profile của app) hoặc gắn vào Chrome có sẵn."""
    session = BR.BrowserSession()
    if args.attach:
        if not session.attach():
            raise SystemExit("Không gắn được vào Chrome ở cổng %s: %s"
                             % (BR.CDP_PORT, session.note))
        print("Đã gắn Chrome:", session.note)
        return session
    url = args.url or BR.START_URL
    if not BR.valid_site_url(url):
        raise SystemExit("URL không hợp lệ (cần https công khai): %s" % url)
    ok = session.start(url, pos=(args.left, args.top), cdp=True,
                       window=BR.normalize_window((args.width, args.height)))
    if not ok:
        raise SystemExit("Không mở được Chrome: %s" % session.note)
    print("Đã mở Chrome:", session.driver_info or session.note)
    if session.wait_for_cdp():
        print("CDP sẵn sàng ở cổng %s." % BR.CDP_PORT)
    return session


def cdp_command(session):
    """Trả hàm gửi một lệnh CDP tới tab đang điều khiển (dùng WebSocket ngắn hạn)."""
    import websocket

    page = session._cdp_page()
    if page is None:
        raise SystemExit("Không thấy tab nào để điều khiển.")
    ws = websocket.create_connection(
        BR.validated_endpoint(page), suppress_origin=True, timeout=BR.SCRIPT_TIMEOUT,
        redirect_limit=0, http_no_proxy=["127.0.0.1", "localhost", "::1"])
    state = {"seq": 0}

    def command(method, params=None):
        state["seq"] += 1
        ws.send(json.dumps({"id": state["seq"], "method": method, "params": params or {}}))
        while True:
            message = json.loads(ws.recv())
            if message.get("id") == state["seq"]:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", "cdp_error"))
                return message.get("result", {})

    # Runtime.enable để tab có execution context mặc định cho Runtime.evaluate.
    command("Runtime.enable")
    return ws, command


def eval_js(command, expression):
    """Chạy JS trong tab và trả giá trị thô."""
    result = command("Runtime.evaluate", {
        "expression": expression, "returnByValue": True, "awaitPromise": True,
        "silent": True, "timeout": BR.SCRIPT_TIMEOUT * 1000})
    return (result.get("result") or {}).get("value")


def element_box(command, selector):
    """Hộp (x, y, width, height) của phần tử đầu tiên khớp selector."""
    expression = (
        "(() => { const el = document.querySelector(%s);"
        " if (!el) return null;"
        " el.scrollIntoView({block:'center', inline:'center'});"
        " const r = el.getBoundingClientRect();"
        " return {x: r.x, y: r.y, width: r.width, height: r.height,"
        " text: (el.textContent||'').trim().slice(0,80)}; })()" % json.dumps(selector))
    box = eval_js(command, expression)
    if not box or box.get("width", 0) < 2 or box.get("height", 0) < 2:
        raise SystemExit("Không thấy phần tử hiển thị khớp %r." % selector)
    return box


# ---------------------------------------------------------------------------
# Hành động
# ---------------------------------------------------------------------------
def do_click(mouse, target, repeat, button):
    for index in range(max(1, repeat)):
        mouse.click(target[0], target[1], button=button)
        if index + 1 < repeat:
            time.sleep(0.12 + 0.1 * random_frac())


def random_frac():
    import random
    return random.random()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Điều khiển chuột qua ChromeDriver bằng python-ghost-cursor.")
    parser.add_argument("action",
                        choices=("doctor", "open", "probe", "click", "move"),
                        help="doctor: kiểm tra; open: mở Chrome; probe: xem hộp phần tử; "
                             "click/move: thao tác chuột")
    parser.add_argument("--url", default=None, help="URL mở đầu (mặc định theo app)")
    parser.add_argument("--attach", action="store_true",
                        help="Gắn vào Chrome đang mở cổng 9222 thay vì tự mở")
    parser.add_argument("--selector", default=None, help="CSS selector để bấm/gỡ hộp")
    parser.add_argument("--x", type=float, default=None, help="toạ độ X tuyệt đối")
    parser.add_argument("--y", type=float, default=None, help="toạ độ Y tuyệt đối")
    parser.add_argument("--repeat", type=int, default=1, help="số lần bấm")
    parser.add_argument("--button", default="left", choices=("left", "right", "middle"))
    parser.add_argument("--padding", type=float, default=12.0,
                        help="%% lề trong hộp phần tử khi chọn điểm rơi")
    parser.add_argument("--no-human", action="store_true",
                        help="tắt quỹ đạo người thật (chỉ 3 sự kiện chuột)")
    parser.add_argument("--style", default="ghost", choices=HM.STYLES,
                        help="kiểu quỹ đạo (mặc định ghost = python-ghost-cursor)")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--left", type=int, default=60)
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args(argv)

    if args.action == "doctor":
        print("ghost-cursor :", GM.status_note())
        print("chrome       :", BR.chrome_path())
        print("chrome ver   :", BR.chrome_version())
        print("chromedriver :", BR.driver_version())
        kind, why = BR.driver_for()
        print("driver mode  :", kind, "-", why)
        print("styles       :", ", ".join(HM.STYLES))
        sample = GM.ghost_path((100, 200), (640, 380))
        print("path sample  : %s điểm, kết thúc %s"
              % (len(sample) if sample else "?", sample[-1] if sample else "?"))
        return 0

    session = open_session(args)
    if args.action == "open":
        # Giữ tiến trình sống để Chrome không bị driver đóng khi script kết thúc.
        print("Chrome đang mở. Thao tác bằng terminal khác với --attach, "
              "hoặc nhấn Ctrl+C để đóng.")
        try:
            while True:
                time.sleep(30)
        except KeyboardInterrupt:
            print("\nĐang đóng Chrome…")
        finally:
            session.stop()
        return 0
    ws, command = cdp_command(session)
    try:
        if args.action == "probe":
            if not args.selector:
                raise SystemExit("probe cần --selector.")
            box = element_box(command, args.selector)
            print("hộp:", {k: (round(v, 1) if isinstance(v, float) else v)
                           for k, v in box.items()})
            return 0

        mouse = HM.HumanMouse(command, style=args.style, enabled=not args.no_human)
        if args.selector:
            box = element_box(command, args.selector)
            # Điểm rơi ngẫu nhiên trong hộp (giống getRandomBoxPoint của ghost-cursor),
            # nên mỗi lần chạy không rơi đúng một pixel.
            target = GM.ghost_target_box(box, padding_percent=args.padding)
        elif args.x is not None and args.y is not None:
            target = (args.x, args.y)
        else:
            raise SystemExit("Cần --selector hoặc cả --x và --y.")

        target = (round(target[0], 1), round(target[1], 1))
        if args.action == "move":
            points = mouse.move_to(*target)
            print("Đã di chuột qua %d điểm tới %s" % (len(points), target))
        else:
            do_click(mouse, target, args.repeat, args.button)
            print("Đã bấm %d lần tại %s (%s)" % (max(1, args.repeat), target, args.style))
    finally:
        try:
            ws.close()
        except Exception:
            pass
        session.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
