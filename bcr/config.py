# -*- coding: utf-8 -*-
"""Hằng số dùng chung: thời gian chờ, ngưỡng dữ liệu, nhận diện bàn đặc biệt.

Mọi tầng (Model · Adapter · Controller · Service) import từ đây để nói cùng một
thang số; không chứa màu sắc hay thiết lập giao diện.
"""

from pathlib import Path

# Thư mục gốc dự án (config.py nằm trong bcr/ nên lên một cấp).
HERE = Path(__file__).resolve().parents[1]

SCAN_SECONDS = 4
FAST_SCAN = 1.5
LIVE_STALE = 30
ENTRY_WAIT = 75
# Vòng tự tìm bàn không có trần số lần: nghỉ CYCLE_RETRY_SECONDS giữa hai lượt quét.
CYCLE_RETRY_SECONDS = 15
CHIP_REFRESH = 3
# Gate "chờ ván mới": sau khi chốt kế hoạch, chờ tối đa BET_GATE_TIMEOUT giây để bàn
# dọn hết chip (cả 3 cửa về 0) rồi mới tự bấm — tránh bấm chồng lên ván cũ còn đang
# hiển thị/gom chip. BET_GATE_POLL là nhịp đọc lại trong lúc chờ.
BET_GATE_TIMEOUT = 20.0
BET_GATE_POLL = 0.3
# Vào bàn từ sảnh mất vài giây tải trang, khay chip/ô cược chưa kịp hiện nên lượt bấm
# đầu thường hỏng. Tay chưa đặt được chip thật thì không được tính (xem
# LiveSimulation.mark_placed), nhưng bỏ quá nhiều tay liên tiếp nghĩa là bàn đang đặt
# không được chút nào — dừng để người dùng nhìn thấy thay vì bỏ tay mãi.
PLACE_SKIP_LIMIT = 3
# Trần thời gian một lượt bấm chip còn được coi là "đang chạy" (gate chờ ván mới 20s +
# vài giây bấm/đọc chip). Quá hạn mà chưa có xác nhận thì coi như không đặt được — nhờ
# vậy thread chết giữa chừng không khiến việc tính tay bị treo vĩnh viễn.
PLACE_CONFIRM_TIMEOUT = 60.0

# Bàn Siêu Tốc Độ (Super Speed) có cửa sổ đặt cược quá ngắn: chờ khay chip render, đọc
# mệnh giá, lượt chuột và xác nhận đã vượt cửa sổ nên tay luôn bị bỏ (not_placed).
# Không tự vào các bàn này khi săn bàn để khỏi đốt tay; người dùng vẫn tự mở được.
FAST_TABLE_MARKERS = ("siêu tốc", "super speed", "superspeed")
