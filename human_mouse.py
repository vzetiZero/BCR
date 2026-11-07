# -*- coding: utf-8 -*-
"""Chuột "người thật" cho tab Chrome của profile BCR qua CDP / ChromeDriver.

Ba kiểu quỹ đạo:
- ``ghost``: dùng đúng toán học của thư viện ``python-ghost-cursor`` (Bezier ba
  điểm neo + Fitts's law + overshoot) rồi gửi qua CDP. Đây là kiểu mặc định khi
  gói đã cài.
- ``windmouse``: WindMouse (Ben Land) tự cài, không cần gói ngoài.
- ``bezier``: cung Bezier đơn giản tự cài.
- ``hybrid``: trộn ngẫu nhiên bezier/windmouse.

Vì sao không dùng thẳng ``GhostCursor.click``: lớp đó gắn với ``Page`` của
Puppeteer. Ở đây app mở Chrome bằng Selenium (ChromeDriver) và gửi sự kiện qua
``Input.dispatchMouseEvent``, nên module này tái dùng phần sinh quỹ đạo và tự
bấm — không cần thêm runtime Puppeteer.

Mọi kiểu đều đảm bảo: điểm cuối trùng đúng tâm đích, chỉ gửi sự kiện vào đúng
tab đang điều khiển (không bấm lạc sang cửa sổ khác như pyautogui).
"""

import math
import random
import time

import ghost_mouse as GM

STYLES = ("ghost", "windmouse", "bezier", "hybrid")
MAX_POINTS = 64


CURSOR_OVERLAY_JS = r"""
(() => {
  if (window.__bcrCursorInstalled) return;
  window.__bcrCursorInstalled = true;
  const install = () => {
    const cursor = document.createElement('div');
    cursor.id = '__bcr_ghost_cursor__';
    cursor.setAttribute('aria-hidden', 'true');
    cursor.style.cssText = 'position:fixed!important;left:0!important;top:0!important;'
      + 'width:26px!important;height:34px!important;pointer-events:none!important;'
      + 'z-index:2147483647!important;display:none;'
      + 'filter:drop-shadow(0 2px 3px rgba(0,0,0,.3))!important;';
    const shadow = cursor.attachShadow({mode: 'open'});
    shadow.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="26" height="34" viewBox="0 0 26 34">'
      + '<path d="M2 2 L2 26 L8 20 L13 31 L18 29 L13 18 L23 18 Z" '
      + 'fill="#19b98e" stroke="white" stroke-width="2" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(cursor);
    document.addEventListener('mousemove', event => {
      cursor.style.display = 'block';
      cursor.style.transform = `translate(${event.clientX - 2}px, ${event.clientY - 2}px)`;
    }, {capture: true, passive: true});
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install, {once: true});
  } else {
    install();
  }
})();
"""
# Lớp hiển thị con trỏ xanh trên trang — nguyên văn kịch bản trong mouse_demo.py
# (C:\Users\Administrator\Desktop\mousse demo). Chỉ vẽ một lớp overlay theo
# mousemove thật; không chiếm quyền chuột hệ điều hành, không đổi dữ liệu gửi đi.
# Gọi install_cursor_overlay(command) một lần sau khi trang đã có DOM.


def install_cursor_overlay(command):
    """Cài con trỏ hiển thị vào tab hiện tại và mọi lần tải lại sau đó.

    ``command`` là hàm gửi lệnh CDP (giống tham số của ``HumanMouse``).
    Dùng ``Page.addScriptToEvaluateOnNewDocument`` để con trỏ còn hiển thị sau khi
    trang điều hướng/tải lại, giống ``install_cursor()`` trong mouse_demo.py.
    """
    try:
        command("Page.addScriptToEvaluateOnNewDocument", {"source": CURSOR_OVERLAY_JS})
    except Exception:
        pass
    try:
        command("Runtime.evaluate", {"expression": CURSOR_OVERLAY_JS})
    except Exception:
        pass




