# -*- coding: utf-8 -*-
"""Cầu nối tới thư viện ``python-ghost-cursor`` (áp dụng đúng logic trong
``C:\\Users\\Administrator\\Desktop\\mousse demo\\mouse_demo.py``).

Thư viện cài dưới tên module ``python_ghost_cursor`` và export sẵn hàm ``path()``
(không cần runtime Pyppeteer/Playwright để tính đường đi — phần đó chỉ dùng cho
lớp điều khiển cấp cao ``createCursor``, ta không dùng tới). ``path()`` sinh một
đường cong Bezier 3 điểm neo ngẫu nhiên rồi chia bước theo Fitts's law, đúng thuật
toán gốc của Xetera/ghost-cursor.
"""

import math
import random

try:
    from python_ghost_cursor import path as _ghost_path
    from python_ghost_cursor.shared._spoof import (
        should_overshoot as _ghost_should_overshoot,
    )
    from python_ghost_cursor.shared._math import (
        Vector as _GhostVector,
        overshoot as _ghost_overshoot,
    )
    _IMPORT_ERROR = ""
except Exception as exc:                     # pragma: no cover - phụ thuộc máy
    _ghost_path = None
    _IMPORT_ERROR = type(exc).__name__


def available():
    """Thư viện python-ghost-cursor đã nạp được chưa."""
    return _ghost_path is not None


def status_note():
    if available():
        return "ghost-cursor: bật (python-ghost-cursor, Bezier + Fitts)"
    return "ghost-cursor: không nạp được (%s) — dùng WindMouse nội bộ" % (_IMPORT_ERROR or "?")


def ghost_path(start, end, width=100.0, max_points=64):
    """Sinh quỹ đạo bằng đúng ``python_ghost_cursor.path()`` (như trong mouse_demo.py).

    ``start``/``end`` là ``(x, y)``. Trả danh sách ``(x, y)`` đã làm tròn, luôn kết
    thúc đúng ``end``; trả ``None`` nếu thư viện chưa nạp được (nơi gọi tự chuyển
    sang WindMouse nội bộ). ``width`` được giữ trong chữ ký để tương thích gọi cũ
    nhưng không truyền vào thư viện: bản ``get_path`` export chỉ nhận toạ độ,
    bề rộng mục tiêu dùng giá trị mặc định 100 ở tầng trong của thư viện.
    """
    if _ghost_path is None:
        return None
    sx, sy = float(start[0]), float(start[1])
    tx, ty = float(end[0]), float(end[1])
    if math.hypot(tx - sx, ty - sy) < 1.0:
        return [(tx, ty)]
    points = _ghost_path({"x": sx, "y": sy}, {"x": tx, "y": ty})
    points = points[:max_points] if len(points) > max_points else points
    out = [(round(max(0.0, float(p["x"])), 2), round(max(0.0, float(p["y"])), 2))
           for p in points]
    if not out:
        out = [(tx, ty)]
    out[-1] = (round(tx, 2), round(ty, 2))
    return out


def should_overshoot(start, end):
    """Chặng dài thì ghost-cursor đi lố rồi hiệu chỉnh lại, như tay người thật."""
    if _ghost_path is None:
        return math.hypot(float(end[0]) - float(start[0]),
                          float(end[1]) - float(start[1])) > 500.0
    return _ghost_should_overshoot(_GhostVector(*start), _GhostVector(*end))


def overshoot_point(end, radius=120.0, rng=None):
    """Điểm lố ngẫu nhiên quanh đích — dùng đúng hàm ``overshoot`` của thư viện."""
    if _ghost_path is not None:
        vector = _ghost_overshoot(_GhostVector(float(end[0]), float(end[1])), radius)
        return (vector.x, vector.y)
    uniform = random.random if rng is None else rng.random
    angle = uniform() * 2 * math.pi
    rad = radius * math.sqrt(uniform())
    return (float(end[0]) + rad * math.cos(angle), float(end[1]) + rad * math.sin(angle))


def ease_cosine(t):
    """Easing vào-chậm/ra-chậm giống ``move_mouse()`` trong mouse_demo.py."""
    return (1.0 - math.cos(math.pi * max(0.0, min(1.0, t)))) / 2.0


def resample(points, frames):
    """Nội suy ``points`` (đường ghost-cursor) thành đúng ``frames`` khung hình.

    Giống hệt vòng lặp trong ``move_mouse()`` của demo: mỗi khung lấy easing
    cosine theo thời gian, rồi nội suy tuyến tính giữa hai điểm liền kề trên
    đường cong — chuyển động đều theo thời gian thực chứ không đều theo số điểm.
    """
    if not points:
        return []
    if len(points) == 1 or frames <= 1:
        return [points[-1]]
    out = []
    last_index = len(points) - 2
    for frame in range(1, frames + 1):
        progress = ease_cosine(frame / frames)
        position = progress * (len(points) - 1)
        index = min(int(position), last_index)
        fraction = position - index
        ax, ay = points[index]
        bx, by = points[index + 1]
        out.append((ax + (bx - ax) * fraction, ay + (by - ay) * fraction))
    return out
