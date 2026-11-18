# -*- coding: utf-8 -*-
"""Harness “test thực tế ở mức chọn bàn” cho nút duy nhất VÀO BÀN HỢP LỆ.

Không cần Chrome/đăng nhập: thay Chrome + sảnh bằng đối tượng giả nhưng chạy đúng
pipeline thật của controller (dispatch → thread nền → hàng đợi sự kiện → lọc bàn →
bấm ô bàn → xác nhận cầu). Chỉ rút ngắn *nhịp chờ* (quét 4 s, săn lại 15 s) để test
chạy nhanh; không bỏ qua bước nào.

Khoá lại 5 điều:

    1. Chỉ một lệnh ``table.enterValid`` là đủ để đi từ sảnh tới đúng bàn đạt chuẩn.
    2. Bàn không đạt bị loại, bàn Siêu Tốc Độ bị bỏ, bàn đạt được chọn.
    3. Vào bàn xong thì ĐỨNG YÊN: không arm kế hoạch, không bấm chip, phiên chưa chạy.
    4. Bật tool sau đó mới chuyển sang theo dõi (và vẫn chờ hết tay hiện tại).
    5. Không có bàn đạt / quá hạn chờ thì báo rõ, không treo chế độ.
"""

import pathlib
import tempfile
import time
import unittest

from bcr.controllers import baccarat as BA
from bcr.service import engine

# B 13 · P 9 (lệch 4), bệt tối đa 3, không có cầu 3-3 → đạt cả 5 điều kiện lọc.
GOOD = "".join(side * count for side, count in (
    ("B", 3), ("P", 2), ("B", 3), ("P", 2), ("B", 2), ("P", 3), ("B", 2), ("P", 2), ("B", 3)))
# Bệt 6 tay liền ở đầu → vượt max_streak, phải bị loại.
BET = "BBBBBB" + "PPBBPPBBPPBBPPBB" + "PPBB"


class FakeEmitter:
    def __init__(self):
        self.events = []

    def event(self, name, data):
        self.events.append((name, data))

    def reply(self, request_id, result=None, error=None):
        pass

    def send(self, payload):
        pass


class FakeStream:
    """Sảnh giả: trả danh sách ô bàn và ghi lại bàn được bấm mở."""

    def __init__(self, tiles):
        self.tiles = tiles
        self.opened = []
        self.sweeps = 0
        self.last_error = ""

    def find_lobby_tab(self):
        return True, "Đã nhận tab sảnh Baccarat (giả lập)."

    def sweep(self, favorites_only=False):
        self.sweeps += 1
        return list(self.tiles)

    def open_table(self, table_id):
        self.opened.append(table_id)
        return True, "Đã mở bàn %s qua ô bàn của sảnh." % table_id


class FakeBrowser:
    """Chrome đã gắn CDP: trong sảnh chưa đọc được bàn, vào bàn rồi mới thấy cầu."""

    connected = True
    note = ""
    mode = "cdp"
    cdp_attached = True
    last_error = ""
    last_diag = {}

    def __init__(self, stream):
        self.stream = stream
        self.scanned = 0

    def scan(self):
        self.scanned += 1
        if not self.stream.opened:
            return []
        tile_id = self.stream.opened[-1]
        tile_row = next(t for t in self.stream.tiles if t["id"] == tile_id)
        return [{"page": {}, "tables": [{
            "id": tile_id, "title": tile_row["title"], "letters": tile_row["letters"],
            "source": "evo-bead", "game_type": "", "read_error": "",
        }]}]

    def stop(self):
        pass


def tile(table_id, label, letters, **extra):
    # Cùng hình dạng với ``LobbyStream.sweep`` thật: có cả ``id`` và ``table_id``.
    row = {"id": table_id, "table_id": table_id, "title": label, "label": label,
           "letters": letters, "road_match": True, "source": "evo-bead"}
    row.update(extra)
    return row


