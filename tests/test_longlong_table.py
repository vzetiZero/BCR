# -*- coding: utf-8 -*-
"""Kiểm thử Adapter đọc bàn LONG-LONG (bcr/adapters/longlong_table.py).

Chỉ kiểm thử phần THUẦN (parse chữ của sàn, map ô cược) — không cần Chrome. Các số
liệu dưới đây lấy từ bàn thật "DragonDragon" (game twocoins) đã dò bằng CDP.
"""

import unittest

from bcr.adapters import longlong_table as LT


class CleanTextTests(unittest.TestCase):
    def test_strips_invisible_isolate_marks_around_money(self):
        # Sàn bọc số tiền bằng U+2066/U+2069 như trong "⁦0 ⁦VN2⁩⁩".
        self.assertEqual(LT.clean_text("\u20660 \u2066VN2\u2069\u2069"), "0 VN2")
        self.assertEqual(LT.clean_text("  SỐ   DƯ \u200e "), "SỐ DƯ")


class ParseVn2Tests(unittest.TestCase):
    def test_reads_balance_text_of_the_real_table(self):
        self.assertEqual(LT.parse_vn2("\u20660 \u2066VN2\u2069\u2069"), 0.0)
        self.assertEqual(LT.parse_vn2("1.234,56 VN2"), 1234.56)
        self.assertEqual(LT.parse_vn2("3.114,69 VN2"), 3114.69)
        self.assertEqual(LT.parse_vn2("105,10"), 105.10)
        self.assertEqual(LT.parse_vn2("50.000 VN2"), 50000.0)

    def test_does_not_guess_when_there_is_no_number(self):
        for value in ("", None, "VN2", "chưa có", "—"):
            self.assertIsNone(LT.parse_vn2(value), value)


class ParseSpotTests(unittest.TestCase):
    def test_reads_the_three_spots_of_the_real_table(self):
        mixed = LT.parse_spot("C 0.95:1 14 VN2 3.114,69 76 %")
        self.assertEqual(mixed["code"], "C")
        self.assertAlmostEqual(mixed["rate"], 0.95)
        self.assertEqual(mixed["chips"], 14)
        self.assertAlmostEqual(mixed["amount"], 3114.69)
        self.assertEqual(mixed["percent"], 76)

        gold = LT.parse_spot("B 2.90:1 8 VN2 457,32 11 %")
        self.assertEqual(gold["code"], "B")
        self.assertAlmostEqual(gold["rate"], 2.90)
        self.assertEqual(gold["chips"], 8)

        red = LT.parse_spot("A 2.90:1 10 VN2 487,68 12 %")
        self.assertEqual(red["code"], "A")

    def test_empty_text_gives_empty_fields(self):
        parsed = LT.parse_spot("")
        self.assertIsNone(parsed["code"])
        self.assertIsNone(parsed["amount"])


class DoorMappingTests(unittest.TestCase):
    def test_dom_roles_match_the_board_labels(self):
        # aria-label trên bàn thật: JNS_B "Bet on GOLD", JNS_A "Bet on RED", JNS_C "Bet on MIX".
        self.assertEqual(LT.DOORS["JNS_B"], ("B", "GOLD", "VV"))
        self.assertEqual(LT.DOORS["JNS_A"], ("A", "RED", "DD"))
        self.assertEqual(LT.DOORS["JNS_C"], ("C", "MIX", "VD"))

    def test_strategy_door_is_the_mixed_one(self):
        self.assertEqual(LT.STRATEGY_DOOR, "JNS_C")
        self.assertEqual(LT.DOOR_OF_RAW["VD"], "JNS_C")
        self.assertEqual(LT.DOOR_OF_RAW["DV"], "JNS_C")
        self.assertEqual(LT.DOOR_OF_RAW["VV"], "JNS_B")
        self.assertEqual(LT.DOOR_OF_RAW["DD"], "JNS_A")


class WinnerDetectionTests(unittest.TestCase):
    """Ô thắng = ô được gắn thêm class hiệu ứng (không bám vào tên class cụ thể)."""

    def spots(self, a, b, c):
        return {"JNS_A": {"cls": a}, "JNS_B": {"cls": b}, "JNS_C": {"cls": c}}

    def test_detects_the_extra_class_of_the_winning_spot(self):
        # Dữ liệu thật: ô thắng có 3 class, hai ô còn lại 2 class.
        self.assertEqual(LT.detect_winner(self.spots(
            "BqH6fC _1gRkDV InFqhC", "BqH6fC wJnCw4", "BqH6fC aCw3mL")), "JNS_A")
        self.assertEqual(LT.detect_winner(self.spots(
            "BqH6fC _1gRkDV", "BqH6fC wJnCw4 InFqhC", "BqH6fC aCw3mL")), "JNS_B")
        self.assertEqual(LT.detect_winner(self.spots(
            "BqH6fC _1gRkDV", "BqH6fC wJnCw4", "BqH6fC aCw3mL InFqhC")), "JNS_C")

    def test_no_winner_when_all_spots_look_the_same(self):
        self.assertIsNone(LT.detect_winner(self.spots(
            "BqH6fC _1gRkDV", "BqH6fC wJnCw4", "BqH6fC aCw3mL")))
        self.assertIsNone(LT.detect_winner({}))
        self.assertIsNone(LT.detect_winner(None))

    def test_winner_maps_to_the_hand_pair(self):
        # JNS_A = RED = 2 đỏ (DD); JNS_B = GOLD = 2 vàng (VV); JNS_C = MIX = VD/DV.
        self.assertEqual(LT.DOORS[LT.detect_winner(self.spots("x y z", "x y", "x w"))][2], "DD")
        self.assertEqual(LT.DOORS[LT.detect_winner(self.spots("x y", "x y z", "x w"))][2], "VV")
        self.assertEqual(LT.DOORS[LT.detect_winner(self.spots("x y", "x w", "x y z"))][2], "VD")


class StateTextTests(unittest.TestCase):
    def test_describes_a_readable_state(self):
        note = LT.read_state_text({"ok": True, "status": "ĐẶT CƯỢC CỦA BẠN",
                                   "winner": None})
        self.assertIn("ĐẶT CƯỢC CỦA BẠN", note)
        self.assertIn("chưa có kết quả", note)

    def test_describes_the_winning_door(self):
        note = LT.read_state_text({"ok": True, "status": "ĐANG CHIA BÀI",
                                   "winner": "JNS_C"})
        self.assertIn("ô C", note)
        self.assertIn("MIX", note)

    def test_describes_an_unreadable_state(self):
        self.assertIn("Chưa đọc được", LT.read_state_text({"ok": False}))
        self.assertIn("Chưa đọc được", LT.read_state_text(None))


class ReadShapeTests(unittest.TestCase):
    def test_read_without_chrome_returns_an_error_state(self):
        from unittest.mock import patch

        # Không phụ thuộc máy đang chạy Chrome hay không: giả lập CDP không có target.
        with patch.object(LT.LongLongTable, "_targets", return_value=[]):
            table = LT.LongLongTable()
            state = table.read()
        self.assertFalse(state["ok"])
        self.assertTrue(state["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
