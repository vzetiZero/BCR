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
    """Sàn giữ class "ô thắng" vài giây -> mỗi ván chỉ được chốt một lần."""

    def test_fires_once_while_the_same_winner_keeps_showing(self):
        tracker = HandTracker(min_gap=15.0)
        self.assertEqual(tracker.feed("JNS_C", "VD", 100.0), "VD")
        for moment in (101.0, 102.5, 104.0, 110.0):
            self.assertIsNone(tracker.feed("JNS_C", "VD", moment))
        self.assertEqual(tracker.hands, 1)

    def test_a_different_winner_is_a_new_hand_immediately(self):
        tracker = HandTracker(min_gap=15.0)
        tracker.feed("JNS_C", "VD", 100.0)
        self.assertEqual(tracker.feed("JNS_A", "DD", 101.0), "DD")
        self.assertEqual(tracker.feed("JNS_B", "VV", 102.0), "VV")
        self.assertEqual(tracker.hands, 3)

    def test_same_winner_after_the_gap_counts_as_a_new_hand(self):
        tracker = HandTracker(min_gap=15.0)
        tracker.feed("JNS_C", "VD", 100.0)
        self.assertIsNone(tracker.feed("JNS_C", "VD", 110.0))
        self.assertEqual(tracker.feed("JNS_C", "VD", 116.0), "VD")
        self.assertEqual(tracker.hands, 2)

    def test_without_a_winner_nothing_is_recorded(self):
        tracker = HandTracker()
        for winner, raw in ((None, None), ("", ""), ("JNS_X", None)):
            self.assertIsNone(tracker.feed(winner, raw, 100.0))
        self.assertEqual(tracker.hands, 0)

    def test_reset_clears_the_counter(self):
        tracker = HandTracker(min_gap=15.0)
        tracker.feed("JNS_C", "VD", 100.0)
        tracker.reset()
        self.assertEqual(tracker.hands, 0)
        self.assertEqual(tracker.feed("JNS_C", "VD", 101.0), "VD")


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