class EnterValidPipelineTests(unittest.TestCase):
    def setUp(self):
        self._saved = (BA.SCAN_SECONDS, BA.FAST_SCAN, BA.CYCLE_RETRY_SECONDS)
        BA.SCAN_SECONDS, BA.FAST_SCAN, BA.CYCLE_RETRY_SECONDS = 0.05, 0.05, 0.05

    def tearDown(self):
        (BA.SCAN_SECONDS, BA.FAST_SCAN, BA.CYCLE_RETRY_SECONDS) = self._saved

    def build(self, tiles):
        service = engine.BaccaratService(pathlib.Path(tempfile.mkdtemp()) / "b.json", FakeEmitter())
        service.stream = FakeStream(tiles)
        service.browser = FakeBrowser(service.stream)
        return service

    def drain(self, service, until=None, rounds=400):
        """Chạy đúng vòng ``_tick`` thật tới khi điều kiện đúng (hoặc hết lượt chờ)."""
        for _ in range(rounds):
            service._tick()
            if until is not None and until(service):
                return True
            time.sleep(0.02)
        return until(service) if until is not None else True

    def test_one_button_goes_from_lobby_to_the_eligible_table(self):
        service = self.build([
            tile("bet", "Bàn bệt", BET),
            tile("fast", "Bàn 7 Siêu Tốc Độ", GOOD),
            tile("ok", "Bàn 12", GOOD),
        ])
        reply = service.dispatch("table.enterValid", {})[0]
        self.assertTrue(reply["ok"], reply)

        # Đọc sảnh xong phải bấm đúng bàn 12 (bỏ bàn bệt và bàn siêu tốc).
        self.assertTrue(self.drain(service, lambda s: bool(s.stream.opened)))
        self.assertEqual(service.stream.opened, ["ok"], service.stream.opened)
        self.assertGreaterEqual(service.stream.sweeps, 1)

    def test_after_opening_the_tool_stays_idle_and_places_no_bet(self):
        service = self.build([tile("ok", "Bàn 12", GOOD)])
        self.assertFalse(service.tool_running)
        service.enter_valid_table()
        self.assertTrue(self.drain(service, lambda s: s.valid_check_done))

        # Bàn đã xác nhận nhưng phiên CHƯA chạy: đây là yêu cầu “kiểm tra (chưa chơi)”.
        self.assertTrue(service.valid_check_done)
        self.assertFalse(service.enter_valid)
        self.assertEqual(service.sim.table_key, "id:ok")
        self.assertFalse(service.sim.running)
        self.assertIsNone(service.sim.pending)
        self.assertFalse(service.place_busy, "không được bấm chip khi chỉ vào bàn")
        self.assertIn("CHƯA đặt cược", service.valid_note)
        self.assertEqual(service.stream.opened, ["ok"])

    def test_tool_then_takes_over_the_confirmed_table(self):
        service = self.build([tile("ok", "Bàn 12", GOOD)])
        service.enter_valid_table()
        self.assertTrue(self.drain(service, lambda s: s.valid_check_done))

        service.tool_start()
        self.assertTrue(service.sim.running)
        self.assertTrue(service.sim.entry_waiting, "chờ hết tay hiện tại rồi mới cược")
        self.assertIsNone(service.sim.pending)

    def test_tool_start_without_a_table_starts_the_hunt_itself(self):
        service = self.build([tile("ok", "Bàn 12", GOOD)])
        reply = service.tool_start()
        self.assertTrue(reply["ok"])
        self.assertTrue(service.enter_valid, "bật tool mà chưa có bàn thì tự tìm bàn")
        self.assertTrue(self.drain(service, lambda s: bool(s.stream.opened)))
        self.assertEqual(service.stream.opened, ["ok"])

    def test_no_eligible_table_keeps_hunting_instead_of_stopping(self):
        service = self.build([tile("bet", "Bàn bệt", BET)])
        service.enter_valid_table()
        self.assertTrue(self.drain(service, lambda s: s.stream.sweeps >= 2))
        self.assertEqual(service.stream.opened, [])
        self.assertFalse(service.valid_check_done)
        self.assertIsNone(service.sim.table_key)
        self.assertTrue(service.enter_valid, "vẫn đang tìm, chưa bỏ cuộc")

    def test_hitting_the_deadline_reports_and_stops(self):
        service = self.build([tile("bet", "Bàn bệt", BET)])
        service.enter_valid_table()
        service.enter_deadline = time.time() - 1
        service._tick()
        self.assertFalse(service.enter_valid)
        self.assertFalse(service.cycle_waiting)
        self.assertIn("Chưa vào được bàn", service.valid_note)

    def test_cancel_stops_the_hunt(self):
        service = self.build([tile("ok", "Bàn 12", GOOD)])
        service.enter_valid_table()
        reply = service.dispatch("table.cancelEnter", {})[0]
        self.assertTrue(reply["ok"])
        self.assertFalse(service.enter_valid)
        self.assertFalse(service.cycle_waiting)

    def test_tick_does_not_cancel_the_flow_while_chrome_is_still_connecting(self):
        """Lỗi thật gặp khi chạy live: vòng tick thấy chrome_connected == false ở những
        nhịp đầu (thread gắn Chrome đang chạy) rồi coi là "Chrome đã đóng" và huỷ chế độ
        vừa bật. Chưa từng kết nối thì không được huỷ."""
        service = self.build([tile("ok", "Bàn 12", GOOD)])
        service.browser.connected = False
        service.had_chrome = False
        service.enter_valid = True
        service.cycle_waiting = True
        service.enter_deadline = time.time() + 60
        for _ in range(5):
            service._tick()
        self.assertTrue(service.enter_valid, service.valid_note)

    def test_tick_cancels_when_chrome_was_closed_after_connecting(self):
        service = self.build([tile("ok", "Bàn 12", GOOD)])
        service.had_chrome = True
        service.enter_valid = True
        service.enter_deadline = time.time() + 60
        service.browser.connected = False
        service._tick()
        self.assertFalse(service.enter_valid)
        self.assertIn("Chrome đã đóng", service.valid_note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