def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def windmouse_points(start, end, gravity=9.0, wind=3.0, max_step=10.0,
                     target_area=8.0, rng=None):
    """Sinh các điểm theo thuật toán WindMouse (Ben Land) từ start tới end.

    max_step được nới theo quãng đường để chặng dài không tạo hàng trăm sự kiện CDP.
    Điểm cuối luôn được ghim đúng tâm đích.
    """
    rng = rng or random.Random()
    sx, sy = float(start[0]), float(start[1])
    tx, ty = float(end[0]), float(end[1])
    distance = math.hypot(tx - sx, ty - sy)
    if distance < 6.0:
        return [(tx, ty)]
    # Chặng ngắn phải bước nhỏ, nếu không lực hút sẽ vượt qua đích rồi dao động.
    step_ceiling = min(28.0, max(float(max_step), distance / 20.0))
    max_step = max(0.8, min(step_ceiling, distance / 12.0))
    target_area = min(float(target_area), max(1.5, distance / 20.0))
    vx = vy = wx = wy = 0.0
    x, y = sx, sy
    points = []
    # Giữ một chỗ cho điểm đích chính xác, nhờ vậy không bao giờ vượt MAX_POINTS.
    while len(points) < MAX_POINTS - 1:
        remaining = math.hypot(tx - x, ty - y)
        if remaining <= 0.8:
            break
        gust = min(float(wind), remaining)
        if remaining >= target_area:
            wx += rng.uniform(-gust, gust) + gravity * (tx - x) / remaining
            wy += rng.uniform(-gust, gust) + gravity * (ty - y) / remaining
        else:
            # Vào vùng đích thì giảm lực để không lao lố.
            wx /= math.sqrt(3.0)
            wy /= math.sqrt(3.0)
            max_step = max(0.5, max_step / math.sqrt(5.0))
        vx += wx
        vy += wy
        speed = math.hypot(vx, vy)
        if speed > max_step:
            vx *= max_step / speed
            vy *= max_step / speed
        x += vx
        y += vy
        points.append((x, y))
    if not points or math.hypot(points[-1][0] - tx, points[-1][1] - ty) > 0.5:
        points.append((tx, ty))
    else:
        points[-1] = (tx, ty)
    return points


def _ease(t):
    """Vào chậm - nhanh - ra chậm giống tay người kéo chuột."""
    return 0.5 - math.cos(math.pi * max(0.0, min(1.0, t))) / 2.0


def bezier_points(start, end, steps=28, rng=None):
    """Đường cong Bezier kiểu ghost-cursor: cung hơi lệch thay vì đường thẳng."""
    rng = rng or random.Random()
    sx, sy = float(start[0]), float(start[1])
    tx, ty = float(end[0]), float(end[1])
    dx, dy = tx - sx, ty - sy
    distance = math.hypot(dx, dy)
    if distance < 1.0:
        return [(tx, ty)]
    if distance > 0:
        nx, ny = -dy / distance, dx / distance
    else:
        nx, ny = 0.0, 0.0
    bend1 = rng.uniform(-0.20, 0.20) * distance
    bend2 = rng.uniform(-0.16, 0.16) * distance
    c1x, c1y = sx + dx * 0.33 + nx * bend1, sy + dy * 0.33 + ny * bend1
    c2x, c2y = sx + dx * 0.66 + nx * bend2, sy + dy * 0.66 + ny * bend2
    steps = max(8, min(MAX_POINTS, int(steps)))
    points = []
    for index in range(1, steps + 1):
        t = _ease(index / steps)
        u = 1.0 - t
        x = u ** 3 * sx + 3 * u ** 2 * t * c1x + 3 * u * t ** 2 * c2x + t ** 3 * tx
        y = u ** 3 * sy + 3 * u ** 2 * t * c1y + 3 * u * t ** 2 * c2y + t ** 3 * ty
        points.append((x, y))
    points[-1] = (tx, ty)
    return points


