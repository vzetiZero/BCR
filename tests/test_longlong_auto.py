# -*- coding: utf-8 -*-
"""Kiểm thử phần tự động của LONG-LONG: chốt ván 1 lần, chia chip, cổng đặt chip.

Không cần Chrome: HandTracker và chia chip là hàm thuần; phần đặt chip được kiểm thử
bằng host giả + Mock để không bấm tiền thật.
"""

import types
import unittest
from unittest.mock import Mock, patch

from bcr.adapters import longlong_table as LT
from bcr.controllers.longlong_auto import HandTracker
from bcr.models import longlong

DENOMS = [10, 20, 40, 100, 500, 2000]


class HandTrackerTests(unittest.TestCase):
    """Sàn giữ class "ô thắng" vài giây -> mỗi ván chỉ chốt một lần, không sinh ván ma."""

    def test_needs_two_consecutive_reads_of_the_same_door(self):
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=2)
        self.assertIsNone(tracker.feed("JNS_C", "VD", 100.0), "lần đọc đầu chỉ là nghi ngờ")
        self.assertEqual(tracker.feed("JNS_C", "VD", 102.0), "VD")
        self.assertEqual(tracker.hands, 1)

    def test_fires_once_while_the_same_winner_keeps_showing(self):
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=2)
        tracker.feed("JNS_C", "VD", 100.0)
        tracker.feed("JNS_C", "VD", 102.0)
        for moment in (104.0, 106.0, 110.0):
            self.assertIsNone(tracker.feed("JNS_C", "VD", moment))
        self.assertEqual(tracker.hands, 1)

    def test_a_flicker_to_another_door_does_not_create_a_phantom_hand(self):
        # Log thật của người dùng: "Ván 5" 10:09:38 rồi "Ván 6" 10:09:40 (cách 2 giây).
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=2)
        self.assertIsNone(tracker.feed("JNS_C", "VD", 100.0))
        self.assertEqual(tracker.feed("JNS_C", "VD", 102.0), "VD")
        # Hiệu ứng đổi ô một nhịp rồi quay lại: không được thành ván mới.
        self.assertIsNone(tracker.feed("JNS_A", "DD", 104.0))
        self.assertIsNone(tracker.feed("JNS_C", "VD", 106.0))
        self.assertEqual(tracker.hands, 1)

    def test_two_real_hands_are_separated_by_the_minimum_gap(self):
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=2)
        tracker.feed("JNS_C", "VD", 100.0)
        tracker.feed("JNS_C", "VD", 102.0)
        self.assertIsNone(tracker.feed("JNS_A", "DD", 115.0))
        self.assertEqual(tracker.feed("JNS_A", "DD", 117.0), "DD")
        self.assertEqual(tracker.hands, 2)

    def test_a_new_hand_right_after_the_last_is_refused(self):
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=1)
        self.assertEqual(tracker.feed("JNS_C", "VD", 100.0), "VD")
        self.assertIsNone(tracker.feed("JNS_A", "DD", 102.0), "2 giây không thể là ván thật")
        self.assertEqual(tracker.hands, 1)

    def test_confirm_one_fires_on_the_first_read(self):
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=1)
        self.assertEqual(tracker.feed("JNS_C", "VD", 100.0), "VD")

    def test_a_blank_read_breaks_a_flickering_pair(self):
        tracker = HandTracker(confirm=2)
        self.assertIsNone(tracker.feed("JNS_C", "VD", 100.0))
        self.assertIsNone(tracker.feed(None, None, 101.0))
        self.assertIsNone(tracker.feed("JNS_C", "VD", 102.0))
        self.assertEqual(tracker.hands, 0)

    def test_without_a_winner_nothing_is_recorded(self):
        tracker = HandTracker()
        for winner, raw in ((None, None), ("", ""), ("JNS_X", None)):
            self.assertIsNone(tracker.feed(winner, raw, 100.0))
        self.assertEqual(tracker.hands, 0)

    def test_configure_clamps_bad_values(self):
        tracker = HandTracker()
        tracker.configure(confirm=99, min_hand_gap=-5, min_gap=-1)
        self.assertEqual(tracker.confirm, 5)
        self.assertEqual(tracker.min_hand_gap, 0.0)
        self.assertEqual(tracker.min_gap, 0.0)

    def test_reset_clears_the_counter(self):
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=1)
        tracker.feed("JNS_C", "VD", 100.0)
        tracker.reset()
        self.assertEqual(tracker.hands, 0)
        self.assertEqual(tracker.feed("JNS_C", "VD", 101.0), "VD")


