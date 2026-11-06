# -*- coding: utf-8 -*-
"""Chuột "người thật" cho tab Chrome của profile BCR qua CDP.

Vì sao không dùng ghost-cursor / invisible_playwright / HumanCursor:
- ghost-cursor và invisible_playwright gắn với Playwright/Node, còn app này mở Chrome
  bằng Selenium + CDP nên không cần và không nên thêm cả một runtime khác.
- HumanCursor điều khiển chuột ở cấp hệ điều hành (pyautogui), có thể bấm lạc sang cửa sổ
  khác. Ở đây chỉ gửi Input.dispatchMouseEvent vào đúng tab sảnh nên an toàn hơn.

Module này giữ đúng tinh thần của windmouse/ghost-cursor:
- đường đi có gia tốc, gió ngẫu nhiên và vòng cung (không phải đường thẳng tắp);
- điểm cuối luôn trùng đúng tâm ô cần bấm (không lố);
- giữ chuột một nhịp ngắn rồi nhả, mô phỏng thao tác tay;
- chỉ dùng thư viện chuẩn, không thêm gói pip nào.
"""

import math
import random
import time

STYLES = ("windmouse", "bezier", "hybrid")
MAX_POINTS = 64


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

    def __init__(self, command, style="windmouse", glide_seconds=0.55,
                 enabled=True, rng=None):
        if style not in STYLES:
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

    def path(self, x, y):
        """Trả về danh sách điểm đã làm tròn 2 chữ số, luôn kết thúc đúng (x, y)."""
        if not (_finite(x) and _finite(y)):
            raise ValueError("toa_do_chuot_khong_hop_le")
        start = self._begin(float(x), float(y))
        style = self.style
        if style == "hybrid":
            style = "bezier" if self.rng.random() < 0.35 else "windmouse"
        if style == "bezier":
            points = bezier_points(start, (x, y), rng=self.rng)
        else:
            points = windmouse_points(start, (x, y), rng=self.rng)
        cleaned = [(round(max(0.0, px), 2), round(max(0.0, py), 2)) for px, py in points
                   if _finite(px) and _finite(py)]
        if not cleaned:
            cleaned = [(float(x), float(y))]
        cleaned[-1] = (round(float(x), 2), round(float(y), 2))
        return cleaned

    def move_to(self, x, y):
        """Lướt chuột tới đích theo đường cong, không bấm."""
        points = self.path(x, y)
        delay = max(0.004, min(0.025, self.glide_seconds / max(1, len(points))))
        for px, py in points:
            self._command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": px, "y": py})
            time.sleep(delay * self.rng.uniform(0.7, 1.3))
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