class HumanMouse:
    """Gửi chuỗi mouseMoved + mousePressed/mouseReleased qua một hàm command CDP."""

    def __init__(self, command, style="ghost", glide_seconds=0.55,
                 enabled=True, rng=None):
        if style not in STYLES:
            style = "ghost"
        # Tự hạ cấp nếu gói ghost-cursor chưa cài.
        if style == "ghost" and not GM.available():
            style = "windmouse"
        self._command = command
        self.style = style
        self.glide_seconds = max(0.05, float(glide_seconds))
        self.enabled = bool(enabled)
        self.rng = rng or random.Random()
        self.last = None


    def _begin(self, x, y):
        """Điểm xuất phát: lần bấm đầu lấy một vị trí hợp lý lệch lên trên/trái đích."""
        if self.last is not None:
            return self.last
        return (max(2.0, x - self.rng.uniform(120.0, 520.0)),
                max(2.0, y - self.rng.uniform(80.0, 320.0)))

    def path(self, x, y, width=100.0):
        """Trả về danh sách điểm đã làm tròn 2 chữ số, luôn kết thúc đúng (x, y)."""
        if not (_finite(x) and _finite(y)):
            raise ValueError("toa_do_chuot_khong_hop_le")
        start = self._begin(float(x), float(y))
        style = self.style
        if style == "hybrid":
            style = "bezier" if self.rng.random() < 0.35 else "windmouse"
        if style == "ghost":
            points = self._ghost_path(start, (x, y), width)
            if points is None:                     # gói hỏng giữa phiên
                points = windmouse_points(start, (x, y), rng=self.rng)
        elif style == "bezier":
            points = bezier_points(start, (x, y), rng=self.rng)
        else:
            points = windmouse_points(start, (x, y), rng=self.rng)
        cleaned = [(round(max(0.0, px), 2), round(max(0.0, py), 2)) for px, py in points
                   if _finite(px) and _finite(py)]
        if not cleaned:
            cleaned = [(float(x), float(y))]
        cleaned[-1] = (round(float(x), 2), round(float(y), 2))
        return cleaned

    def _ghost_path(self, start, end, width):
        """Quỹ đạo ghost-cursor; chặng dài thì đi lố rồi hiệu chỉnh như tay người."""
        goal = end
        if GM.should_overshoot(start, end):
            goal = GM.overshoot_point(end, rng=self.rng)
        points = GM.ghost_path(start, goal, width=width, max_points=MAX_POINTS)
        if points is None:
            return None
        if goal is not end:
            # Hiệu chỉnh: từ điểm lố về đúng đích, đúng tinh thần ghost-cursor.
            correction = GM.ghost_path(goal, end, width=max(20.0, width / 4),
                                       max_points=MAX_POINTS)
            if correction:
                points = points + correction
        return points


    def move_to(self, x, y):
        """Lướt chuột tới đích theo easing cosine, đúng nhịp move_mouse() trong demo.

        Đường cong ghost-cursor cho vị trí; ``resample`` chia theo THỜI GIAN thực
        (60 khung/giây, easing vào-chậm/ra-chậm) thay vì chia đều theo số điểm của
        đường cong — chuyển động mượt bất kể đường cong có bao nhiêu điểm.
        """
        raw_points = self.path(x, y)
        frames = max(2, round(self.glide_seconds * 60))
        points = GM.resample(raw_points, frames)
        delay = self.glide_seconds / max(1, len(points))
        for px, py in points:
            self._command("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": round(px, 2), "y": round(py, 2)})
            time.sleep(delay * self.rng.uniform(0.85, 1.15))
        self.last = (float(x), float(y))
        return points

    def _tap(self, x, y, button="left", click_count=1):
        """Đường dự phòng khi tắt chuột người thật: đúng 3 sự kiện như bản cũ."""
        for event in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": event, "x": x, "y": y}
            if event != "mouseMoved":
                params.update(button=button, clickCount=click_count, buttons=1 if event == "mousePressed" else 0)
            self._command("Input.dispatchMouseEvent", params)
        self.last = (float(x), float(y))

    def click(self, x, y, button="left", click_count=1):
        """Bấm một điểm: lướt tới nơi, dừng rất ngắn rồi nhấn - giữ - nhả."""
        if not (_finite(x) and _finite(y)):
            raise ValueError("toa_do_chuot_khong_hop_le")
        click_count = 1 if int(click_count) < 1 else min(2, int(click_count))
        x, y = float(x), float(y)
        if not self.enabled:
            self._tap(x, y, button, click_count)
            return
        self.move_to(x, y)
        # Đưa chuột đúng tâm lần cuối: đường cong đã kết thúc ở đích, đây là nhịp "chỉnh tay".
        self._command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        time.sleep(self.rng.uniform(0.03, 0.09))
        for index in range(click_count):
            self._command("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y, "button": button,
                "buttons": 1, "clickCount": index + 1,
            })
            time.sleep(self.rng.uniform(0.04, 0.11))
            self._command("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y, "button": button,
                "buttons": 0, "clickCount": index + 1,
            })
            if index + 1 < click_count:
                time.sleep(self.rng.uniform(0.06, 0.12))
        self.last = (x, y)
