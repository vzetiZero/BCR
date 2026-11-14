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
from bcr.controllers.longlong_live import LongLongLiveMixin
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


class FakeApp(LongLongLiveMixin):
    """Host tối thiểu: kế thừa mixin để mọi method tự resolve như app thật."""

    def __init__(self, **overrides):
        self.longlong_auto_lines = []
        self.longlong_auto_log = Mock()
        self.longlong_place_var = Mock(get=Mock(return_value=False))
        self.longlong_auto_var = Mock(get=Mock(return_value=True))
        self.longlong_pending_place = None
        self.longlong_place_attempts = 0
        self.longlong_place_busy = False
        self.longlong_arm_hand = None
        self.longlong_tracker = HandTracker(min_gap=15.0)
        self.longlong_table = Mock()
        self.browser = Mock()
        self.root = Mock()
        self._longlong_simulation = Mock()
        for key, value in overrides.items():
            setattr(self, key, value)


def host(**overrides):
    return FakeApp(**overrides)


class PlaceGateTests(unittest.TestCase):
    """Đặt chip thật chỉ chạy khi được bật, đủ tiền và còn lượt thử."""

    PLAN = {"action": "bet", "side": longlong.DOOR, "stake": 40, "step": 0}

    def test_does_nothing_when_real_placing_is_off(self):
        node = host(longlong_pending_place=dict(self.PLAN))
        with patch.object(LongLongLiveMixin, "_longlong_try_place", autospec=True):
            LongLongLiveMixin._longlong_try_place(node, {"ok": True, "balance_vn2": 500.0})
        node.longlong_table.place.assert_not_called()

    def test_places_when_enabled_and_balance_is_enough(self):
        ll = host(longlong_pending_place=dict(self.PLAN),
                  longlong_place_var=Mock(get=Mock(return_value=True)))
        with patch("bcr.controllers.longlong_live.threading.Thread") as thread:
            LongLongLiveMixin._longlong_try_place(ll, {"ok": True, "balance_vn2": 500.0})
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()
        self.assertTrue(ll.longlong_place_busy)
        self.assertEqual(ll.longlong_place_attempts, 1)

    def test_skips_the_hand_when_balance_is_too_low(self):
        ll = host(longlong_pending_place=dict(self.PLAN),
                  longlong_place_var=Mock(get=Mock(return_value=True)))
        with patch("bcr.controllers.longlong_live.threading.Thread") as thread:
            LongLongLiveMixin._longlong_try_place(ll, {"ok": True, "balance_vn2": 10.0})
        thread.assert_not_called()
        self.assertIsNone(ll.longlong_pending_place)
        self.assertIn("Không đủ số dư", ll.longlong_auto_lines[-1])

    def test_gives_up_after_three_attempts(self):
        ll = host(longlong_pending_place=dict(self.PLAN), longlong_place_attempts=3,
                  longlong_place_var=Mock(get=Mock(return_value=True)))
        with patch("bcr.controllers.longlong_live.threading.Thread") as thread:
            LongLongLiveMixin._longlong_try_place(ll, {"ok": True, "balance_vn2": 500.0})
        thread.assert_not_called()
        self.assertIsNone(ll.longlong_pending_place)
        self.assertIn("3 lần", ll.longlong_auto_lines[-1])

    def test_does_not_double_place_while_a_placement_is_running(self):
        ll = host(longlong_pending_place=dict(self.PLAN), longlong_place_busy=True,
                  longlong_place_var=Mock(get=Mock(return_value=True)))
        with patch("bcr.controllers.longlong_live.threading.Thread") as thread:
            LongLongLiveMixin._longlong_try_place(ll, {"ok": True, "balance_vn2": 500.0})
        thread.assert_not_called()


class AfterPlaceTests(unittest.TestCase):
    def test_success_arms_the_plan_and_clears_pending(self):
        strategy = Mock()
        ll = host(longlong_pending_place={"action": "bet", "stake": 40})
        ll._longlong_simulation = Mock(return_value=types.SimpleNamespace(strategy=strategy))
        ll.longlong_place_busy = True
        LongLongLiveMixin._longlong_after_place(
            ll, {"ok": True, "placed": [40], "total": 40.0}, 40)
        strategy.arm.assert_called_once()
        self.assertIsNone(ll.longlong_pending_place)
        self.assertFalse(ll.longlong_place_busy)
        self.assertIn("Đã đặt", ll.longlong_auto_lines[-1])

    def test_failure_keeps_the_plan_for_a_retry(self):
        plan = {"action": "bet", "stake": 40}
        ll = host(longlong_pending_place=plan)
        ll.longlong_place_busy = True
        LongLongLiveMixin._longlong_after_place(ll, {"ok": False, "error": "Ván đã có kết quả"},
                                                40)
        self.assertEqual(ll.longlong_pending_place, plan)
        self.assertFalse(ll.longlong_place_busy)
        self.assertIn("Ván đã có kết quả", ll.longlong_auto_lines[-1])


class OnHandTests(unittest.TestCase):
    """Một ván chốt xong: tất toán ván trước, rồi tính kế hoạch cho ván sau."""

    def make(self, real=False):
        sim = longlong.LongLongSimulation([40, 80], capital=1_000_000)
        ll = host(longlong_place_var=Mock(get=Mock(return_value=real)))
        ll._longlong_simulation = Mock(return_value=sim)
        ll.longlong_tracker.hands = 1
        return ll, sim

    def test_simulation_mode_arms_on_paper_and_never_places(self):
        ll, sim = self.make(real=False)
        LongLongLiveMixin._longlong_on_hand(ll, "VD")
        self.assertEqual(sim.strategy.results, ["VD"])
        self.assertIsNone(ll.longlong_pending_place, "mô phỏng thì chốt luôn trên giấy")
        self.assertIsNotNone(sim.strategy.pending)
        LongLongLiveMixin._longlong_on_hand(ll, "VV")     # thua trên giấy
        self.assertEqual(sim.strategy.step, 1)
        self.assertIn("THUA", ll.longlong_auto_lines[-2] + ll.longlong_auto_lines[-1])

    def test_real_mode_waits_for_chips_before_arming(self):
        ll, sim = self.make(real=True)
        LongLongLiveMixin._longlong_on_hand(ll, "VD")
        self.assertIsNone(sim.strategy.pending, "chưa bấm chip thì chưa chốt lệnh")
        self.assertEqual(ll.longlong_pending_place["stake"], 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