class ResultSignatureTests(unittest.TestCase):
    """Khối KẾT QUẢ của sàn là trọng tài: khối đó không đổi thì không phải ván mới."""

    SIG = "cls-result|html-result"

    def test_a_door_flicker_without_a_new_result_is_ignored(self):
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=2)
        self.assertIsNone(tracker.feed("JNS_C", "VD", 100.0, self.SIG + "-1"))
        self.assertEqual(tracker.feed("JNS_C", "VD", 102.0, self.SIG + "-1"), "VD")
        # Ô đỏ nháy sau khi bấm chip, nhưng khối kết quả KHÔNG đổi -> không có ván mới.
        self.assertIsNone(tracker.feed("JNS_A", "DD", 104.0, self.SIG + "-1"))
        self.assertIsNone(tracker.feed("JNS_A", "DD", 106.0, self.SIG + "-1"))
        self.assertEqual(tracker.hands, 1)

    def test_a_real_new_result_is_accepted(self):
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=2)
        tracker.feed("JNS_C", "VD", 100.0, self.SIG + "-1")
        tracker.feed("JNS_C", "VD", 102.0, self.SIG + "-1")
        # Sàn công bố kết quả mới -> chốt ngay ván mới dù cùng ô thắng.
        self.assertEqual(tracker.feed("JNS_C", "VD", 118.0, self.SIG + "-2"), "VD")
        self.assertEqual(tracker.hands, 2)

    def test_a_repeat_outcome_with_identical_markup_is_accepted_later(self):
        # Cầu lặp: ván mới ra y hệt ván trước nên markup khối kết quả không đổi.
        # Không được chặn mãi — quá 2*min_hand_gap thì vẫn phải chốt.
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=2)
        tracker.feed("JNS_C", "VD", 100.0, self.SIG)
        self.assertEqual(tracker.feed("JNS_C", "VD", 102.0, self.SIG), "VD")
        self.assertIsNone(tracker.feed("JNS_C", "VD", 110.0, self.SIG), "còn trong hạn hoãn")
        self.assertEqual(tracker.feed("JNS_C", "VD", 130.0, self.SIG), "VD")
        self.assertEqual(tracker.hands, 2)

    def test_without_a_result_element_the_door_rule_still_works(self):
        tracker = HandTracker(min_gap=15.0, min_hand_gap=6.0, confirm=2)
        tracker.feed("JNS_C", "VD", 100.0, "")
        self.assertEqual(tracker.feed("JNS_C", "VD", 102.0, ""), "VD")
        self.assertEqual(tracker.hands, 1)

    def test_reset_forgets_the_last_result_signature(self):
        tracker = HandTracker(confirm=1)
        tracker.feed("JNS_C", "VD", 100.0, self.SIG + "-1")
        tracker.reset()
        self.assertIsNone(tracker._fired_sig)


class StatusGateTests(unittest.TestCase):
    """Câu chữ trạng thái của sàn: chỉ chặn khi rõ ràng "đã đóng/đang ra kết quả"."""

    def test_open_bets_status_is_not_closed(self):
        self.assertFalse(LT.bets_closed("ĐẶT CƯỢC CỦA BẠN"))
        self.assertFalse(LT.bets_closed("PLACE YOUR BETS"))

    def test_result_status_is_closed(self):
        self.assertTrue(LT.bets_closed("KẾT QUẢ"))
        self.assertTrue(LT.bets_closed("Chờ kết quả"))
        self.assertTrue(LT.bets_closed("NO MORE BETS"))

    def test_unknown_status_is_not_blocked(self):
        for text in ("", None, "XYZ", "—"):
            self.assertFalse(LT.bets_closed(text), text)


class DecomposeTests(unittest.TestCase):
    """Chia số tiền thành mệnh giá chip đang có trên khay bàn thật."""

    def test_exact_denominations(self):
        self.assertEqual(LT.decompose_amount(40, DENOMS), [40])
        self.assertEqual(LT.decompose_amount(10, DENOMS), [10])
        self.assertEqual(LT.decompose_amount(80, DENOMS), [40, 40])
        self.assertEqual(LT.decompose_amount(2000, DENOMS), [2000])

    def test_full_ladder_step_of_4920(self):
        chips = LT.decompose_amount(4920, DENOMS)
        self.assertEqual(sum(chips), 4920)
        self.assertEqual(chips, [2000, 2000, 500, 100, 100, 100, 100, 20])

    def test_refuses_amounts_that_cannot_be_made(self):
        self.assertIsNone(LT.decompose_amount(15, [10, 20]))
        self.assertIsNone(LT.decompose_amount(0, DENOMS))
        self.assertIsNone(LT.decompose_amount(None, DENOMS))
        self.assertIsNone(LT.decompose_amount(40, []))

    def test_clickable_boxes_only(self):
        self.assertTrue(LT.clickable({"x": 10, "y": 10, "w": 33, "h": 33}))
        self.assertFalse(LT.clickable({"x": 10, "y": 10, "w": 2, "h": 33}))
        self.assertFalse(LT.clickable({"x": -5, "y": 10, "w": 33, "h": 33}))
        self.assertFalse(LT.clickable(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
