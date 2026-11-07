"""In-memory live simulation and URL regressions; no UI, browser, or state files."""

import sys
import re

sys.dont_write_bytecode = True

from copy import deepcopy
import json
import inspect
import math
import queue
import random
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, mock_open, patch
from urllib.parse import urlsplit
from urllib.error import URLError

import app
import browser as BR
import engine as E
import human_mouse as HM
import lobby_stream as LS
import method as ME
import money as M
import table_bets as TB


BASELINE = "BPBPBPBPBPBPBPB"
PLAYER_BASELINE = BASELINE.translate(str.maketrans("BP", "PB"))
UNIT = 100_000
CAPITAL = 100_000_000
# Sảnh Evolution đổi mirror liên tục nên test dùng một host ví dụ của đường dẫn lobby.
LOBBY_HOST = "tmrw17k36as.evo-games.com"
LOBBY_ORIGIN = "https://" + LOBBY_HOST
LOBBY_URL = LOBBY_ORIGIN + BR.LOBBY_PATH + "/#category=all_games&game=baccarat&see=baccarat"


class MoneyInputTests(unittest.TestCase):
    """Ô nhập vốn phải cho người dùng thấy mốc triệu ngay khi gõ."""

    def test_groups_digits_and_keeps_the_caret_before_the_next_digit(self):
        self.assertEqual(app.group_money_input("2000000", 7), ("2,000,000", 9))
        self.assertEqual(app.group_money_input("10000", 2), ("10,000", 3))
        self.assertEqual(app.group_money_input("", 0), ("", 0))
        # Chuỗi đã đúng nhóm thì giữ nguyên, kể cả khi đang gõ dở dấu phân cách.
        self.assertEqual(app.group_money_input("2,000,000", 4), ("2,000,000", 4))
        self.assertEqual(app.group_money_input("2000000,", 9), ("2,000,000", 9))

    def test_reads_every_grouping_style_back_to_the_same_number(self):
        for text in ("2,000,000", "2.000.000", "2000000", " 2,000,000 "):
            self.assertEqual(M.parse_money(text), 2_000_000.0)
        self.assertEqual(M.parse_money("100,000"), UNIT)

    def test_reads_decimal_wallet_amounts_from_evolution(self):
        self.assertEqual(M.parse_money("⁦3.882,74 ⁦VN2 ⁩⁩"), 3882.74)
        self.assertEqual(M.parse_money("3,882.74"), 3882.74)
        self.assertEqual(M.parse_money(M.fmt_input_money(3882.74)), 3882.74)

    def test_rejects_text_that_is_not_a_money_amount(self):
        for text in ("", "invalid", "1 000 000", "2e6", "12abc", "2.000đ"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    M.parse_money(text)


class CurrentRoadTests(unittest.TestCase):
    def test_lobby_requires_both_sources_to_agree_on_the_same_road(self):
        row = dict(letters="BPBP", ws_confirmed_letters="BPBP", stable=2, conflict=False, read_error="",
                   source="evo-ws-histories")
        self.assertTrue(app.road_is_current(row))
        # Một tay mới chỉ đến từ cầu đang vẽ: chưa đối chiếu hai nguồn thì chưa được chọn.
        row["letters"] = "BPBPP"
        self.assertFalse(app.road_is_current(row))
        row["letters"] = "BPBP"
        self.assertFalse(app.road_is_current(dict(row, stable=1)))
        self.assertFalse(app.road_is_current(dict(row, ws_confirmed_letters="")))

    def test_open_table_requires_two_stable_bead_reads(self):
        row = dict(letters="BPBP", source="evo-bead", screen_stable=1, conflict=False, read_error="")
        self.assertFalse(app.road_is_current(row))
        row["screen_stable"] = 2
        self.assertTrue(app.road_is_current(row))

    def test_same_shoe_accepts_only_forward_growth(self):
        self.assertTrue(app.same_shoe("BPBP", "BPBPT"))
        self.assertTrue(app.same_shoe("BPBPT", "BPBP"))
        self.assertTrue(app.same_shoe("BPBP", "TTTBPBP"))
        self.assertFalse(app.same_shoe("BPBP", "PBPB"))
        self.assertFalse(app.same_shoe("", "BPBP"))
        self.assertFalse(app.same_shoe("BPBP", ""))


class AutoStartAtEntryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        clock = patch.object(app.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def screen_row(self, letters, **changes):
        row = dict(key="id:t1", table_id="t1", label="Baccarat Nhanh A", letters=letters, ts=self.now,
                   stable=3, conflict=False, read_error="", game_type="", source="evo-bead", screen_stable=2)
        row.update(changes)
        return row

    def host(self, entry_letters, row):
        book = M.MoneyBook(capital=CAPITAL, unit=UNIT, take_profit_pct=100)
        # Chu trình tự đổi bàn tắt để test chỉ đi đúng một tay/lần gọi như trước.
        host = SimpleNamespace(tables={row["key"]: row}, sim=app.LiveSimulation(book),
                               auto_start_table_key=row["key"], auto_start_letters=entry_letters,
                               auto_start_deadline=self.now + app.ENTRY_WAIT, sweep_note_text="",
                               auto_cycle_var=Mock(get=lambda: False), cycle_waiting=False,
                               cycle_attempts=0, cycle_table_key=None, chrome_busy=False,
                               sweep_busy=False, open_table_busy=False, exit_requested=False,
                               _cdp_available=lambda: True, stream=Mock(), events=Mock(),
                               refresh=lambda: None)
        host._cycle_search = MethodType(app.BCRApp._cycle_search, host)
        return host

    def test_whole_shoe_history_does_not_block_start_after_entry(self):
        row = self.screen_row("B" * 30 + BASELINE)
        self.assertFalse(ME.check_table(list(row["letters"]))["eligible"])
        host = self.host(BASELINE, row)
        app.BCRApp._auto_start_verified_table(host)
        self.assertTrue(host.sim.running)
        self.assertEqual(host.sim.letters, row["letters"])
        self.assertEqual(host.sim.pending["baseline"], row["letters"])
        self.assertIsNone(host.auto_start_table_key)

    def test_first_hand_after_entry_settles_and_earlier_hands_are_never_counted(self):
        host = self.host(BASELINE, self.screen_row(BASELINE + "T"))
        app.BCRApp._auto_start_verified_table(host)
        self.assertEqual(host.sim.money.hands_played, 0)
        bet, baseline = host.sim.pending["side"], host.sim.letters
        row = self.screen_row(baseline + ("P" if bet == "B" else "B"))
        self.now += 2
        self.assertTrue(host.sim.process(row, self.now))
        self.assertEqual(len(host.sim.log), 1)
        self.assertEqual(host.sim.log[0]["hand"], len(baseline) + 1)
        self.assertEqual(host.sim.money.losses, 1)

    def test_other_shoe_is_not_betting_data(self):
        host = self.host(BASELINE, self.screen_row(PLAYER_BASELINE + "P" * 40))
        app.BCRApp._auto_start_verified_table(host)
        self.assertFalse(host.sim.running)
        self.assertIsNone(host.auto_start_table_key)
        self.assertIn("không cùng shoe", host.sweep_note_text)

    def test_unconfirmed_screen_waits_then_gives_up_after_the_window(self):
        host = self.host(BASELINE, self.screen_row(BASELINE, screen_stable=1))
        app.BCRApp._auto_start_verified_table(host)
        self.assertEqual(host.auto_start_table_key, "id:t1")
        self.assertFalse(host.sim.running)
        host.auto_start_deadline = self.now - 1
        app.BCRApp._auto_start_verified_table(host)
        self.assertIsNone(host.auto_start_table_key)
        self.assertIn("chưa đọc được cầu bàn đã mở", host.sweep_note_text)

    def test_stale_screen_read_does_not_arm(self):
        host = self.host(BASELINE, self.screen_row(BASELINE, ts=self.now - app.LIVE_STALE - 1))
        app.BCRApp._auto_start_verified_table(host)
        self.assertFalse(host.sim.running)
        self.assertIn("chưa cập nhật", host.sweep_note_text)

    def test_failed_entry_search_keeps_looking_for_another_table(self):
        host = self.host(BASELINE, self.screen_row(PLAYER_BASELINE))
        host.cycle_waiting = True
        host.auto_open_table = Mock()
        app.BCRApp._auto_start_verified_table(host)
        self.assertFalse(host.sim.running)
        host.auto_open_table.assert_called_once_with(allow_sweep=True, exclude_key="id:t1")

    def test_started_cycle_clears_the_search_state(self):
        host = self.host(BASELINE, self.screen_row(BASELINE))
        host.cycle_waiting = True
        host.cycle_attempts = 3
        host.cycle_table_key = "id:t9"
        app.BCRApp._auto_start_verified_table(host)
        self.assertTrue(host.sim.running)
        self.assertEqual((host.cycle_waiting, host.cycle_attempts, host.cycle_table_key),
                         (False, 0, None))


class LiveSimulationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        clock = patch.object(app.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def row(self, letters=BASELINE, **changes):
        row = dict(key="baccarat01", label="Baccarat 01", letters=letters,
                   ts=self.now, stable=2, conflict=False)
        row.update(changes)
        return row

    def simulation(self, baseline=BASELINE, capital=CAPITAL, start=True):
        self.assertTrue(ME.check_table(list(baseline))["eligible"])
        book = M.MoneyBook(capital=capital, unit=UNIT, take_profit_pct=100)
        # Thang bậc trong test bám theo UNIT của test, không phụ thuộc mức mặc định của app.
        ladder = [UNIT * m for m in (1, 3, 7, 15, 32, 64)]
        sim = app.LiveSimulation(book, ladder=ladder)
        self.assertIs(sim.money, book)
        sim.bind(self.row(baseline))
        if start:
            self.assertTrue(sim.start(self.row(baseline), verified=True, now=self.now))
        return sim

    def snapshot(self, sim):
        return deepcopy(dict(money=vars(sim.money), results=sim.results,
                             letters=sim.letters, method=sim.method_state, log=sim.log))

    def assert_pristine_book(self, sim):
        self.assertEqual(sim.money.balance, sim.money.start_capital)
        self.assertEqual(sim.money.profit, 0)
        for field in ("hands_played", "wins", "losses", "pushes", "ties_bet",
                      "total_staked", "total_returned", "max_drawdown", "loss_streak"):
            self.assertEqual(getattr(sim.money, field), 0, field)
        self.assertEqual(sim.log, [])

    def settle_next(self, sim, result):
        """Feed one hand without ever editing the previously armed plan."""
        self.assertTrue(sim.running)
        pending = sim.pending
        self.assertIsNotNone(pending)
        frozen = deepcopy(pending)
        baseline = sim.letters
        previous_log_count = len(sim.log)
        real_settle = sim.money.settle

        def checked_settle(*args, **kwargs):
            # Check at the settlement boundary, not only after process returns.
            self.assertIs(sim.pending, pending)
            self.assertEqual(pending, frozen)
            self.assertEqual(sim.letters, baseline)
            self.assertEqual(sim.results, list(baseline))
            return real_settle(*args, **kwargs)

        self.now += 1
        row = self.row(baseline + result, key=sim.table_key, label=sim.table_name)
        with patch.object(sim.money, "settle", side_effect=checked_settle) as settle:
            self.assertTrue(sim.process(row, now=self.now))
            settle.assert_called_once_with(frozen["side"], result, frozen["stake"], E.PAYOUT)
        self.assertEqual(pending, frozen)
        self.assertEqual(len(sim.log), previous_log_count + 1)
        entry = sim.log[-1]
        self.assertEqual(entry["armed_at"], frozen["armed_at"])
        self.assertLess(entry["armed_at"], entry["time"])
        self.assertEqual(entry["hand"], len(baseline) + 1)
        self.assertEqual(entry["bet"], frozen["side"])
        self.assertEqual(entry["stake"], frozen["stake"])
        self.assertEqual(entry["block_pos"], frozen["block_pos"])
        self.assertEqual(entry["ladder"], frozen["ladder_step"])
        self.assertEqual(entry["result"], result)
        self.assertEqual(entry["balance_after"], sim.money.balance)
        self.assertEqual(entry["source"], "live-driver")
        self.assertEqual(sim.letters, row["letters"])
        self.assertEqual(sim.results, list(row["letters"]))
        if sim.running:
            self.assertIsNot(sim.pending, pending)
            self.assertEqual(sim.pending["baseline"], row["letters"])
            self.assertEqual(sim.pending["armed_at"], self.now)
        return entry

    def assert_pauses_without_backfill(self, sim, row):
        before = self.snapshot(sim)
        pending = sim.pending
        frozen = deepcopy(pending)
        with patch.object(sim.money, "settle", wraps=sim.money.settle) as settle:
            self.assertFalse(sim.process(row, now=self.now))
            settle.assert_not_called()
        self.assertFalse(sim.running)
        self.assertIsNone(sim.pending)
        self.assertTrue(sim.note)
        self.assertEqual(pending, frozen)
        self.assertEqual(self.snapshot(sim), before)
        # A later healthy reading cannot retroactively settle the cancelled plan.
        self.now += 1
        self.assertFalse(sim.process(self.row(sim.letters + "B"), now=self.now))
        self.assertEqual(self.snapshot(sim), before)

    def test_bind_and_start_do_not_book_preexisting_history(self):
        sim = self.simulation(start=False)
        self.assertEqual(len(sim.results), 15)
        self.assertEqual(sim.results, list(BASELINE))
        self.assertEqual(sim.method_state, app.fresh_method())
        self.assertFalse(sim.running)
        self.assertIsNone(sim.pending)
        before = self.snapshot(sim)
        self.assertTrue(sim.start(self.row(), verified=True, now=self.now))
        self.assertEqual(self.snapshot(sim), before)
        self.assert_pristine_book(sim)
        self.assertEqual(sim.pending["baseline"], BASELINE)
        self.assertEqual(sim.pending["side"], "B")
        self.assertEqual(sim.pending["stake"], UNIT)
        self.assertEqual(sim.pending["armed_at"], self.now)

    def test_start_requires_manual_verification(self):
        sim = self.simulation(start=False)
        for kwargs in ({}, {"verified": False}):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(sim.start(self.row(), now=self.now, **kwargs))
                self.assertFalse(sim.running)
                self.assertIsNone(sim.pending)
                self.assert_pristine_book(sim)
        self.assertTrue(sim.start(self.row(), verified=True, now=self.now))

    def test_start_requires_bound_matching_table(self):
        sim = app.LiveSimulation()
        self.assertFalse(sim.start(self.row(), verified=True, now=self.now))
        sim.bind(self.row())
        for row in (None, self.row(key="baccarat02")):
            with self.subTest(row=row):
                self.assertFalse(sim.start(row, verified=True, now=self.now))
                self.assertFalse(sim.running)
                self.assertIsNone(sim.pending)
                self.assert_pristine_book(sim)

    def test_start_rejects_stale_rows_but_accepts_twenty_second_boundary(self):
        sim = self.simulation(start=False)
        self.assertFalse(sim.start(self.row(ts=self.now - app.LIVE_STALE - 0.01),
                                   verified=True, now=self.now))
        self.assertIsNone(sim.pending)
        self.assert_pristine_book(sim)
        self.assertTrue(sim.start(self.row(ts=self.now - app.LIVE_STALE),
                                  verified=True, now=self.now))

    def test_start_rejects_unstable_or_conflicting_rows(self):
        for changes in ({"stable": 0}, {"stable": 1}, {"conflict": True},
                        {"stable": 1, "conflict": True}):
            with self.subTest(changes=changes):
                sim = self.simulation(start=False)
                self.assertFalse(sim.start(self.row(**changes), verified=True, now=self.now))
                self.assertFalse(sim.running)
                self.assertIsNone(sim.pending)
                self.assert_pristine_book(sim)

    def test_start_rejects_unidentified_table_labels(self):
        for label in ("Ban 01", "Baccarat", ""):
            with self.subTest(label=label):
                sim = self.simulation(start=False)
                self.assertFalse(sim.start(self.row(label=label), verified=True, now=self.now))
                self.assertIsNone(sim.pending)
                self.assert_pristine_book(sim)

    def test_changed_history_must_be_rebound_and_reverified(self):
        sim = self.simulation(start=False)
        changed = self.row(BASELINE + "P")
        self.assertTrue(ME.check_table(list(changed["letters"]))["eligible"])
        self.assertFalse(sim.start(changed, verified=True, now=self.now))
        self.assertEqual(sim.letters, changed["letters"])
        self.assertFalse(sim.running)
        self.assertIsNone(sim.pending)
        self.assertFalse(sim.start(changed, now=self.now))
        self.assert_pristine_book(sim)
        self.assertTrue(sim.start(changed, verified=True, now=self.now))
        self.assertEqual(sim.pending["baseline"], changed["letters"])

    def test_start_rejects_ineligible_history_even_when_verified(self):
        # Loại bàn khi: dưới 10 tay, trên 50 tay, hoặc đã có bệt 4 tay trở lên.
        for letters in (BASELINE[:9], BASELINE * 4, "BBBBBBBPPBPPBPB"):
            with self.subTest(letters=letters):
                self.assertFalse(ME.check_table(list(letters))["eligible"])
                sim = app.LiveSimulation()
                row = self.row(letters)
                sim.bind(row)
                self.assertFalse(sim.start(row, verified=True, now=self.now))
                self.assertFalse(sim.running)
                self.assertIsNone(sim.pending)
                self.assert_pristine_book(sim)

    def test_table_exits_after_a_live_road_exceeds_its_streak_filter(self):
        book = M.MoneyBook(capital=CAPITAL, unit=UNIT, take_profit_pct=100)
        ladder = [UNIT * m for m in (1, 3, 7, 15, 32, 64)]
        sim = app.LiveSimulation(book, ladder=ladder, max_streak=6)
        sim.bind(self.row(PLAYER_BASELINE))
        self.assertTrue(sim.start(self.row(PLAYER_BASELINE), verified=True, now=self.now))

        for _ in range(6):
            self.settle_next(sim, "P")

        self.assertFalse(sim.running)
        self.assertTrue(sim.cycle_done)
        self.assertTrue(sim.filter_exit_done)
        self.assertFalse(sim.take_profit_done)
        self.assertEqual(sim.block_hands, 6)
        self.assertEqual(sim.money.hands_played, 6)
        self.assertIn("bệt Player 7 tay", sim.note)
        self.assertIsNone(sim.pending)

    def test_blocked_book_cannot_start(self):
        sim = self.simulation(start=False)
        sim.money.blocked = ("TAKE_PROFIT", "Chốt lời của phiên đã chạm")
        before = self.snapshot(sim)
        self.assertFalse(sim.start(self.row(), verified=True, now=self.now))
        self.assertEqual(sim.note, "Chốt lời của phiên đã chạm")
        self.assertFalse(sim.running)
        self.assertIsNone(sim.pending)
        self.assertEqual(self.snapshot(sim), before)

    def test_banker_win_pays_ninety_five_percent(self):
        sim = self.simulation()
        entry = self.settle_next(sim, "B")
        self.assertEqual(entry["outcome"], "win")
        self.assertEqual(entry["delta"], 95_000)
        self.assertEqual(sim.money.profit, 95_000)
        self.assertEqual(sim.money.total_returned, 195_000)
        self.assertEqual(sim.money.total_staked, UNIT)
        self.assertEqual((sim.money.hands_played, sim.money.wins, sim.money.losses), (1, 1, 0))

    def test_player_win_pays_one_hundred_percent(self):
        sim = self.simulation(PLAYER_BASELINE)
        self.assertEqual(sim.pending["side"], "P")
        entry = self.settle_next(sim, "P")
        self.assertEqual(entry["outcome"], "win")
        self.assertEqual(entry["delta"], UNIT)
        self.assertEqual(sim.money.profit, UNIT)
        self.assertEqual(sim.money.total_returned, 2 * UNIT)
        self.assertEqual(sim.money.total_staked, UNIT)
        self.assertEqual((sim.money.hands_played, sim.money.wins, sim.money.losses), (1, 1, 0))

    def test_losses_deduct_full_stake_and_advance_ladder(self):
        for baseline, result in ((BASELINE, "P"), (PLAYER_BASELINE, "B")):
            with self.subTest(baseline=baseline):
                sim = self.simulation(baseline)
                entry = self.settle_next(sim, result)
                self.assertEqual(entry["outcome"], "loss")
                self.assertEqual(entry["delta"], -UNIT)
                self.assertEqual(sim.money.profit, -UNIT)
                self.assertEqual(sim.money.total_returned, 0)
                self.assertEqual(sim.money.losses, 1)
                self.assertEqual(sim.method_state["ladder_step"], 1)
                self.assertEqual(sim.pending["stake"], 3 * UNIT)

    def test_ties_count_cycle_hands_but_hold_ladder_and_refund_stake(self):
        sim = self.simulation()
        self.settle_next(sim, "P")
        for index, side in enumerate("BBPPP", start=1):
            self.assertEqual(sim.pending["side"], side)
            self.assertEqual(sim.pending["stake"], 3 * UNIT)
            self.assertEqual(sim.pending["block_pos"], index)
            entry = self.settle_next(sim, "T")
            self.assertEqual((entry["delta"], entry["outcome"]), (0, "push"))
            self.assertEqual(sim.money.profit, -UNIT)
            self.assertEqual(sim.method_state["ladder_step"], 1)
            self.assertEqual(sim.method_state["hands_in_block"], (index + 1) % 6)
        # Hết 6 tay vẫn ở lại bàn: khối kế tiếp được chốt ngay, không rời bàn.
        self.assertTrue(sim.running)
        self.assertFalse(sim.cycle_done)
        self.assertFalse(sim.take_profit_done)
        self.assertEqual(sim.pending["block_pos"], 0)
        self.assertEqual(sim.pending["side"], sim.pending["strong"])
        self.assertEqual((sim.money.hands_played, sim.money.pushes, sim.money.loss_streak), (6, 5, 1))
        self.assertEqual(sim.money.total_staked, 16 * UNIT)
        self.assertEqual(sim.money.total_returned, 15 * UNIT)
        self.assertEqual(sim.money.ties_bet, 0)

    def test_cycle_bets_three_strong_then_three_opposite_for_either_side(self):
        for baseline, expected in ((BASELINE, "BBBPPP"), (PLAYER_BASELINE, "PPPBBB")):
            with self.subTest(baseline=baseline):
                sim = self.simulation(baseline)
                for position, side in enumerate(expected):
                    self.assertEqual(sim.pending["side"], side)
                    self.assertEqual(sim.pending["block_pos"], position)
                    self.assertEqual(sim.pending["stake"], UNIT)
                    self.settle_next(sim, "T")
                self.assertEqual("".join(entry["bet"] for entry in sim.log), expected)
                # Hết khối thì lặp lại tại chỗ: tay kế tiếp mở khối mới cùng bàn.
                self.assertTrue(sim.running)
                self.assertIsNotNone(sim.pending)
                self.assertFalse(sim.cycle_done)
                self.assertFalse(sim.take_profit_done)
                self.assertEqual(sim.pending["block_pos"], 0)
                self.assertEqual(sim.pending["side"], expected[0])
                self.assertEqual(sim.block_hands, sim.cycle_hands)

    def test_win_resets_ladder_without_resetting_cycle(self):
        sim = self.simulation()
        self.settle_next(sim, "P")
        self.settle_next(sim, "P")
        self.assertEqual(sim.pending["stake"], 7 * UNIT)
        entry = self.settle_next(sim, "B")
        self.assertEqual(entry["delta"], 7 * UNIT * 0.95)
        self.assertEqual(sim.method_state["ladder_step"], 0)
        self.assertEqual(sim.method_state["hands_in_block"], 3)
        self.assertEqual(sim.money.loss_streak, 0)
        self.assertEqual(sim.pending["side"], "P")
        self.assertEqual(sim.pending["stake"], UNIT)
        self.assertEqual(sim.money.profit, 2.65 * UNIT)

    def test_six_losses_halt_ladder_not_capital(self):
        sim = self.simulation()
        ladder = sim.ladder
        self.assertGreater(sim.money.balance, sum(ladder))
        for side, result, step in zip("BBBPPP", "PPPBBB", range(6)):
            self.assertEqual(sim.pending["side"], side)
            self.assertEqual(sim.pending["stake"], ladder[step])
            self.settle_next(sim, result)
        self.assertEqual([entry["stake"] for entry in sim.log], list(ladder))
        self.assertEqual(sim.money.profit, -sum(ladder))
        self.assertEqual((sim.money.hands_played, sim.money.losses), (6, 6))
        self.assertIsNone(sim.money.blocked)
        self.assertEqual(sim.method_state["ladder_step"], 6)
        self.assertEqual(sim.plan()["action"], "halt")
        self.assertFalse(sim.running)
        self.assertIsNone(sim.pending)
        before = self.snapshot(sim)
        self.assertFalse(sim.process(self.row(sim.letters + "B"), now=self.now))
        self.assertFalse(sim.start(self.row(sim.letters), verified=True, now=self.now))
        self.assertEqual(self.snapshot(sim), before)

    def test_duplicate_rows_never_settle_or_rearm(self):
        sim = self.simulation()
        for after_settlement in (False, True):
            if after_settlement:
                self.settle_next(sim, "P")
            before = self.snapshot(sim)
            pending = sim.pending
            frozen = deepcopy(pending)
            with patch.object(sim.money, "settle", wraps=sim.money.settle) as settle:
                for stable in (2, 3, 10):
                    self.now += 1
                    self.assertFalse(sim.process(self.row(sim.letters, stable=stable), now=self.now))
                settle.assert_not_called()
            self.assertTrue(sim.running)
            self.assertIs(sim.pending, pending)
            self.assertEqual(sim.pending, frozen)
            self.assertEqual(self.snapshot(sim), before)

    def test_unstable_new_hand_waits_without_changing_pending_then_settles_once(self):
        sim = self.simulation()
        pending = sim.pending
        frozen = deepcopy(pending)
        before = self.snapshot(sim)
        self.now += 1
        with patch.object(sim.money, "settle", wraps=sim.money.settle) as settle:
            self.assertFalse(sim.process(self.row(BASELINE + "P", stable=1), now=self.now))
            settle.assert_not_called()
        self.assertTrue(sim.running)
        self.assertIs(sim.pending, pending)
        self.assertEqual(sim.pending, frozen)
        self.assertEqual(self.snapshot(sim), before)
        self.settle_next(sim, "P")
        self.assertEqual(sim.log[-1]["armed_at"], frozen["armed_at"])
        self.assertEqual(sim.log[-1]["bet"], "B")
        self.assertEqual(sim.log[-1]["stake"], UNIT)
        self.assertEqual(sim.money.hands_played, 1)

    def test_gaps_pause_without_backfilling_any_hands(self):
        for suffix in ("BP", "BPTB"):
            with self.subTest(suffix=suffix):
                sim = self.simulation()
                self.assert_pauses_without_backfill(sim, self.row(BASELINE + suffix))

    def test_shoe_resets_and_rewritten_history_pause_without_backfill(self):
        for letters in ("", "BPTB", BASELINE[:-1], PLAYER_BASELINE,
                        PLAYER_BASELINE + "P"):
            with self.subTest(letters=letters):
                sim = self.simulation()
                self.assert_pauses_without_backfill(sim, self.row(letters))

    def test_conflict_pauses_even_with_unstable_or_unchanged_history(self):
        for stable in (1, 2):
            for suffix in ("", "B"):
                with self.subTest(stable=stable, suffix=suffix):
                    sim = self.simulation()
                    self.assert_pauses_without_backfill(
                        sim, self.row(BASELINE + suffix, stable=stable, conflict=True))

    def test_stale_unchanged_rows_wait_but_stale_new_results_pause(self):
        for stable in (1, 2):
            with self.subTest(stable=stable, suffix=""):
                sim = self.simulation()
                pending = deepcopy(sim.pending)
                stale = self.row(BASELINE, stable=stable,
                                 ts=self.now - app.LIVE_STALE - 0.01)
                self.assertFalse(sim.process(stale, now=self.now))
                self.assertTrue(sim.running)
                self.assertEqual(sim.pending, pending)
                self.assertEqual(sim.money.hands_played, 0)
            with self.subTest(stable=stable, suffix="B"):
                sim = self.simulation()
                stale = self.row(BASELINE + "B", stable=stable,
                                 ts=self.now - app.LIVE_STALE - 0.01)
                self.assert_pauses_without_backfill(sim, stale)

    def test_missing_or_different_table_pauses_without_backfill(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                sim = self.simulation()
                row = None if missing else self.row(BASELINE + "B", key="baccarat02")
                self.assert_pauses_without_backfill(sim, row)

    def test_pause_and_verified_resume_do_not_count_hands_seen_while_paused(self):
        sim = self.simulation()
        pending = sim.pending
        frozen = deepcopy(pending)
        before = self.snapshot(sim)
        sim.pause("Manual pause")
        self.assertEqual(sim.note, "Manual pause")
        self.assertFalse(sim.running)
        self.assertIsNone(sim.pending)
        self.assertEqual(pending, frozen)
        changed = self.row(BASELINE + "PB")
        self.assertTrue(ME.check_table(list(changed["letters"]))["eligible"])
        self.assertFalse(sim.process(changed, now=self.now))
        self.assertEqual(self.snapshot(sim), before)
        self.assertFalse(sim.start(changed, verified=True, now=self.now))
        self.assertFalse(sim.start(changed, now=self.now))
        self.assert_pristine_book(sim)
        self.assertTrue(sim.start(changed, verified=True, now=self.now))
        self.assertEqual(sim.pending["baseline"], changed["letters"])
        self.settle_next(sim, "B")
        self.assertEqual(sim.money.hands_played, 1)
        self.assertEqual(sim.money.profit, 95_000)
        self.assertEqual(sim.log[0]["hand"], 18)

    def test_bind_another_table_resets_method_but_preserves_settled_ledger(self):
        sim = self.simulation()
        self.settle_next(sim, "P")
        ledger = deepcopy(vars(sim.money))
        log = deepcopy(sim.log)
        pending = sim.pending
        frozen = deepcopy(pending)
        sim.bind(self.row(PLAYER_BASELINE, key="baccarat02", label="Baccarat 02"))
        self.assertFalse(sim.running)
        self.assertIsNone(sim.pending)
        self.assertEqual(pending, frozen)
        self.assertEqual(sim.table_key, "baccarat02")
        self.assertEqual(sim.results, list(PLAYER_BASELINE))
        self.assertEqual(sim.method_state, app.fresh_method())
        self.assertEqual(vars(sim.money), ledger)
        self.assertEqual(sim.log, log)

    def test_insufficient_initial_stake_is_not_capped_to_balance(self):
        sim = self.simulation(capital=75_000, start=False)
        before = self.snapshot(sim)
        self.assertEqual(sim.plan()["stake"], UNIT)
        self.assertFalse(sim.start(self.row(), verified=True, now=self.now))
        self.assertFalse(sim.running)
        self.assertIsNone(sim.pending)
        self.assertEqual(sim.money.balance, 75_000)
        self.assertEqual(self.snapshot(sim), before)

    def test_insufficient_next_ladder_stake_pauses_instead_of_min_cap(self):
        sim = self.simulation(capital=350_000)
        self.settle_next(sim, "P")
        self.assertEqual(sim.money.balance, 250_000)
        self.assertIsNone(sim.money.blocked)
        self.assertEqual(sim.plan()["stake"], 300_000)
        self.assertFalse(sim.running)
        self.assertIsNone(sim.pending)
        self.assertEqual(sim.money.total_staked, UNIT)
        before = self.snapshot(sim)
        self.assertFalse(sim.process(self.row(sim.letters + "B"), now=self.now))
        self.assertEqual(self.snapshot(sim), before)

    def test_stake_exactly_equal_to_remaining_balance_can_be_armed(self):
        sim = self.simulation(capital=400_000)
        self.settle_next(sim, "P")
        self.assertTrue(sim.running)
        self.assertEqual(sim.money.balance, 300_000)
        self.assertEqual(sim.pending["stake"], 300_000)

    def test_table_exits_when_a_live_streak_breaks_the_entry_filter(self):
        for baseline, result in ((BASELINE, "P"), (PLAYER_BASELINE, "B")):
            with self.subTest(result=result):
                sim = self.simulation(baseline)
                for _ in range(ME.MAX_STREAK_ALLOWED + 1):
                    self.settle_next(sim, result)
                self.assertFalse(sim.running)
                self.assertTrue(sim.cycle_done)
                self.assertTrue(sim.filter_exit_done)
                self.assertFalse(sim.take_profit_done)
                self.assertIn("bệt", sim.note)
                self.assertIsNone(sim.pending)
                self.assertEqual(sim.money.hands_played, ME.MAX_STREAK_ALLOWED + 1)

    def test_take_profit_ends_the_table_instead_of_starting_another_block(self):
        # Vốn nhỏ + mục tiêu 1%: chỉ một tay thắng là chạm mục tiêu.
        book = M.MoneyBook(capital=UNIT, unit=UNIT, take_profit_pct=1)
        sim = app.LiveSimulation(book, ladder=[UNIT * m for m in (1, 3, 7, 15, 32, 64)])
        row = self.row(BASELINE)
        sim.bind(row)
        self.assertTrue(sim.start(row, verified=True, now=self.now))
        winning_side = sim.pending["side"]
        self.settle_next(sim, winning_side)
        self.assertEqual(sim.money.blocked[0], "TAKE_PROFIT")
        self.assertTrue(sim.take_profit_done)
        self.assertTrue(sim.cycle_done, "phải báo cho app đóng bàn")
        self.assertFalse(sim.running, "chốt lời xong là dừng, không lặp khối mới")
        self.assertIsNone(sim.pending)
        self.assertIn("mục tiêu lãi", sim.note)

    def test_ties_alone_never_reach_take_profit(self):
        book = M.MoneyBook(capital=UNIT, unit=UNIT, take_profit_pct=1)
        sim = app.LiveSimulation(book, ladder=[UNIT * m for m in (1, 3, 7, 15, 32, 64)])
        row = self.row(BASELINE)
        sim.bind(row)
        self.assertTrue(sim.start(row, verified=True, now=self.now))
        for _ in range(sim.cycle_hands):
            self.assertFalse(sim.take_profit_done)
            self.settle_next(sim, "T")
        self.assertFalse(sim.take_profit_done)
        self.assertFalse(sim.cycle_done)
        self.assertTrue(sim.running)
        self.assertEqual(sim.money.profit, 0)

    def test_default_now_uses_clock_without_real_waiting(self):
        sim = self.simulation(start=False)
        row = self.row()
        self.assertTrue(sim.start(row, verified=True))
        self.now += app.LIVE_STALE + 1
        self.assertFalse(sim.process(row))
        self.assertTrue(sim.running)
        self.assertIsNotNone(sim.pending)
        self.assert_pristine_book(sim)


class SessionLimitTests(unittest.TestCase):
    def test_hand_count_limit_is_gone_from_the_book(self):
        book = M.MoneyBook(capital=CAPITAL, unit=UNIT, take_profit_pct=100)
        self.assertFalse(hasattr(book, "max_hands"))
        with self.assertRaises(TypeError):
            M.MoneyBook(capital=CAPITAL, unit=UNIT, max_hands=50)

    def test_a_long_session_never_stops_on_number_of_hands(self):
        book = M.MoneyBook(capital=CAPITAL, unit=UNIT, take_profit_pct=100)
        for _ in range(200):
            book.settle(E.BANKER, E.BANKER, UNIT, E.PAYOUT)
        self.assertEqual(book.hands_played, 200)
        self.assertIsNone(book.blocked)


class MethodProfileSimulationTests(unittest.TestCase):
    LADDER_32 = [40_000, 160_000, 360_000, 740_000, 1_700_000]

    def setUp(self):
        self.now = 1_800_000_000.0
        clock = patch.object(app.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def row(self, letters, **changes):
        row = dict(key="baccarat01", label="Baccarat 01", letters=letters, ts=self.now,
                   stable=2, conflict=False)
        row.update(changes)
        return row

    def simulation(self, profile, ladder):
        book = M.MoneyBook(capital=CAPITAL, unit=ladder[0], take_profit_pct=100)
        sim = app.LiveSimulation(book, profile=profile, ladder=ladder)
        row = self.row(BASELINE)
        sim.bind(row)
        self.assertTrue(sim.start(row, verified=True, now=self.now))
        return sim

    def test_three_two_bets_five_hands_with_the_amounts_the_user_typed(self):
        sim = self.simulation("3-2", self.LADDER_32)
        self.assertEqual(sim.cycle_hands, 5)
        for position in range(5):
            self.assertEqual(sim.pending["stake"], self.LADDER_32[position], "bậc %d" % position)
            self.assertEqual(sim.pending["block_pos"], position)
            self.now += 1
            losing = "P" if sim.pending["side"] == "B" else "B"
            row = self.row(sim.letters + losing, key=sim.table_key, label=sim.table_name)
            self.assertTrue(sim.process(row, now=self.now))
        self.assertEqual([entry["stake"] for entry in sim.log], [float(s) for s in self.LADDER_32])
        self.assertEqual(sim.money.total_staked, sum(self.LADDER_32))
        # Thua hết 5 bậc thì dừng vì cạn thang, KHÔNG phải vì hết khối 3-2.
        self.assertFalse(sim.running)
        self.assertFalse(sim.cycle_done)
        self.assertFalse(sim.take_profit_done)
        self.assertIsNone(sim.pending)
        self.assertIn("thua hết 5 bậc", sim.note)

    def test_each_profile_starts_with_its_own_default_ladder(self):
        self.assertEqual(app.LiveSimulation().ladder, [float(s) for s in ME.DEFAULT_LADDER["3-3"]])
        short = app.LiveSimulation(profile="3-2")
        self.assertEqual(short.ladder, [float(s) for s in ME.DEFAULT_LADDER["3-2"]])
        self.assertEqual(short.cycle_hands, 5)

    def test_a_ladder_written_for_another_block_is_refused(self):
        with self.assertRaises(ValueError):
            app.LiveSimulation(profile="3-3", ladder=self.LADDER_32)

    def test_profile_and_ladder_survive_a_state_round_trip(self):
        sim = self.simulation("3-2", self.LADDER_32)
        settings = dict(profile=sim.profile, ladder=sim.ladder)
        restored = app.LiveSimulation()
        restored.set_method(settings["profile"], settings["ladder"])
        self.assertEqual((restored.profile, restored.ladder, restored.cycle_hands),
                         ("3-2", [float(s) for s in self.LADDER_32], 5))

    def test_switching_profile_mid_session_uses_the_new_ladder_next_hand(self):
        sim = self.simulation("3-3", ME.DEFAULT_LADDER["3-3"])
        self.assertEqual(sim.pending["stake"], sim.ladder[0])
        sim.set_method("3-2", self.LADDER_32)
        sim._arm()
        self.assertEqual(sim.pending["stake"], self.LADDER_32[0])
        self.assertEqual(sim.cycle_hands, 5)


class CanvasMetadataTests(unittest.TestCase):
    def row(self, **updates):
        row = dict(key="id:tableABC", table_id="tableABC", label="Baccarat Tốc Độ A",
                   game_type="baccarat", letters=BASELINE, ts=app.time.time(), stable=2, conflict=False)
        row.update(updates)
        return row

    def test_table_id_allows_translated_name_without_digits(self):
        row = self.row()
        self.assertEqual(app.row_problem(row), "")
        sim = app.LiveSimulation()
        sim.bind(row)
        self.assertTrue(sim.start(row, verified=True))

    def test_special_payout_table_is_not_simulated(self):
        row = self.row(game_type="luckybaccarat")
        sim = app.LiveSimulation()
        sim.bind(row)
        self.assertFalse(sim.start(row, verified=True))
        self.assertEqual(sim.money.profit, 0)

    def test_ambiguous_first_tie_cannot_start(self):
        row = self.row(read_error="Ô đầu có hòa")
        sim = app.LiveSimulation()
        sim.bind(row)
        self.assertFalse(sim.start(row, verified=True))

    def test_reader_error_pauses_even_before_second_confirmation(self):
        row = self.row()
        sim = app.LiveSimulation()
        sim.bind(row)
        self.assertTrue(sim.start(row, verified=True))
        self.assertFalse(sim.process(self.row(letters=BASELINE + "B", stable=1, read_error="Sai định dạng")))
        self.assertFalse(sim.running)
        self.assertEqual(sim.money.profit, 0)


class LobbySweepTests(unittest.TestCase):
    def test_results_decode_in_array_order(self):
        letters, error = LS.decode_results([{"pos": [0, 0], "c": "R"}, {"pos": [1, 0], "c": "B", "ties": 2},
                                            {"pos": [2, 0], "c": "R"}])
        self.assertEqual((letters, error), ("BPTTB", ""))

    def test_ambiguous_or_truncated_heads_are_flagged(self):
        self.assertEqual(LS.decode_results([{"pos": [0, 0], "c": "R", "ties": 1}])[1],
                         "Ô đầu có hòa: Big Road không cho biết hòa trước hay sau tay đầu.")
        self.assertTrue(LS.decode_results([{"pos": [3, 1], "c": "R"}])[1])

    def test_unknown_formats_are_rejected_without_guessing(self):
        for results in ([{"pos": [0, 0], "c": "G"}], [{"pos": [0, 0], "c": "R", "ties": -1}],
                        [{"c": "R"}], [{"pos": [0, 0], "c": "R", "ties": True}], "not-a-list"):
            letters, error = LS.decode_results(results)
            self.assertEqual(letters, "", results)
            self.assertTrue(error, results)

    def test_special_payout_variants_are_detected(self):
        for table in ({"id": "LightningBac0001", "gameDetails": {"gameType": "baccarat"}},
                      {"id": "custom", "gameDetails": {"gameType": "baccarat", "gameConfig": {"lightning": True}}},
                      {"id": "custom", "gameDetails": {"gameType": "luckybaccarat"}}):
            self.assertTrue(LS.is_special(table), table)
        self.assertFalse(LS.is_special({"id": "custom", "gameDetails": {"gameType": "baccarat"}}))

    def test_table_screen_row_keeps_the_lobby_table_name(self):
        now = 1000.0
        host = SimpleNamespace(tables={"id:t1": dict(key="id:t1", table_id="t1", label="Baccarat Nhanh Thái B",
                                                     letters="BPBP", ts=now - 3, stable=2, conflict=False,
                                                     game_type="baccarat", read_error="", source="evo-ws-histories")},
                               sim=app.LiveSimulation(), verified=Mock())
        app.BCRApp._ingest_reports(host, [{"tables": [dict(label="t1", letters="BPBPT", table_id="t1",
                                                            game_type="", read_error="", source="evo-bead")]}], now)
        row = host.tables["id:t1"]
        self.assertEqual((row["label"], row["source"], row["letters"]),
                         ("Baccarat Nhanh Thái B", "evo-bead", "BPBPT"))


class LobbyEntryTests(unittest.TestCase):
    """Transcript CDP giả lập; không mở Chrome thật và không bấm cược."""

    PORTAL = dict(id="portal", type="page", url=BR.START_URL,
                  webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/portal")
    LOBBY = dict(id="lobby", type="page",
                 url="https://tmrw17k36as.evo-games.com/frontend/evo/r3/#category=all_games",
                 webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/lobby")

    class Socket:
        def __init__(self, messages):
            self.pending = list(messages)
            self.sent = []
            self.closed = False

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self):
            return json.dumps(self.pending.pop(0))

        def settimeout(self, _seconds):
            pass

        def close(self):
            self.closed = True

    def setUp(self):
        self.session = Mock()
        self.stream = LS.LobbyStream(self.session)
        # Giữ bản bấm 3 sự kiện cố định để các test cũ đếm đúng số lệnh CDP;
        # test riêng bên dưới bật human_mouse để kiểm tra đường lướt nhiều điểm.
        self.stream.human_mouse = False
        self.sockets = []

        def connect(endpoint, **kwargs):
            self.last_kwargs = kwargs
            socket = self.Socket(self.scripted[len(self.sockets)])
            self.sockets.append(socket)
            return socket

        self.websocket = Mock(spec=["create_connection"])
        self.websocket.create_connection.side_effect = connect
        patcher = patch.dict(sys.modules, {"websocket": self.websocket})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = patch("time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def click_transcript(self, value):
        return [[{"method": "Runtime.executionContextCreated",
                  "params": {"context": {"id": 7, "auxData": {"isDefault": True}}}},
                 {"id": 1, "result": {}},
                 {"id": 2, "result": {"result": {"value": value}}},
                 {"id": 3, "result": {}}, {"id": 4, "result": {}},
                 {"id": 5, "result": {}}, {"id": 6, "result": {}}]]

    def test_clicks_the_evolution_button_and_opens_the_lobby_without_rewriting_the_url(self):
        box = {"x": 320.5, "y": 640.25, "label": "Chơi game"}
        self.scripted = self.click_transcript(box)
        self.session._cdp_pages.side_effect = [[self.PORTAL], [self.PORTAL, self.LOBBY]]
        ok, note = self.stream.open_lobby_from_portal()
        self.assertTrue(ok, note)
        self.assertIn("sảnh Evolution", note)
        portal_socket, = self.sockets
        clicks = [message for message in portal_socket.sent
                  if message["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual([message["params"]["type"] for message in clicks],
                         ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertEqual({message["params"]["x"] for message in clicks}, {box["x"]})
        self.assertTrue(portal_socket.closed)
        self.assertNotIn("location.hash", json.dumps(portal_socket.sent))

    def test_uses_the_portal_tab_and_leaves_a_tab_that_is_already_open_untouched(self):
        self.session._cdp_pages.return_value = [self.PORTAL, dict(self.LOBBY, url=LOBBY_URL)]
        self.scripted = self.click_transcript({"x": 10.0, "y": 20.0})
        ok, note = self.stream.open_lobby_from_portal()
        self.assertTrue(ok, note)
        self.assertEqual(len(self.sockets), 1, "Không mở thêm kênh CDP vào tab sảnh đã có")
        self.assertNotIn("location.hash", json.dumps(self.sockets[0].sent))

    def test_reports_why_the_evolution_button_was_not_clickable(self):
        for reason in ("evo_item_not_found", "evo_button_not_found", "evo_button_pending",
                       "evo_card_blocked", "evo_button_ambiguous"):
            with self.subTest(reason=reason):
                self.sockets, self.scripted = [], self.click_transcript({"reason": reason})
                self.session._cdp_pages.return_value = [self.PORTAL]
                # wait_seconds=0: không chờ retry cuộn-nút-render, lấy ngay lý do của lượt đọc đầu.
                ok, note = self.stream.open_lobby_from_portal(wait_seconds=0)
                self.assertFalse(ok)
                self.assertIn(reason, note)
                self.assertNotIn("Input.dispatchMouseEvent",
                                 [message["method"] for message in self.sockets[0].sent])

    def test_an_iframe_answer_never_ends_the_retry_or_the_report(self):
        # Mọi iframe đều trả not_top_frame nên lý do đó vô nghĩa; chỉ khung chính được tin.
        self.sockets, self.scripted = [], self.click_transcript({"reason": "not_top_frame"})
        self.session._cdp_pages.return_value = [self.PORTAL]
        ok, note = self.stream.open_lobby_from_portal(wait_seconds=0)
        self.assertFalse(ok)
        self.assertNotIn("not_top_frame", note)

    def test_clicks_the_evo_card_when_the_start_button_stays_hidden(self):
        box = {"x": 167, "y": 479, "via": "card", "label": "LIVE-EVO"}
        self.scripted = self.click_transcript(box)
        self.session._cdp_pages.side_effect = [[self.PORTAL], [self.PORTAL, self.LOBBY]]
        ok, note = self.stream.open_lobby_from_portal()
        self.assertTrue(ok, note)
        clicks = [message for message in self.sockets[0].sent
                  if message["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual({message["params"]["x"] for message in clicks}, {box["x"]})

    def test_reports_when_there_is_no_usable_tab_before_clicking_anything(self):
        # Không còn trang chủ cố định: chỉ cần KHÔNG có tab HTTPS công khai nào.
        self.session._cdp_pages.return_value = []
        ok, note = self.stream.open_lobby_from_portal()
        self.assertFalse(ok)
        self.assertTrue(note)
        self.websocket.create_connection.assert_not_called()

    def test_reports_when_the_lobby_tab_never_appears(self):
        self.scripted = self.click_transcript({"x": 10.0, "y": 20.0})
        self.session._cdp_pages.side_effect = [[self.PORTAL]] * 5
        ok, note = self.stream.open_lobby_from_portal(wait_seconds=0)
        self.assertFalse(ok)
        self.assertIn("chưa thấy tab sảnh", note)

    def test_accepts_a_lobby_embedded_in_the_entry_page(self):
        portal = self.click_transcript({"x": 10.0, "y": 20.0})
        portal[0].append({"id": 7, "result": {"result": {"value": {
            "tiles": [{"id": "t1", "title": "Baccarat A", "top": 40}],
            "scroll": {"top": 0, "max": 900, "client": 600}}}}})
        self.scripted = portal
        self.session._cdp_pages.return_value = [self.PORTAL]
        ok, note = self.stream.open_lobby_from_portal()
        self.assertTrue(ok, note)
        self.assertIn("Quét toàn sảnh", note)
        self.assertEqual(len(self.sockets), 1, "Sảnh trong iframe thì không mở tab nào thêm")

    def test_wheel_scroll_uses_real_mouse_wheel_when_human_mouse_is_on(self):
        self.stream.human_mouse = True
        socket = self.Socket([
            {"id": 1, "result": {}},                               # cài overlay con trỏ (addScriptToEvaluateOnNewDocument)
            {"id": 2, "result": {}},                               # cài overlay con trỏ (Runtime.evaluate)
            {"id": 3, "result": {"result": {"value": 0}}},        # đọc scrollTop hiện tại
            {"id": 4, "result": {}},                               # cú lăn chuột đưa về đích
            {"id": 5, "result": {"result": {"value": 500}}},       # đã đạt đích sau cú lăn
            {"id": 6, "result": {"result": {"value": 500}}},       # chốt đúng pixel bằng JS
        ])
        box = {"x": 100.0, "y": 200.0, "width": 300.0, "height": 400.0}
        # move_to sinh số điểm ngẫu nhiên theo ghost-cursor; chỉ cần xác nhận NÓ được
        # gọi đúng tâm khung, không cần khớp từng sự kiện mouseMoved trong transcript.
        with patch.object(HM.HumanMouse, "move_to", return_value=[]) as move_to:
            top = self.stream._wheel_scroll_to(socket, 9, 500, box)
        self.assertEqual(top, 500)
        move_to.assert_called_once_with(250.0, 400.0)
        wheels = [m for m in socket.sent if m["method"] == "Input.dispatchMouseWheelEvent"]
        self.assertTrue(wheels, "phải lăn chuột thật, không gán scrollTop trực tiếp")
        self.assertTrue(all(w["params"]["x"] == 250.0 and w["params"]["y"] == 400.0
                            for w in wheels), "phải lăn ngay giữa khung danh sách")
        self.assertTrue(all(w["params"]["deltaY"] > 0 for w in wheels))

    def test_wheel_scroll_falls_back_to_js_when_human_mouse_is_off(self):
        self.stream.human_mouse = False
        socket = self.Socket([{"id": 1, "result": {"result": {"value": 240}}}])
        top = self.stream._wheel_scroll_to(socket, 9, 240,
                                           {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0})
        self.assertEqual(top, 240)
        self.assertNotIn("Input.dispatchMouseWheelEvent", [m["method"] for m in socket.sent])
        self.assertNotIn("Input.dispatchMouseEvent", [m["method"] for m in socket.sent])

    def status_transcript(self, value):
        return [[{"method": "Runtime.executionContextCreated",
                  "params": {"context": {"id": 9, "auxData": {"isDefault": True}}}},
                 {"id": 1, "result": {}},
                 {"id": 2, "result": {"result": {"value": value}}}]]

    def test_recognizes_the_baccarat_tab_the_user_opened_without_touching_it(self):
        self.scripted = self.status_transcript({"tiles": 8, "loading": False})
        self.session._cdp_pages.return_value = [self.PORTAL, dict(self.LOBBY, url=LOBBY_URL)]
        ok, note = self.stream.find_lobby_tab()
        self.assertTrue(ok, note)
        self.assertIn("Đã nhận tab sảnh", note)
        self.assertNotIn("location.hash", json.dumps(self.sockets[0].sent))
        self.assertTrue(self.sockets[0].closed)

    def test_accepts_a_lobby_tab_on_another_category_without_rewriting_its_url(self):
        self.scripted = self.status_transcript({"tiles": 4, "loading": False})
        self.session._cdp_pages.return_value = [self.PORTAL, self.LOBBY]
        ok, note = self.stream.find_lobby_tab()
        self.assertTrue(ok, note)
        self.assertIn("Đã nhận tab sảnh", note)
        socket, = self.sockets
        self.assertNotIn("Input.dispatchMouseEvent", [m["method"] for m in socket.sent])
        self.assertNotIn("location.hash", json.dumps(socket.sent))
        self.assertTrue(socket.closed)

    def test_keeps_an_open_table_screen_untouched_and_says_so(self):
        table_url = LOBBY_URL + "&table_id=t1"
        self.scripted = self.status_transcript({"tiles": 0, "roads": 1})
        self.session._cdp_pages.return_value = [dict(self.LOBBY, url=table_url)]
        ok, note = self.stream.find_lobby_tab()
        self.assertTrue(ok, note)
        self.assertIn("màn hình bàn", note)
        self.assertNotIn("table_id", json.dumps(self.sockets[0].sent))
        self.assertNotIn("location.hash", json.dumps(self.sockets[0].sent))

    def test_asks_the_user_to_open_the_lobby_when_no_lobby_tab_exists(self):
        self.session._cdp_pages.return_value = [self.PORTAL]
        ok, note = self.stream.find_lobby_tab()
        self.assertFalse(ok)
        self.assertIn("Chưa thấy tab sảnh", note)
        self.websocket.create_connection.assert_not_called()

    def test_accepts_the_tab_when_the_health_read_cannot_answer(self):
        self.scripted = [[]]
        self.session._cdp_pages.return_value = [self.LOBBY]
        ok, note = self.stream.find_lobby_tab()
        self.assertTrue(ok, note)
        self.assertIn("Đã nhận tab sảnh", note)

    def test_reports_ev7_and_never_claims_the_expired_tab_is_ready(self):
        self.scripted = self.status_transcript({"auth": True,
                                                "body": "User authentication failed. Error Code: EV.7"})
        self.session._cdp_pages.return_value = [self.LOBBY]
        ok, note = self.stream.find_lobby_tab()
        self.assertFalse(ok)
        self.assertIn("EV.7", note)
        self.assertIn("không đổi link", note)

    def test_reports_a_launchgame_failure_instead_of_waiting_for_tiles(self):
        self.scripted = self.status_transcript({"launch_failed": True,
                                                "body": "Mở game không thành công"})
        self.session._cdp_pages.return_value = [self.LOBBY]
        ok, note = self.stream.find_lobby_tab()
        self.assertFalse(ok)
        self.assertIn("Mở game không thành công", note)


    def test_a_chrome_without_the_debug_port_is_not_reported_as_a_missing_tab(self):
        self.session._cdp_pages.side_effect = URLError(ConnectionRefusedError(10061, "chi tiết thô"))
        ok, note = self.stream.find_lobby_tab()
        self.assertFalse(ok)
        self.assertIn("Mở Chrome", note)

    def test_a_sweep_against_a_closed_debug_port_blames_the_window_not_the_lobby(self):
        self.session._cdp_page.side_effect = URLError(ConnectionRefusedError(10061, "chi tiết thô"))
        self.assertEqual(self.stream.sweep(max_seconds=1), [])
        self.assertIn("Mở Chrome", self.stream.last_error)


class LoggedOutModalDetectionTests(unittest.TestCase):
    """Sảnh bản tiếng Việt báo mất phiên bằng hộp “Bạn đã đăng xuất. Vui lòng đóng cửa
    sổ này lại rồi đăng nhập lại để chơi.” — không có dòng tiếng Anh nào, nên chỗ nào
    chỉ dò “User authentication failed”/EV.7 sẽ coi trang chết là sảnh bình thường và
    tiếp tục bấm, trong khi site đã đá phiên ra."""

    MODAL = "Bạn đã đăng xuất. Vui lòng đóng cửa sổ này lại rồi đăng nhập lại để chơi. OK"
    EV7 = "User authentication failed. Error Code:  EV.7"
    HEALTHY = "Baccarat · 54 bàn đang mở · cầu đầy đủ"

    def auth_pattern(self, script, label):
        match = re.search(r"auth(?:_required)?\s*[:=]\s*/([^/]*)/i\.test", script)
        self.assertIsNotNone(match, "%s không còn regex phát hiện mất phiên" % label)
        return re.compile(match.group(1), re.IGNORECASE)

    def scripts(self):
        return ((LS.LOBBY_HEALTH_JS, "lobby_stream.LOBBY_HEALTH_JS"),
                (BR.BrowserSession()._reader_script(), "browser.BrowserSession._reader_script"))

    def test_both_auth_reads_accept_vietnamese_and_english_wording(self):
        for script, label in self.scripts():
            pattern = self.auth_pattern(script, label)
            self.assertTrue(pattern.search(self.MODAL), "%s: hộp đăng xuất tiếng Việt bị bỏ qua" % label)
            self.assertTrue(pattern.search(self.EV7), label)

    def test_neither_read_calls_a_healthy_lobby_logged_out(self):
        for script, label in self.scripts():
            self.assertFalse(self.auth_pattern(script, label).search(self.HEALTHY), label)

    def test_the_note_tells_the_user_to_sign_in_at_the_portal_not_to_edit_the_link(self):
        note = LS.LobbyStream._health_note({"status": "auth", "entry": None})
        self.assertIn("đăng nhập lại ở trang gốc", note)
        self.assertIn("app không đổi link sảnh", note)


class HumanMouseTests(unittest.TestCase):
    """Chuột người thật: đường cong, đúng đích, không vượt trần sự kiện CDP."""

    def test_windmouse_curves_but_finishes_exactly_on_the_target(self):
        start, end = (10.0, 10.0), (420.5, 260.25)
        points = HM.windmouse_points(start, end, rng=random.Random(7))
        self.assertGreater(len(points), 3)
        self.assertLessEqual(len(points), HM.MAX_POINTS)
        self.assertEqual(points[-1], end)
        dx, dy = end[0] - start[0], end[1] - start[1]
        span = math.hypot(dx, dy)
        offsets = [abs(dx * (y - start[1]) - dy * (x - start[0])) / span for x, y in points]
        self.assertGreater(max(offsets), 1.0, "Đường phải cong, không phải đường thẳng tắp")

    def test_bezier_style_also_finishes_on_the_target(self):
        points = HM.bezier_points((4.0, 8.0), (333.0, 222.0), rng=random.Random(2))
        self.assertGreater(len(points), 8)
        self.assertEqual(points[-1], (333.0, 222.0))

    def test_human_click_glides_then_presses_exactly_on_target(self):
        sent = []
        mouse = HM.HumanMouse(lambda method, params: sent.append((method, params)),
                              rng=random.Random(3))
        with patch("human_mouse.time.sleep"):
            mouse.click(300.0, 200.0)
        moved = [params for _, params in sent if params["type"] == "mouseMoved"]
        pressed = [params for _, params in sent if params["type"] == "mousePressed"]
        released = [params for _, params in sent if params["type"] == "mouseReleased"]
        self.assertGreater(len(moved), 3, "Phải lướt nhiều điểm thay vì nhảy thẳng")
        self.assertEqual([(p["x"], p["y"]) for p in pressed], [(300.0, 200.0)])
        self.assertEqual([(p["x"], p["y"]) for p in released], [(300.0, 200.0)])
        self.assertTrue(all(method == "Input.dispatchMouseEvent" for method, _ in sent))

    def test_disabled_mouse_keeps_the_three_event_click(self):
        sent = []
        mouse = HM.HumanMouse(lambda method, params: sent.append((method, params)),
                              enabled=False)
        mouse.click(12.5, 34.25)
        self.assertEqual([params["type"] for _, params in sent],
                         ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertEqual({params["x"] for _, params in sent}, {12.5})


class FavoriteFilterTests(unittest.TestCase):
    """Ô lọc Ưa thích: bấm đúng một lần, đọc đúng trạng thái, chỉ quét bàn đã thích."""

    def test_turning_on_favorites_clicks_the_filter_once(self):
        stream = LS.LobbyStream(Mock(), human_mouse=False)
        stream._command = Mock()
        stream._favorite_state = Mock(side_effect=[
            {"x": 40.0, "y": 70.0, "active": False},
            {"active": True},
        ])
        with patch("lobby_stream.time.sleep"):
            ok, supported, note = stream._set_favorite_filter(Mock(), 9, True, wait_seconds=1)
        self.assertTrue(ok, note)
        self.assertTrue(supported)
        types = [c.args[2]["type"] for c in stream._command.call_args_list
                 if c.args[1] == "Input.dispatchMouseEvent"]
        self.assertEqual(types, ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertIn("Page.bringToFront", [c.args[1] for c in stream._command.call_args_list])

    def test_already_on_favorites_never_clicks_again(self):
        stream = LS.LobbyStream(Mock(), human_mouse=False)
        stream._command = Mock()
        stream._favorite_state = Mock(return_value={"active": True})
        ok, supported, _ = stream._set_favorite_filter(Mock(), 9, True)
        self.assertTrue(ok and supported)
        stream._command.assert_not_called()

    def test_missing_favorites_filter_is_reported_as_unsupported(self):
        stream = LS.LobbyStream(Mock(), human_mouse=False)
        stream._favorite_state = Mock(return_value={"reason": "favorite_filter_not_found"})
        ok, supported, note = stream._set_favorite_filter(Mock(), 9, True)
        self.assertFalse(ok)
        self.assertFalse(supported)
        self.assertIn("Ưa thích", note)

    def test_sweep_keeps_only_the_tiles_still_visible_after_filtering(self):
        stream = LS.LobbyStream(Mock(), human_mouse=False)
        stream.session._cdp_page.return_value = dict(
            id="table", type="page", url=LOBBY_URL,
            webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/table")
        snapshot = {
            "tiles": [{"id": "t1", "title": "Baccarat 01", "road": []},
                      {"id": "t2", "title": "Baccarat 02", "road": []}],
            "scroll": {"top": 0, "max": 600, "client": 600},
        }
        results = [{"c": "R", "ties": 0, "pos": [0, 0]},
                   {"c": "B", "ties": 0, "pos": [1, 0]}]
        stream.histories = {
            table_id: {"results": results, "source": "lobby.histories", "ts": 1.0, "messages": 1}
            for table_id in ("t1", "t2", "t3")
        }
        stream._reset = Mock()
        stream._command = Mock()
        stream._wait_lobby_context = Mock(return_value=(9, snapshot))
        stream._set_favorite_filter = Mock(return_value=(True, True, ""))
        stream._favorite_state = Mock(return_value={"active": True})
        stream._try_value = Mock(return_value=snapshot)
        stream._evaluate = Mock(return_value={"result": {"value": snapshot}})
        websocket = Mock(create_connection=Mock(return_value=Mock()))
        with patch.dict(sys.modules, {"websocket": websocket}), patch("lobby_stream.time.sleep"):
            rows = stream.sweep(max_seconds=1, favorites_only=True)
        self.assertEqual([row["table_id"] for row in rows], ["t1", "t2"])
        self.assertTrue(all(row["favorite"] for row in rows))
        self.assertTrue(stream.diag["favorites_only"])
        stream._set_favorite_filter.assert_called_once()
        self.assertIs(stream._set_favorite_filter.call_args.args[2], True)

    def starred_sweep(self, favorite_flags, with_histories=True, health=None):
        """Dựng một lượt quét với sao “đã thích” ngay trong ô bàn (markup sảnh bản mới)."""
        stream = LS.LobbyStream(Mock(), human_mouse=False)
        stream.session._cdp_page.return_value = dict(
            id="table", type="page", url=LOBBY_URL,
            webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/table")
        snapshot = {
            "tiles": [{"id": "t%d" % index, "title": "Baccarat %d" % index, "road": [],
                       "favorite": flag} for index, flag in enumerate(favorite_flags, start=1)],
            "scroll": {"top": 0, "max": 600, "client": 600},
        }
        results = [{"c": "R", "ties": 0, "pos": [0, 0]}, {"c": "B", "ties": 0, "pos": [1, 0]}]
        stream.histories = {
            "t%d" % index: {"results": results, "source": "lobby.histories", "ts": 1.0, "messages": 1}
            for index in range(1, len(favorite_flags) + 1)} if with_histories else {}
        stream._reset = Mock()
        stream._command = Mock()
        stream._wait_lobby_context = Mock(return_value=(9, snapshot))
        stream._set_favorite_filter = Mock(side_effect=AssertionError("khong duoc bam bo loc"))
        stream._try_value = Mock(return_value=snapshot)
        stream._evaluate = Mock(return_value={"result": {"value": snapshot}})
        if health is not None:
            stream._health = Mock(return_value=health)
        websocket = Mock(create_connection=Mock(return_value=Mock()))
        with patch.dict(sys.modules, {"websocket": websocket}), patch("lobby_stream.time.sleep"):
            return stream, stream.sweep(max_seconds=1, favorites_only=True)

    def test_starred_tables_are_read_straight_from_the_tiles_without_any_click(self):
        stream, rows = self.starred_sweep([True, False, True])
        self.assertEqual([row["table_id"] for row in rows], ["t1", "t3"])
        self.assertTrue(all(row["favorite"] for row in rows))
        self.assertEqual(stream.favorite_mode, "stars")
        self.assertEqual(stream.diag["favorite_tiles"], 2)
        stream._set_favorite_filter.assert_not_called()

    def test_a_lobby_without_starred_tables_is_reported_instead_of_blamed_on_the_filter(self):
        stream, rows = self.starred_sweep([False, False])
        self.assertEqual(rows, [])
        self.assertIn("Không có bàn nào được đánh dấu Ưa thích", stream.last_error)
        stream._set_favorite_filter.assert_not_called()

    def test_silent_history_feed_keeps_scrolling_and_then_blames_the_socket(self):
        stream, rows = self.starred_sweep([True, True], with_histories=False)
        self.assertEqual(rows, [])
        self.assertIn("không gửi lịch sử bàn qua WebSocket", stream.last_error)
        self.assertGreaterEqual(stream.diag["scroll_steps"], 10)
        self.assertEqual(stream.diag["favorite_tiles"], 2)

    def test_a_logged_out_lobby_stops_the_hunt_instead_of_blaming_the_socket(self):
        """Hộp “Bạn đã đăng xuất” cũng khiến WebSocket im lặng. Nếu chỉ báo “sảnh không
        gửi lịch sử” thì vòng 15 giây tiếp tục cuộn trên trang đã chết."""
        stream, rows = self.starred_sweep(
            [True], with_histories=False,
            health={"status": "auth", "entry": None,
                    "body": "Bạn đã đăng xuất. Vui lòng đóng cửa sổ này lại rồi đăng nhập lại để chơi."})
        self.assertEqual(rows, [])
        self.assertTrue(stream.session_lost)
        self.assertNotIn("WebSocket", stream.last_error)
        self.assertIn("đăng nhập lại ở trang gốc", stream.last_error)

    def test_no_reader_script_asks_querySelector_for_a_namespaced_attribute(self):
        """Selector dạng [xlink:href=…] ném SyntaxError trong JS: cả đoạn đọc bàn chết,
        nên sảnh đang đầy bàn vẫn báo "chưa hiển thị danh sách bàn"."""
        for name in sorted(dir(LS)):
            script = getattr(LS, name)
            if isinstance(script, str) and script.lstrip().startswith("(() =>"):
                for selector in re.findall(r"""querySelectorAll?\(\s*['"]([^'"]+)['"]""", script):
                    for part in selector.split(","):
                        self.assertNotIn(":", part[part.index("["):] if "[" in part else "",
                                         "%s: %r" % (name, part.strip()))


class LobbyListReturnTests(unittest.TestCase):
    """Rời bàn: nút X bấm chuột thật → đổi hash về danh sách → mở URL sảnh tổng (CDP giả).

    Không dùng history.back(): mục /entry dùng một lần đứng đầu lịch sử tab launch,
    lùi tới đó là EV.7. Đồng hồ giả để các cửa sổ chờ chạy deterministik.
    """

    TABLE = dict(id="table", type="page",
                 url="https://tmrw17k36as.evo-games.com/frontend/evo/r3/#category=all_games&table_id=t1",
                 webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/table")
    LIST = dict(id="table", type="page", url=LOBBY_URL,
                webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/table")
    TILES = {"tiles": [{"id": "t2", "title": "Baccarat A", "top": 40}],
             "scroll": {"top": 0, "max": 900, "client": 600}}
    LIST_URL = LOBBY_ORIGIN + BR.LOBBY_PATH + "/#category=all_games"

    def setUp(self):
        self.session = Mock()
        self.stream = LS.LobbyStream(self.session)
        # Giữ đường bấm 3 sự kiện để transcript đếm đúng lệnh CDP; đường chuột người
        # thật đã có test riêng ở LobbyEntryTests.
        self.stream.human_mouse = False
        self.sockets = []
        self.now = 1_000.0

        def connect(endpoint, **kwargs):
            socket = LobbyEntryTests.Socket(self.scripted[len(self.sockets)])
            self.sockets.append(socket)
            return socket

        def nap(seconds):
            self.now += seconds

        patcher = patch.dict(sys.modules, {"websocket": Mock(create_connection=connect)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for target, side in (("time.monotonic", lambda: self.now), ("time.sleep", nap)):
            clock = patch(target, side_effect=side)
            clock.start()
            self.addCleanup(clock.stop)

    def context_event(self, context_id=9, frame_id=None):
        aux = {"isDefault": True}
        if frame_id:
            aux["frameId"] = frame_id
        return {"method": "Runtime.executionContextCreated",
                "params": {"context": {"id": context_id, "auxData": aux}}}

    def sent(self, method):
        return [message for message in self.sockets[0].sent if message["method"] == method]

    def test_lobby_list_url_strips_table_id_and_keeps_the_rest_of_the_view(self):
        base = LOBBY_ORIGIN + BR.LOBBY_PATH + "/"
        self.assertEqual(LS.lobby_list_url(base + "#category=all_games&table_id=t1"),
                         base + "#category=all_games")
        self.assertEqual(LS.lobby_list_url(base + "#table_id=t1"),
                         base + "#" + LS.LOBBY_LIST_FRAGMENT)
        self.assertEqual(LS.lobby_list_url(base + "?table_id=t1#category=all_games&see=baccarat"),
                         base + "#category=all_games&see=baccarat")
        self.assertEqual(LS.lobby_list_url(
                             base + "#category=all_games&game=baccarat&table_id=t1&see=baccarat"),
                         base + "#category=all_games&game=baccarat&see=baccarat")
        self.assertEqual(LS.lobby_list_url(base + "#table_id2=x&table_id=t1"),
                         base + "#table_id2=x")

    def test_missing_close_button_falls_back_to_the_lobby_hash(self):
        # Khung không có nút X: không bấm gì, đổi hash về danh sách (cùng document).
        # Context cũ của kết nối trước phải bị bỏ, không gửi lệnh vào id chết.
        self.stream.contexts = [77]
        self.stream.context_frames = {77: "old-frame"}
        self.scripted = [[self.context_event(),
                          {"id": 1, "result": {}},
                          {"id": 2, "result": {"result": {"value": None}}},
                          {"id": 3, "result": {"result": {"value": LOBBY_URL}}},
                          {"id": 4, "result": {"result": {"value": self.TILES}}}]]
        self.session._cdp_page.side_effect = [self.TABLE, self.LIST]
        ok, note = self.stream.back_to_lobby()
        self.assertTrue(ok, note)
        self.assertIn("về URL sảnh tổng bằng đổi hash", note)
        evaluate = self.sent("Runtime.evaluate")
        self.assertEqual(evaluate[0]["params"]["expression"], LS.CLOSE_TABLE_JS)
        self.assertEqual(evaluate[1]["params"]["expression"],
                         LS.LIST_HASH_JS % json.dumps("category=all_games"))
        self.assertEqual({message["params"].get("contextId") for message in evaluate}, {9, None})
        self.assertEqual(self.sent("Page.navigate"), [])
        self.assertEqual(self.sent("Input.dispatchMouseEvent"), [])
        self.assertNotIn("history.back()", json.dumps(self.sockets[0].sent))
        self.assertTrue(self.sockets[0].closed)

    def test_clicks_the_close_button_with_a_real_mouse_and_never_navigates(self):
        # Nút X người dùng chỉ ra: data-role="close-button". Bấm chuột thật (không phải
        # btn.click() tổng hợp); về được danh sách thì không đổi hash, không navigate.
        self.scripted = [[self.context_event(),
                          {"id": 1, "result": {}},
                          {"id": 2, "result": {"result": {"value": {
                              "x": 1180.0, "y": 42.0,
                              "frame_offset": {"x": 0, "y": 0, "depth": 0}}}}},
                          {"id": 3, "result": {}}, {"id": 4, "result": {}},
                          {"id": 5, "result": {}}, {"id": 6, "result": {}},
                          {"id": 7, "result": {"result": {"value": self.TILES}}}]]
        self.session._cdp_page.side_effect = [self.TABLE, self.LIST]
        ok, note = self.stream.back_to_lobby()
        self.assertTrue(ok, note)
        self.assertIn("danh sách bàn Baccarat hiển thị trở lại", note)
        clicks = [message["params"] for message in self.sent("Input.dispatchMouseEvent")
                  if message["params"]["type"] == "mousePressed"]
        self.assertEqual([(click["x"], click["y"]) for click in clicks], [(1180.0, 42.0)])
        sent = json.dumps(self.sockets[0].sent)
        self.assertNotIn("history.back()", sent)
        self.assertNotIn("location.hash", sent)
        self.assertEqual(self.sent("Page.navigate"), [])

    def test_finds_the_close_button_inside_the_table_frame_and_adds_the_offset(self):
        # Màn hình bàn có thể trong iframe: dò nút X ở mọi context, cộng lệch khung
        # (đo bằng JS cùng origin) trước khi bấm theo khung gốc của tab.
        self.scripted = [[self.context_event(),
                          self.context_event(11, "frame-table"),
                          {"id": 1, "result": {}},
                          {"id": 2, "result": {"result": {"value": None}}},
                          {"id": 3, "result": {"result": {"value": {
                              "x": 900.0, "y": 30.0,
                              "frame_offset": {"x": 10, "y": 20, "depth": 1}}}}},
                          {"id": 4, "result": {}}, {"id": 5, "result": {}},
                          {"id": 6, "result": {}}, {"id": 7, "result": {}},
                          {"id": 8, "result": {"result": {"value": self.TILES}}}]]
        self.session._cdp_page.side_effect = [self.TABLE, self.LIST]
        ok, note = self.stream.back_to_lobby()
        self.assertTrue(ok, note)
        evaluate = self.sent("Runtime.evaluate")
        self.assertEqual([message["params"]["contextId"] for message in evaluate[:2]], [9, 11])
        clicks = [message["params"] for message in self.sent("Input.dispatchMouseEvent")
                  if message["params"]["type"] == "mousePressed"]
        self.assertEqual([(click["x"], click["y"]) for click in clicks], [(910.0, 50.0)])
        self.assertEqual(self.sent("Page.navigate"), [])

    def test_cross_origin_frame_offset_is_measured_via_cdp_before_clicking(self):
        # JS không với tới iframe khác origin (frame_offset null): đo bằng
        # Page.getFrameTree + DOM.getFrameOwner/getBoxModel như đường bấm chip.
        tree = {"frameTree": {"frame": {"id": "root"},
                              "childFrames": [{"frame": {"id": "game"}}]}}
        self.scripted = [[self.context_event(9, "root"),
                          self.context_event(11, "game"),
                          {"id": 1, "result": {}},
                          {"id": 2, "result": {"result": {"value": None}}},
                          {"id": 3, "result": {"result": {"value": {
                              "x": 100.0, "y": 50.0, "frame_offset": None}}}},
                          {"id": 4, "result": {"root": {}}},
                          {"id": 5, "result": tree},
                          {"id": 6, "result": {"backendNodeId": 7}},
                          {"id": 7, "result": {"model": {"content": [10.0, 20.0, 210.0, 20.0,
                                                                       210.0, 420.0, 10.0, 420.0]}}},
                          {"id": 8, "result": {}}, {"id": 9, "result": {}},
                          {"id": 10, "result": {}}, {"id": 11, "result": {}},
                          {"id": 12, "result": {"result": {"value": self.TILES}}}]]
        self.session._cdp_page.side_effect = [self.TABLE, self.LIST]
        ok, note = self.stream.back_to_lobby()
        self.assertTrue(ok, note)
        clicks = [message["params"] for message in self.sent("Input.dispatchMouseEvent")
                  if message["params"]["type"] == "mousePressed"]
        self.assertEqual([(click["x"], click["y"]) for click in clicks], [(110.0, 70.0)])

    def test_no_navigation_when_the_tab_is_already_on_the_list(self):
        self.scripted = [[self.context_event(), {"id": 1, "result": {}}]]
        self.session._cdp_page.return_value = self.LIST
        ok, note = self.stream.back_to_lobby()
        self.assertTrue(ok, note)
        self.assertIn("đã ở danh sách bàn", note.lower())
        self.assertEqual(self.sent("Runtime.evaluate"), [])
        self.assertEqual(self.sent("Page.navigate"), [])

    def test_refuses_a_tab_that_is_not_the_lobby(self):
        self.scripted = [[]]
        self.session._cdp_page.return_value = LobbyEntryTests.PORTAL
        ok, note = self.stream.back_to_lobby()
        self.assertFalse(ok)
        self.assertIn("Không có tab sảnh", note)
        self.assertEqual(self.sockets, [], "Không nối CDP vào tab không phải sảnh")

    def test_reports_when_no_way_back_ever_renders_the_list(self):
        self.scripted = [[self.context_event(),
                          {"id": 1, "result": {}},
                          {"id": 2, "result": {"result": {"value": None}}},
                          {"id": 3, "result": {}},
                          {"id": 4, "result": {"frameId": "root"}},
                          {"id": 5, "result": {"result": {"value": {"tiles": 0, "auth": False}}}}]]
        self.session._cdp_page.side_effect = [self.TABLE, self.TABLE, self.TABLE]
        ok, note = self.stream.back_to_lobby(wait_seconds=0)
        self.assertFalse(ok)
        self.assertIn("chưa thấy lại danh sách bàn", note)
        navigate = self.sent("Page.navigate")
        self.assertEqual([message["params"]["url"] for message in navigate], [self.LIST_URL])
        self.assertFalse(self.stream.session_lost)

    def test_reports_ev7_if_the_returned_page_lost_the_session(self):
        self.scripted = [[self.context_event(),
                          {"id": 1, "result": {}},
                          {"id": 2, "result": {"result": {"value": None}}},
                          {"id": 3, "result": {}},
                          {"id": 4, "result": {"frameId": "root"}},
                          {"id": 5, "result": {"result": {"value": {"auth": True}}}}]]
        self.session._cdp_page.side_effect = [self.TABLE, self.TABLE, self.TABLE]
        ok, note = self.stream.back_to_lobby(wait_seconds=0)
        self.assertFalse(ok)
        self.assertIn("EV.7", note)
        self.assertTrue(self.stream.session_lost)

    def test_hash_without_tiles_falls_through_to_opening_the_lobby_url(self):
        # Đổi hash xong SPA vẫn chưa render danh sách: mở thẳng URL sảnh tổng.
        self.scripted = [[self.context_event(),
                          {"id": 1, "result": {}},
                          {"id": 2, "result": {"result": {"value": None}}},
                          {"id": 3, "result": {}},
                          {"id": 4, "result": {"frameId": "root"}},
                          {"id": 5, "result": {"result": {"value": self.TILES}}}]]
        self.session._cdp_page.side_effect = [self.TABLE, self.TABLE, self.TABLE,
                                              self.TABLE, self.LIST]
        ok, note = self.stream.back_to_lobby(wait_seconds=3)
        self.assertTrue(ok, note)
        self.assertIn("mở lại URL sảnh tổng", note)
        navigate = self.sent("Page.navigate")
        self.assertEqual([message["params"]["url"] for message in navigate], [self.LIST_URL])

    def test_contexts_killed_before_the_hash_step_recover_via_the_lobby_url(self):
        # Context chết ngay từ lệnh đo nút X: đổi hash cũng lỗi, nhưng Page.navigate
        # không cần context — trang tải lại, context mới được dò và thấy ô bàn.
        self.scripted = [[self.context_event(),
                          {"id": 1, "result": {}},
                          {"id": 2, "error": {"code": -32000, "message": "Cannot find context"}},
                          {"id": 3, "error": {"code": -32000, "message": "Cannot find context"}},
                          {"id": 4, "result": {"frameId": "root"}},
                          self.context_event(10),
                          {"id": 5, "result": {}},
                          {"id": 6, "result": {"result": {"value": self.TILES}}}]]
        self.session._cdp_page.side_effect = [self.TABLE, self.LIST]
        ok, note = self.stream.back_to_lobby()
        self.assertTrue(ok, note)
        self.assertEqual(self.stream.contexts, [10])
        self.assertEqual([message["params"]["url"] for message in self.sent("Page.navigate")],
                         [self.LIST_URL])

    def test_contexts_cleared_by_the_close_click_are_probed_again(self):
        self.scripted = [[self.context_event(),
                          {"id": 1, "result": {}},
                          {"id": 2, "result": {"result": {"value": {
                              "x": 1180.0, "y": 42.0,
                              "frame_offset": {"x": 0, "y": 0, "depth": 0}}}}},
                          {"id": 3, "result": {}},
                          {"method": "Runtime.executionContextsCleared"},
                          {"id": 4, "result": {}}, {"id": 5, "result": {}},
                          {"id": 6, "result": {}},
                          self.context_event(10),
                          {"id": 7, "result": {}},
                          {"id": 8, "result": {"result": {"value": self.TILES}}}]]
        self.session._cdp_page.side_effect = [self.TABLE, self.LIST]
        ok, note = self.stream.back_to_lobby()
        self.assertTrue(ok, note)
        self.assertEqual(self.stream.contexts, [10])


class TableCycleTests(unittest.TestCase):
    """Vòng tìm bàn và đường rời bàn: chốt lời thì đóng bàn rồi dừng, không săn bàn mới."""

    def setUp(self):
        self.now = 1_800_000_000.0
        clock = patch.object(app.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def row(self, table_id, letters=BASELINE):
        return dict(key="id:" + table_id, table_id=table_id, label="Baccarat " + table_id,
                    letters=letters, ts=self.now, stable=3, conflict=False, read_error="",
                    game_type="", source="evo-ws-histories", ws_confirmed_letters=letters,
                    screen_stable=0)

    def host(self, tables=None, **changes):
        host = SimpleNamespace(
            tables=tables or {}, sim=app.LiveSimulation(M.MoneyBook(capital=CAPITAL, unit=UNIT,
                                                                    take_profit_pct=100)),
            auto_cycle_var=Mock(get=lambda: True), chrome_busy=False, sweep_busy=False,
            open_table_busy=False, exit_requested=False, _cdp_available=lambda: True,
            stream=Mock(back_to_lobby=Mock(return_value=(True, "Đã rời bàn · danh sách bàn Baccarat."))),
            events=Mock(), refresh=lambda: None, sweep_lobby=Mock(), sweep_note_text="",
            cycle_waiting=False, cycle_attempts=0, cycle_retry_at=0.0, cycle_table_key=None,
            auto_hunt_var=Mock(get=lambda: True), auto_start_letters="", auto_start_table_key=None,
            session_lost=False, _trace=lambda *args, **kwargs: None,
            tables_done=0, tables_var=Mock(get=lambda: "1"),
            entries={"capital": Mock()}, _save_state=lambda: True,
            _auth_blame=lambda: False)
        host._schedule_cycle_retry = MethodType(app.BCRApp._schedule_cycle_retry, host)
        host.leave_table_for_lobby = MethodType(app.BCRApp.leave_table_for_lobby, host)
        host._on_cycle_back = MethodType(app.BCRApp._on_cycle_back, host)
        host._tables_target = MethodType(app.BCRApp._tables_target, host)
        host._session_complete = MethodType(app.BCRApp._session_complete, host)
        host._start_next_table = MethodType(app.BCRApp._start_next_table, host)
        for key, value in changes.items():
            setattr(host, key, value)
        return host

    def started_thread(self):
        captured = []

        def fake_thread(target=None, daemon=False):
            captured.append(target)
            return Mock(start=lambda: target())

        return captured, patch("app.threading.Thread", side_effect=fake_thread)

    def test_leaving_the_table_sends_the_tab_back_to_the_lobby(self):
        host = self.host()
        host.sim.table_key = "id:t1"
        captured, patcher = self.started_thread()
        with patcher:
            app.BCRApp.leave_table_for_lobby(host)
        self.assertTrue(host.cycle_waiting and host.sweep_busy)
        self.assertEqual(host.cycle_table_key, "id:t1")
        self.assertIn("đủ 6 tay", host.sweep_note_text)
        self.assertEqual(host.events.put.call_args[0][0],
                         ("cycle_back", (True, "Đã rời bàn · danh sách bàn Baccarat.")))

    def test_take_profit_exit_closes_the_table_even_when_auto_cycle_is_off(self):
        host = self.host(auto_cycle_var=Mock(get=lambda: False))
        host.sim.table_key = "id:t1"
        host.sim.take_profit_done = True
        captured, patcher = self.started_thread()
        with patcher:
            app.BCRApp.leave_table_for_lobby(host, take_profit=True)
        self.assertTrue(host.sweep_busy)
        self.assertIn("mục tiêu lãi", host.sweep_note_text)
        self.assertEqual(host.events.put.call_args[0][0],
                         ("cycle_back", (True, "Đã rời bàn · danh sách bàn Baccarat.")))

    def test_successful_take_profit_exit_stops_without_another_sweep(self):
        host = self.host(cycle_waiting=True, cycle_table_key="id:t1")
        host.sim.take_profit_done = True
        app.BCRApp._on_cycle_back(host, True, "Đã rời bàn · danh sách bàn Baccarat.")
        host.sweep_lobby.assert_not_called()
        self.assertFalse(host.cycle_waiting)
        self.assertEqual(host.cycle_retry_at, 0.0)
        self.assertIsNone(host.cycle_table_key)
        self.assertIn("đã đóng bàn và dừng phiên", host.sweep_note_text)

    def test_failed_take_profit_exit_retries_the_close_not_a_new_table(self):
        host = self.host(cycle_waiting=True)
        host.sim.take_profit_done = True
        app.BCRApp._on_cycle_back(host, False, "Không về được sảnh (RuntimeError).")
        host.sweep_lobby.assert_not_called()
        self.assertTrue(host.cycle_waiting)
        self.assertEqual(host.cycle_retry_at, self.now + app.CYCLE_RETRY_SECONDS)
        self.assertIn("thử đóng bàn lại", host.sweep_note_text)

    def test_take_profit_retry_reopens_the_close_not_the_hunt(self):
        host = self.host(cycle_waiting=True, cycle_retry_at=self.now - 1)
        host.sim.table_key = "id:t1"
        host.sim.take_profit_done = True
        captured, patcher = self.started_thread()
        with patcher:
            app.BCRApp._resume_cycle_hunt(host, self.now)
        host.sweep_lobby.assert_not_called()
        self.assertTrue(host.sweep_busy)
        self.assertIn("mục tiêu lãi", host.sweep_note_text)

    def test_take_profit_blocks_opening_another_table(self):
        host = self.host(tables={"id:t2": self.row("t2")})
        host.sim.take_profit_done = True
        app.BCRApp.auto_open_table(host, allow_sweep=True)
        self.assertFalse(host.open_table_busy)
        host.stream.open_table.assert_not_called()
        host.sweep_lobby.assert_not_called()

    def test_cycle_stays_off_when_the_user_disabled_it(self):
        host = self.host(auto_cycle_var=Mock(get=lambda: False))
        captured, patcher = self.started_thread()
        with patcher:
            app.BCRApp.leave_table_for_lobby(host)
        self.assertFalse(host.cycle_waiting)
        self.assertEqual(captured, [], "Tắt chu trình thì không điều hướng Chrome")
        self.assertEqual(host.sweep_note_text, "")

    def test_filter_violation_forces_exit_and_a_new_lobby_scan(self):
        host = self.host(auto_cycle_var=Mock(get=lambda: False))
        host.sim.filter_exit_done = True
        captured, patcher = self.started_thread()
        with patcher:
            app.BCRApp.leave_table_for_lobby(host)
        self.assertTrue(host.sweep_busy)
        self.assertEqual(len(captured), 1)
        self.assertIn("điều kiện lọc", host.sweep_note_text)

        app.BCRApp._on_cycle_back(host, True, "Đã về sảnh")
        host.sweep_lobby.assert_called_once_with()

    def test_cycle_hops_the_retry_timer_when_chrome_is_not_attached(self):
        host = self.host(_cdp_available=lambda: False)
        app.BCRApp.leave_table_for_lobby(host)
        self.assertTrue(host.cycle_waiting, "Chưa gắn Chrome vẫn phải giữ vòng tìm bàn")
        self.assertEqual(host.cycle_retry_at, self.now + app.CYCLE_RETRY_SECONDS)
        self.assertIn("Chưa gắn Chrome", host.sweep_note_text)

    def test_the_finished_table_is_not_offered_again(self):
        host = self.host(tables={"id:t1": self.row("t1"), "id:t2": self.row("t2")},
                         cycle_waiting=True, cycle_table_key="id:t1")
        captured, patcher = self.started_thread()
        with patcher:
            app.BCRApp.auto_open_table(host, allow_sweep=True, exclude_key="id:t1")
        self.assertTrue(host.open_table_busy)
        host.stream.open_table.assert_called_once_with("t2")

    def test_search_never_gives_up_hopes_after_the_sweeps(self):
        for attempt in range(1, 40):
            host = self.host(cycle_waiting=True, cycle_attempts=attempt)
            self.assertTrue(app.BCRApp._schedule_cycle_retry(host, "Chưa có bàn đạt 3 điều kiện"))
            self.assertTrue(host.cycle_waiting)
            self.assertEqual(host.cycle_attempts, attempt + 1)
            self.assertEqual(host.cycle_retry_at, self.now + app.CYCLE_RETRY_SECONDS)
            host.sweep_lobby.assert_not_called()

    def test_no_eligible_table_arms_the_next_sweep_instead_of_re_sweeping_now(self):
        host = self.host(cycle_waiting=True, cycle_attempts=1)
        app.BCRApp.auto_open_table(host, allow_sweep=True, exclude_key="id:t1")
        self.assertEqual(host.cycle_attempts, 2)
        self.assertEqual(host.cycle_retry_at, self.now + app.CYCLE_RETRY_SECONDS)
        host.sweep_lobby.assert_not_called()
        self.assertIn("tự quét lại sau %d giây" % app.CYCLE_RETRY_SECONDS, host.sweep_note_text)

    def test_first_scan_without_eligible_table_starts_auto_hunt_countdown(self):
        host = self.host()
        app.BCRApp.auto_open_table(host, allow_sweep=True)
        self.assertTrue(host.cycle_waiting)
        self.assertEqual(host.cycle_attempts, 1)
        self.assertEqual(host.cycle_retry_at, self.now + app.CYCLE_RETRY_SECONDS)
        host.sweep_lobby.assert_not_called()
        self.assertIn("tự quét lại sau %d giây" % app.CYCLE_RETRY_SECONDS, host.sweep_note_text)

    def test_a_single_manual_sweep_button_still_sweeps_once(self):
        host = self.host(auto_hunt_var=Mock(get=lambda: False))
        app.BCRApp.auto_open_table(host, allow_sweep=True)
        self.assertFalse(host.cycle_waiting)
        host.sweep_lobby.assert_called_once_with()
        self.assertEqual(host.cycle_retry_at, 0.0)

    def test_retry_is_not_armed_when_the_user_turned_auto_hunt_off(self):
        host = self.host(cycle_waiting=True, auto_hunt_var=Mock(get=lambda: False))
        app.BCRApp.auto_open_table(host, allow_sweep=True, exclude_key="id:t1")
        self.assertEqual(host.cycle_retry_at, 0.0)
        host.sweep_lobby.assert_not_called()
        self.assertIn("Tự quét tìm bàn", host.sweep_note_text)

    def test_failed_join_rearms_the_hunt_on_the_next_table(self):
        host = self.host(cycle_waiting=True, cycle_table_key="id:t1")
        app.BCRApp._schedule_cycle_retry(host, "Không mở được bàn (RuntimeError).", exclude_key="id:t9")
        self.assertEqual((host.cycle_table_key, host.cycle_retry_at),
                         ("id:t9", self.now + app.CYCLE_RETRY_SECONDS))
        self.assertTrue(host.cycle_waiting)

    def test_back_to_lobby_failure_without_hunt_ends_the_cycle(self):
        host = self.host(cycle_waiting=True, auto_hunt_var=Mock(get=lambda: False))
        note = "Không về được sảnh (RuntimeError)."
        if not app.BCRApp._schedule_cycle_retry(host, note):
            host.cycle_waiting = False
        self.assertFalse(host.cycle_waiting)
        self.assertEqual(host.cycle_retry_at, 0.0)

    def test_a_logged_out_lobby_ends_the_hunt_instead_of_clicking_the_error_page(self):
        host = self.host(cycle_waiting=True, cycle_attempts=3, cycle_table_key="id:t1",
                         cycle_retry_at=self.now + app.CYCLE_RETRY_SECONDS)
        app.BCRApp._session_lost_step(host, "Site báo phiên Evolution đã hết hạn (EV.7).")
        self.assertEqual((host.cycle_waiting, host.cycle_attempts, host.cycle_retry_at,
                          host.cycle_table_key, host.session_lost), (False, 0, 0.0, None, True))
        self.assertFalse(host.sim.running)
        self.assertFalse(app.BCRApp._schedule_cycle_retry(host, "Chưa có bàn đạt 3 điều kiện"))
        app.BCRApp._resume_cycle_hunt(host, self.now + 600)
        host.sweep_lobby.assert_not_called()

    def test_the_timer_starts_the_next_sweep_only_after_the_break(self):
        host = self.host(cycle_waiting=True, cycle_attempts=1,
                         cycle_retry_at=self.now + app.CYCLE_RETRY_SECONDS)
        app.BCRApp._resume_cycle_hunt(host, self.now)
        host.sweep_lobby.assert_not_called()
        self.assertEqual(host.cycle_retry_at, self.now + app.CYCLE_RETRY_SECONDS)
        self.now += app.CYCLE_RETRY_SECONDS
        app.BCRApp._resume_cycle_hunt(host, self.now)
        host.sweep_lobby.assert_called_once_with()
        self.assertEqual(host.cycle_retry_at, 0.0)

    def test_a_busy_app_pushes_the_retry_instead_of_jumping_the_queue(self):
        host = self.host(cycle_waiting=True, sweep_busy=True, cycle_retry_at=self.now - 1)
        app.BCRApp._resume_cycle_hunt(host, self.now)
        host.sweep_lobby.assert_not_called()
        self.assertEqual(host.cycle_retry_at, self.now + app.CYCLE_RETRY_SECONDS)

    def test_the_timer_ignores_a_running_block_or_a_pending_entry(self):
        for running, pending in ((True, None), (False, "id:t2")):
            host = self.host(cycle_waiting=True, cycle_retry_at=self.now - 1)
            host.sim.running = running
            host.auto_start_table_key = pending
            app.BCRApp._resume_cycle_hunt(host, self.now)
            host.sweep_lobby.assert_not_called()
            self.assertEqual(host.cycle_retry_at, 0.0)

    def test_the_stop_hunt_button_ends_the_auto_hunt(self):
        host = self.host(cycle_waiting=True, cycle_attempts=2, cycle_table_key="id:t1",
                         cycle_retry_at=self.now + app.CYCLE_RETRY_SECONDS)
        host.sim.running = True
        app.BCRApp.cancel_auto_hunt(host)
        self.assertEqual((host.cycle_waiting, host.cycle_retry_at, host.cycle_table_key),
                         (False, 0.0, None))
        self.assertIn("Đã dừng tự quét", host.sweep_note_text)
        app.BCRApp._resume_cycle_hunt(host, self.now + 600)
        host.sweep_lobby.assert_not_called()

    def test_a_detached_chrome_keeps_the_hunt_waiting(self):
        host = self.host(cycle_waiting=True, cycle_retry_at=self.now - 1,
                         _cdp_available=lambda: False)
        app.BCRApp._resume_cycle_hunt(host, self.now)
        host.sweep_lobby.assert_not_called()
        self.assertEqual(host.cycle_retry_at, self.now + app.CYCLE_RETRY_SECONDS)
        self.assertTrue(host.cycle_waiting)

    def test_joined_table_disarms_the_hunt(self):
        row = self.row("t9")
        row.update(source="evo-bead", screen_stable=2)
        host = self.host(tables={"id:t9": row}, cycle_waiting=True, cycle_attempts=4,
                         cycle_table_key="id:t1", auto_start_letters=row["letters"],
                         auto_start_table_key="id:t9",
                         auto_start_deadline=self.now + app.ENTRY_WAIT,
                         cycle_retry_at=self.now + app.CYCLE_RETRY_SECONDS)
        host.sim.arm_at_entry = Mock(return_value=True)
        app.BCRApp._auto_start_verified_table(host)
        self.assertEqual((host.cycle_waiting, host.cycle_attempts, host.cycle_retry_at,
                          host.cycle_table_key), (False, 0, 0.0, None))
        app.BCRApp._resume_cycle_hunt(host, self.now + 600)
        self.assertFalse(host.cycle_waiting)


class TableCountCycleTests(unittest.TestCase):
    """Chơi đủ số bàn cấu hình: mỗi bàn chốt lời thì rời bàn, đồng bộ vốn và quét bàn kế;
    xong đủ số bàn thì dừng hẳn. Đây là yêu cầu "cấu hình 3 bàn thì lặp lại 3 lần"."""

    def setUp(self):
        self.now = 1_800_000_000.0
        clock = patch.object(app.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def host(self, target="3", done=0, balance=CAPITAL + 1000, **changes):
        book = M.MoneyBook(capital=CAPITAL, unit=UNIT, take_profit_pct=10)
        book.balance = balance
        book.hands_played = 5
        sim = app.LiveSimulation(book)
        sim.table_key = "id:t1"
        sim.take_profit_done = True
        host = SimpleNamespace(
            tables={}, sim=sim,
            auto_cycle_var=Mock(get=lambda: True), auto_hunt_var=Mock(get=lambda: True),
            chrome_busy=False, sweep_busy=False, open_table_busy=False, exit_requested=False,
            _cdp_available=lambda: True, stream=Mock(), events=Mock(),
            refresh=lambda: None, sweep_lobby=Mock(), sweep_note_text="",
            cycle_waiting=True, cycle_attempts=0, cycle_retry_at=0.0, cycle_table_key="id:t1",
            auto_start_letters="", auto_start_table_key=None, session_lost=False,
            _trace=lambda *a, **k: None, _auth_blame=lambda: False,
            tables_done=done, tables_var=Mock(get=lambda: target),
            entries={"capital": Mock()}, _save_state=lambda: True)
        for name in ("_on_cycle_back", "_tables_target", "_session_complete", "_start_next_table"):
            setattr(host, name, MethodType(getattr(app.BCRApp, name), host))
        for key, value in changes.items():
            setattr(host, key, value)
        return host

    def test_finished_table_under_the_limit_rebases_capital_and_hunts_again(self):
        host = self.host(target="3", done=0, balance=CAPITAL + 1000)
        app.BCRApp._on_cycle_back(host, True, "Đã rời bàn.")
        self.assertEqual(host.tables_done, 1)
        host.sweep_lobby.assert_called_once_with()
        self.assertFalse(host.sim.take_profit_done)
        self.assertTrue(host.cycle_waiting)
        self.assertEqual(host.sim.money.start_capital, CAPITAL + 1000)
        self.assertEqual(host.sim.money.capital, CAPITAL + 1000)
        self.assertEqual(host.sim.money.profit, 0)
        self.assertIsNone(host.sim.money.blocked)
        self.assertEqual(host.sim.money.hands_played, 5)
        host.entries["capital"].set.assert_called_once_with(M.fmt_input_money(CAPITAL + 1000))
        self.assertIn("1/3", host.sweep_note_text)
        self.assertEqual(host.cycle_table_key, "id:t1")

    def test_each_completed_table_rebases_again_on_the_new_balance(self):
        host = self.host(target="3", done=1, balance=CAPITAL + 2000)
        app.BCRApp._on_cycle_back(host, True, "Đã rời bàn.")
        self.assertEqual(host.tables_done, 2)
        self.assertEqual(host.sim.money.start_capital, CAPITAL + 2000)
        host.sweep_lobby.assert_called_once_with()
        self.assertIn("2/3", host.sweep_note_text)

    def test_last_table_stops_without_rebasing_or_sweeping(self):
        host = self.host(target="3", done=2, balance=CAPITAL + 3000)
        app.BCRApp._on_cycle_back(host, True, "Đã rời bàn.")
        self.assertEqual(host.tables_done, 3)
        host.sweep_lobby.assert_not_called()
        self.assertTrue(host.sim.take_profit_done)
        self.assertFalse(host.cycle_waiting)
        self.assertEqual(host.sim.money.start_capital, CAPITAL)
        self.assertIn("dừng phiên", host.sweep_note_text)
        self.assertIn("3/3", host.sweep_note_text)

    def test_default_one_table_stops_after_the_first_profit(self):
        host = self.host(target="1", done=0)
        app.BCRApp._on_cycle_back(host, True, "Đã rời bàn.")
        self.assertEqual(host.tables_done, 1)
        host.sweep_lobby.assert_not_called()
        self.assertTrue(host.sim.take_profit_done)

    def test_failed_exit_does_not_count_the_table(self):
        host = self.host(target="3", done=0)
        app.BCRApp._on_cycle_back(host, False, "Không về được sảnh (RuntimeError).")
        self.assertEqual(host.tables_done, 0)
        host.sweep_lobby.assert_not_called()
        self.assertTrue(host.cycle_waiting)
        self.assertEqual(host.cycle_retry_at, self.now + app.CYCLE_RETRY_SECONDS)

    def test_invalid_target_falls_back_to_one(self):
        host = self.host(target="abc")
        self.assertEqual(app.BCRApp._tables_target(host), 1)
        host.tables_var = Mock(get=lambda: "0")
        self.assertEqual(app.BCRApp._tables_target(host), 1)
        host.tables_var = Mock(get=lambda: "5")
        self.assertEqual(app.BCRApp._tables_target(host), 5)


class CapitalRebaseTests(unittest.TestCase):
    """Đồng bộ vốn sau mỗi bàn: mốc vốn dời theo số dư để % chốt lời bàn sau tính trên tiền mới."""

    def test_rebase_moves_the_baseline_to_the_current_balance(self):
        book = M.MoneyBook(capital=1000, unit=10, take_profit_pct=10)
        book.balance = 1100
        book.hands_played = 6
        book.wins, book.losses = 4, 2
        book.blocked = ("TAKE_PROFIT", "đã chốt lời")
        self.assertEqual(book.rebase_to_balance(), 1100)
        self.assertEqual((book.start_capital, book.capital, book.balance), (1100, 1100, 1100))
        self.assertEqual(book.profit, 0)
        self.assertIsNone(book.blocked)
        self.assertEqual(book.hands_played, 6)
        self.assertEqual((book.wins, book.losses), (4, 2))

    def test_next_table_target_is_percent_of_the_rebased_capital(self):
        book = M.MoneyBook(capital=1000, unit=10, take_profit_pct=10)
        book.balance = 1100
        book.rebase_to_balance()
        book.balance = 1200
        book.check_limits()
        self.assertIsNone(book.blocked, "9,09% vốn mới chưa đủ ngưỡng 10%")
        book.balance = 1210
        book.check_limits()
        self.assertEqual(book.blocked[0], "TAKE_PROFIT", "đúng 10% của 1100 thì chốt lời")


class TableStreakFilterTests(unittest.TestCase):
    """Bộ lọc cầu ngắn: bệt từ 4 tay trở lên bị loại, tay hoà không đếm và không ngắt bệt."""

    def streak_check(self, results):
        checks = ME.check_table(list(results))["checks"]
        return next(c for c in checks if c["name"].startswith("Cầu ngắn"))

    def test_default_limit_is_four_hands(self):
        self.assertEqual(ME.MAX_STREAK_ALLOWED, 3)
        self.assertEqual(ME.has_long_streak(list("BBB")), (None, 0))
        self.assertEqual(ME.has_long_streak(list("BBBB")), ("B", 4))
        self.assertEqual(ME.has_long_streak(list("PPPP")), ("P", 4))

    def test_three_hand_run_is_still_accepted(self):
        for letters in ("BBBPBPBPBPB", "PPPBPBPBPPP"):
            with self.subTest(letters=letters):
                info = ME.check_table(list(letters))
                self.assertTrue(info["eligible"], info["checks"])
                self.assertTrue(self.streak_check(letters)["ok"])

    def test_four_hand_run_is_rejected_for_both_sides(self):
        for letters, side in (("BBBBPBPPBPB", "Banker"), ("PPPPBPBPBPB", "Player")):
            with self.subTest(letters=letters):
                info = ME.check_table(list(letters))
                self.assertFalse(info["eligible"])
                check = self.streak_check(letters)
                self.assertFalse(check["ok"])
                self.assertEqual(check["detail"], "có bệt %s 4 tay" % side)

    def test_tie_bridges_the_same_colour_into_one_streak(self):
        letters = "BBTBBPBPPBPB"          # B-B-(hoà)-B-B = bệt Banker 4 tay
        info = ME.check_table(list(letters))
        self.assertFalse(info["eligible"])
        self.assertEqual(self.streak_check(letters)["detail"], "có bệt Banker 4 tay")
        self.assertEqual(ME.has_long_streak(list("BBTBB")), ("B", 4))

    def test_tie_is_not_counted_as_a_hand_of_the_streak(self):
        letters = "BBTBPBPBPBPB"          # chỉ 3 tay Banker phân thắng bại -> còn nhận
        info = ME.check_table(list(letters))
        self.assertTrue(info["eligible"], info["checks"])
        self.assertEqual(ME.has_long_streak(list(letters)), (None, 0))

    def test_check_label_states_the_three_hand_cap(self):
        check = self.streak_check(BASELINE)
        self.assertEqual(check["name"], "Cầu ngắn, bệt tối đa 3 tay")
        self.assertEqual(check["detail"], "chưa có vệt nào quá 3 tay")

    def test_streak_appearing_midblock_only_warns_not_blocks(self):
        plan = ME.plan(list("BBBPBPBPBPBBBB"), ME.DEFAULT_LADDER["3-3"],
                       hands_in_block=1, started=True, strong="B")
        self.assertEqual(plan["action"], "bet")
        self.assertIn("bệt 4 tay", plan["reason"])

    def test_configured_cap_is_clamped_and_garbage_keeps_the_default(self):
        self.assertEqual(ME.normalize_streak(5), 5)
        self.assertEqual(ME.normalize_streak("7"), 7)
        self.assertEqual(ME.normalize_streak(0), ME.STREAK_LOWER)
        self.assertEqual(ME.normalize_streak(99), ME.STREAK_UPPER)
        self.assertEqual(ME.normalize_streak(None), ME.MAX_STREAK_ALLOWED)
        self.assertEqual(ME.normalize_streak("abc"), ME.MAX_STREAK_ALLOWED)

    def test_raising_the_cap_accepts_a_four_hand_streak(self):
        letters = "BBBBPBPPBPB"
        self.assertFalse(ME.check_table(list(letters))["eligible"])
        info = ME.check_table(list(letters), max_streak=4)
        self.assertTrue(info["eligible"], info["checks"])
        check = next(c for c in info["checks"] if c["name"].startswith("Cầu ngắn"))
        self.assertEqual(check["name"], "Cầu ngắn, bệt tối đa 4 tay")
        self.assertEqual(check["detail"], "chưa có vệt nào quá 4 tay")
        self.assertEqual(ME.has_long_streak(list(letters), 4), (None, 0))
        self.assertEqual(ME.has_long_streak(list(letters), 3), ("B", 4))

    def test_lowering_the_cap_rejects_a_three_hand_streak(self):
        letters = "BBBPBPBPBPB"
        self.assertTrue(ME.check_table(list(letters))["eligible"])
        info = ME.check_table(list(letters), max_streak=2)
        self.assertFalse(info["eligible"])
        check = next(c for c in info["checks"] if c["name"].startswith("Cầu ngắn"))
        self.assertEqual(check["detail"], "có bệt Banker 3 tay")
        self.assertEqual(ME.has_long_streak(list(letters), 2), ("B", 3))

    def test_tie_still_bridges_the_streak_under_a_custom_cap(self):
        letters = "BBTBBPBPPBPB"
        self.assertTrue(ME.check_table(list(letters), max_streak=4)["eligible"])
        self.assertFalse(ME.check_table(list(letters), max_streak=3)["eligible"])
        self.assertEqual(ME.has_long_streak(list(letters), 4), (None, 0))

    def test_plan_honours_the_configured_cap(self):
        letters = list("BBBBPBPPBPB")
        raised = ME.plan(letters, ME.DEFAULT_LADDER["3-3"], max_streak=4)
        self.assertEqual(raised["action"], "start")
        default = ME.plan(letters, ME.DEFAULT_LADDER["3-3"])
        self.assertEqual(default["action"], "wait")
        self.assertEqual(default["reason"], "có bệt Banker 4 tay")

    def test_midblock_caution_uses_the_configured_cap(self):
        letters = list("BBBPBPBPBPBBBB")
        plan = ME.plan(letters, ME.DEFAULT_LADDER["3-3"], hands_in_block=1, started=True,
                       strong="B", max_streak=4)
        self.assertEqual(plan["action"], "bet")
        self.assertNotIn("bệt", plan["reason"])


class RoadFilterConfigTests(unittest.TestCase):
    """Bộ lọc cầu nằm trong Cài đặt: khoảng tay đã đi + số tay bệt tối đa, lưu xuống state."""

    def host(self, low="10", high="50", streak="3"):
        sim = app.LiveSimulation(M.MoneyBook(capital=CAPITAL, unit=UNIT))
        var = lambda value: Mock(get=lambda: value, set=Mock())
        return SimpleNamespace(sim=sim, hands_min_var=var(low), hands_max_var=var(high),
                               streak_var=var(streak), hands_note=Mock(), root=Mock(),
                               _save_state=Mock(), refresh=Mock(), apply_settings=Mock())

    def test_set_road_filter_clamps_both_ranges(self):
        sim = app.LiveSimulation(M.MoneyBook(capital=CAPITAL, unit=UNIT))
        self.assertEqual(sim.set_road_filter(12, 40, 5), (12, 40, 5))
        self.assertEqual(sim.set_road_filter(3, 900, 99), (ME.HANDS_LOWER, ME.HANDS_UPPER,
                                                          ME.STREAK_UPPER))
        self.assertEqual(sim.set_road_filter(40, 12, 0), (12, 40, ME.STREAK_LOWER))
        self.assertEqual((sim.min_hands, sim.max_hands, sim.max_streak), (12, 40, ME.STREAK_LOWER))

    def test_filter_reaches_the_plan_of_the_running_simulation(self):
        host = self.host()
        host.sim.bind(dict(key="id:t1", label="Baccarat t1", letters="BBBBPBPPBPB"))
        self.assertEqual(host.sim.plan()["action"], "wait")
        host.sim.set_road_filter(10, 50, 4)
        self.assertEqual(host.sim.plan()["action"], "start")

    def test_saving_the_filter_writes_back_the_clamped_numbers(self):
        host = self.host(low="3", high="900", streak="99")
        app.BCRApp.apply_road_filter(host)
        host.hands_min_var.set.assert_called_once_with(str(ME.HANDS_LOWER))
        host.hands_max_var.set.assert_called_once_with(str(ME.HANDS_UPPER))
        host.streak_var.set.assert_called_once_with(str(ME.STREAK_UPPER))
        self.assertIn("bệt tối đa %d tay" % ME.STREAK_UPPER, host.hands_note.config.call_args[1]["text"])
        host._save_state.assert_called_once_with()
        host.refresh.assert_called_once_with()

    def test_saving_the_filter_does_not_archive_the_session(self):
        host = self.host(streak="4")
        app.BCRApp.apply_road_filter(host)
        host.apply_settings.assert_not_called()
        self.assertEqual(host.sim.max_streak, 4)

    def test_a_non_numeric_filter_is_refused_without_touching_the_simulation(self):
        host = self.host(streak="ba tay")
        with patch("app.messagebox.showerror") as error:
            app.BCRApp.apply_road_filter(host)
        self.assertTrue(error.called)
        self.assertEqual(host.sim.max_streak, ME.MAX_STREAK_ALLOWED)
        host._save_state.assert_not_called()


class ManualTablePickTests(unittest.TestCase):
    """Chọn bàn trong danh sách là vào đánh luôn; nút Bắt đầu/Tạm dừng đã bỏ khỏi UI."""

    def setUp(self):
        self.now = 1_800_000_000.0
        clock = patch.object(app.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def row(self, key="id:t1", letters=BASELINE):
        return dict(key=key, table_id=key.split(":")[1], label="Baccarat " + key, letters=letters,
                    ts=self.now, stable=3, conflict=False, read_error="", game_type="",
                    source="evo-ws-histories", ws_confirmed_letters=letters, screen_stable=0)

    def host(self, row=None, selection=None):
        rows = {row["key"]: row} if row else {}
        picked = (row["key"],) if row else ()
        return SimpleNamespace(tables=rows,
                               sim=app.LiveSimulation(M.MoneyBook(capital=CAPITAL, unit=UNIT)),
                               live_tree=Mock(selection=Mock(return_value=picked if selection is None
                                                             else selection)),
                               _save_state=Mock(), refresh=Mock())

    def test_picking_an_eligible_table_arms_it_right_away(self):
        host = self.host(self.row())
        app.BCRApp.load_live_selected(host)
        self.assertTrue(host.sim.running)
        self.assertEqual(host.sim.table_key, "id:t1")
        self.assertEqual(host.sim.pending["action"], "start")
        self.assertEqual(host.sim.pending["baseline"], BASELINE)
        host._save_state.assert_called_once_with()
        host.refresh.assert_called_once_with()

    def test_picking_an_ineligible_table_reports_the_reason_and_bets_nothing(self):
        host = self.host(self.row(letters="BBBBPBPPBPB"))
        app.BCRApp.load_live_selected(host)
        self.assertFalse(host.sim.running)
        self.assertIsNone(host.sim.pending)
        self.assertEqual(host.sim.note, "có bệt Banker 4 tay")
        self.assertEqual(host.sim.money.hands_played, 0)

    def test_repicking_the_running_table_keeps_the_block_in_progress(self):
        host = self.host(self.row())
        app.BCRApp.load_live_selected(host)
        host.sim.method_state["hands_in_block"] = 2
        host.sim.method_state["started"] = True
        host.sim.pending = None
        app.BCRApp.load_live_selected(host)
        self.assertTrue(host.sim.running)
        self.assertEqual(host.sim.method_state["hands_in_block"], 2)
        self.assertIsNone(host.sim.pending)
        self.assertEqual(host.sim.note, "Đang theo dõi bàn này.")

    def test_picking_another_table_switches_the_block_to_it(self):
        first, second = self.row("id:t1"), self.row("id:t2", letters=PLAYER_BASELINE)
        host = self.host(first)
        app.BCRApp.load_live_selected(host)
        host.sim.method_state["hands_in_block"] = 2
        host.tables[second["key"]] = second
        host.live_tree.selection.return_value = (second["key"],)
        app.BCRApp.load_live_selected(host)
        self.assertTrue(host.sim.running)
        self.assertEqual(host.sim.table_key, "id:t2")
        self.assertEqual(host.sim.letters, PLAYER_BASELINE)
        self.assertEqual(host.sim.method_state["hands_in_block"], 0)

    def test_no_selection_asks_for_a_row(self):
        host = self.host()
        app.BCRApp.load_live_selected(host)
        self.assertFalse(host.sim.running)
        self.assertIn("Chọn một dòng bàn", host.sim.note)
        host._save_state.assert_called_once_with()

    def test_the_start_pause_widgets_are_gone_from_the_app(self):
        source = inspect.getsource(app.BCRApp)
        for gone in ("start_button", "toggle_simulation", "verify_check", "self.verified"):
            with self.subTest(widget=gone):
                self.assertNotIn(gone, source)


class MethodBlockRegressionTests(unittest.TestCase):
    LADDER = ME.DEFAULT_LADDER["3-3"]

    def test_started_zero_position_recalculates_strong_not_cached_side(self):
        for baseline, old, new in ((BASELINE, "B", "P"), (PLAYER_BASELINE, "P", "B")):
            with self.subTest(old=old, new=new):
                results = list(baseline + new * 6)
                before = results[:]
                self.assertEqual(ME.strong_side(results)[0], new)
                plan = ME.plan(results, self.LADDER, ladder_step=2, hands_in_block=0,
                               started=True, strong=old)
                self.assertEqual(plan["action"], "bet")
                self.assertEqual((plan["strong"], plan["side"], plan["block_pos"]), (new, new, 0))
                self.assertEqual(plan["stake"], self.LADDER[2])
                self.assertEqual(results, before)

    def test_midblock_keeps_original_strong_even_when_current_strong_flips(self):
        for baseline, old, new in ((BASELINE, "B", "P"), (PLAYER_BASELINE, "P", "B")):
            results = list(baseline + new * 6)
            self.assertEqual(ME.strong_side(results)[0], new)
            for position in range(1, 6):
                with self.subTest(old=old, position=position):
                    plan = ME.plan(results, self.LADDER, hands_in_block=position, started=True, strong=old)
                    self.assertEqual(plan["strong"], old)
                    self.assertEqual(plan["side"], old if position < 3 else new)
                    self.assertEqual(plan["block_pos"], position)

    def test_explicit_completed_block_also_recalculates_strong(self):
        plan = ME.plan(list(BASELINE + "PPPPPP"), self.LADDER, hands_in_block=6,
                       started=True, strong="B")
        self.assertEqual((plan["strong"], plan["side"], plan["block_pos"]), ("P", "P", 0))

    def test_ladder_is_taken_exactly_as_entered_for_both_profiles(self):
        """Không nhân cứng: bậc nào là số tiền người dùng gõ, kể cả khi tỉ lệ giữa các bậc tùy ý."""
        for profile, ladder, positions in (("3-3", [40_000, 160_000, 360_000, 740_000, 1_700_000, 3_000_000], 6),
                                           ("3-2", [40_000, 160_000, 360_000, 740_000, 1_700_000], 5)):
            with self.subTest(profile=profile):
                self.assertEqual(ME.block_hands(profile), positions)
                for step in range(positions):
                    plan = ME.plan(list(BASELINE), ladder, ladder_step=step, hands_in_block=1,
                                   started=True, strong="B", profile=profile)
                    self.assertEqual(plan["stake"], ladder[step], "bậc %d" % step)
                    self.assertIn("bậc %d/%d" % (step + 1, positions), plan["reason"])

    def test_three_two_block_bets_three_strong_then_two_opposite(self):
        ladder = ME.DEFAULT_LADDER["3-2"]
        sides = [ME.plan(list(BASELINE), ladder, hands_in_block=position, started=True, strong="B",
                         profile="3-2")["side"] for position in range(5)]
        self.assertEqual(sides, ["B", "B", "B", "P", "P"])
        self.assertEqual(ME.plan(list(BASELINE), ladder, hands_in_block=5, started=True, strong="B",
                                 profile="3-2")["block_pos"], 0)

    def test_advance_wraps_the_block_at_the_profile_length(self):
        state = {"started": True, "ladder_step": 0, "hands_in_block": 4, "strong": "B"}
        self.assertEqual(ME.advance(state, "B", "B", 40_000, "3-2")[0]["hands_in_block"], 0)
        self.assertEqual(ME.advance(state, "B", "B", 40_000, "3-3")[0]["hands_in_block"], 5)

    def test_ladder_must_match_the_number_of_hands_of_the_profile(self):
        with self.assertRaises(ValueError):
            ME.ladder_stakes("3-2", [40_000, 160_000, 360_000, 740_000])
        with self.assertRaises(ValueError):
            ME.ladder_stakes("3-3", [0] * 6)
        self.assertEqual(ME.ladder_stakes("3-2", [40_000, 160_000, 360_000, 740_000, 1_700_000]),
                         [40_000.0, 160_000.0, 360_000.0, 740_000.0, 1_700_000.0])


class BrowserUrlRegressionTests(unittest.TestCase):
    def setUp(self):
        self.host = urlsplit(LOBBY_URL).hostname
        self.origin = "https://" + self.host
        self.mirror = "https://ivi4sibf.kurxcbnpmjvc.link"

    def test_public_https_mirrors_with_default_port_are_allowed(self):
        self.assertTrue(BR.is_lobby_host(self.host))
        for url in (LOBBY_URL, self.origin + BR.LOBBY_PATH + "/",
                    self.origin + ":443/frontend/evo/r3/",
                    self.mirror + BR.LOBBY_PATH + "/#category=all_games&see=baccarat",
                    "https://another-mirror.example.net/frontend/evo/r3/#category=all_games",
                    "https://" + self.host.upper() + BR.LOBBY_PATH + "/", BR.START_URL):
            with self.subTest(url=url):
                self.assertTrue(BR.valid_site_url(url))

    def test_lobby_needs_the_exact_evo_path(self):
        # is_lobby_url vẫn chỉ nhận đúng /frontend/evo/r3 — đó là nơi CÓ bảng cầu.
        for url in (self.origin + "/", self.origin + "/other/path?table_id=01#category=all_games",
                    self.origin + "/frontend/evo/", self.origin + "/frontend/other",
                    self.origin + "/frontend/evo/r3evil", self.mirror + "/frontend/evo",
                    self.mirror + "/"):
            with self.subTest(url=url):
                self.assertFalse(BR.is_lobby_url(url))
                # Nhưng mọi trang HTTPS công khai đều được phép mở (không phụ thuộc trang chủ).
                self.assertTrue(BR.valid_site_url(url), url)
        self.assertTrue(BR.valid_site_url(self.origin + BR.LOBBY_PATH + "/"))
        self.assertTrue(BR.valid_site_url(self.origin + BR.LOBBY_PATH + "/x"))
        # Sảnh nằm trong iframe của trang vào: hai URL đều thuộc danh sách được phép.
        self.assertTrue(BR.is_portal_url(BR.START_URL))
        self.assertTrue(BR.is_lobby_url(LOBBY_URL))
        self.assertFalse(BR.is_lobby_url(BR.START_URL))
        self.assertFalse(BR.is_portal_url(LOBBY_URL))

    def test_entry_is_detected_but_never_treated_as_a_lobby(self):
        entry = self.mirror + "/entry"
        self.assertTrue(BR.is_evo_entry_url(entry))
        self.assertFalse(BR.is_lobby_url(entry))
        self.assertFalse(BR.valid_site_url(entry))

    def test_private_and_lookalike_hosts_are_rejected(self):
        for host in ("localhost", "127.0.0.1", "127.0.0.2", "[::1]", "::1",
                     "notevo.local", "host.local", ".example.net", "example.net.",
                     "double..dot.example.net", "-bad.example.net", "bad-.example.net",
                     "no-dot-host"):
            with self.subTest(host=host):
                self.assertFalse(BR.valid_site_url("https://" + host + BR.LOBBY_PATH + "/"))
        # Không còn gắn với trang chủ nào: mọi tên miền công khai HTTPS đều mở được.
        self.assertTrue(BR.valid_site_url("https://sub." + self.host + BR.LOBBY_PATH + "/"))
        for url in ("https://" + BR.PORTAL_HOST + "/",
                    "https://" + BR.PORTAL_HOST + "/gamelobby",
                    "https://" + BR.PORTAL_HOST + "/other/page",
                    "https://neo179.com" + BR.PORTAL_PATH,
                    "https://api.neo179.com/vb9prod-ecp/api/v1/profile",
                    "https://some-other-casino.example/anything"):
            with self.subTest(url=url):
                self.assertTrue(BR.valid_site_url(url))
                self.assertTrue(BR.is_portal_url(url))
        # Vẫn loại đúng những thứ không phải trang HTTPS công khai.
        for url in ("https://example.invalid/entry", "http://neo179.com/",
                    "https://127.0.0.1/", "https://user:pw@neo179.com/"):
            with self.subTest(url=url):
                self.assertFalse(BR.valid_site_url(url))

    def test_non_https_file_and_relative_urls_are_rejected(self):
        for url in ("http://" + self.host, "http://" + self.host + BR.LOBBY_PATH + "/",
                    "ftp://" + self.host + BR.LOBBY_PATH,
                    "file:///C:/Users/Administrator/Desktop/BCR/test_live_app.py",
                    "file://" + self.host + "/", "data:text/plain,BPT",
                    "javascript:void(0)", "about:blank", "chrome://settings",
                    "//" + self.host, self.host + "/frontend", "/frontend"):
            with self.subTest(url=url):
                self.assertFalse(BR.valid_site_url(url))

    def test_all_userinfo_is_rejected_even_on_approved_host(self):
        for userinfo in ("user@", "user:password@", "@", ":@", "example.invalid@"):
            with self.subTest(userinfo=userinfo):
                self.assertFalse(BR.valid_site_url(
                    "https://" + userinfo + self.host + BR.LOBBY_PATH + "/"))
        self.assertFalse(BR.valid_site_url(self.origin + "@example.invalid/"))

    def test_nondefault_or_malformed_ports_are_rejected(self):
        for port in ("80", "444", "8443", "0", "-1", "65536", "bad"):
            with self.subTest(port=port):
                self.assertFalse(BR.valid_site_url(
                    self.origin + ":" + port + BR.LOBBY_PATH + "/"))
        self.assertTrue(BR.valid_site_url(self.origin + ":443" + BR.LOBBY_PATH + "/"))

    def test_whitespace_control_characters_and_backslashes_are_rejected(self):
        for character in (" ", "\t", "\r", "\n", "\x00", "\x1f", "\x7f", "\u00a0", "\\"):
            for url in (character + self.origin, self.origin + "/" + character,
                        "https://" + character + self.host):
                with self.subTest(url=url):
                    self.assertFalse(BR.valid_site_url(url))

    def test_malformed_and_nonstring_urls_fail_closed(self):
        for url in (None, "", 443, False, [], {}, b"https://example.invalid",
                    "https://[", "https://", "https:///" + self.host):
            with self.subTest(url=url):
                self.assertFalse(BR.valid_site_url(url))

    def test_lobby_detection_never_rewrites_the_launch_url(self):
        session = BR.BrowserSession()
        script = session._reader_script()
        self.assertIn("'/frontend/evo/r3'", script)
        self.assertNotRegex(script, r"location\.hash\s*=")
        # Đọc hash để nhận diện lobby không điều hướng hay đổi URL launch của EVO.
        self.assertFalse(hasattr(BR, "lobby_url_with_route"))
        self.assertFalse(hasattr(BR, "lobby_list_url"))

    def test_start_guard_rejects_urls_before_any_browser_discovery_or_launch(self):
        # BrowserSession construction is pure; every external entry point is mocked.
        with patch.object(BR, "chrome_path") as discover, \
                patch.object(BR, "driver_for") as driver_for, \
                patch.object(BR.BrowserSession, "_start_driver") as launch, \
                patch.object(BR.BrowserSession, "alive") as alive, \
                patch.object(BR.BrowserSession, "stop") as stop:
            for url in ("file:///C:/not-a-page",
                        "http://" + self.host, self.origin + ":8443/",
                        "https://user:password@" + self.host + "/"):
                with self.subTest(url=url):
                    session = BR.BrowserSession()
                    self.assertFalse(session.start(url))
                    self.assertIsNone(session.driver)
                    self.assertEqual(session.connection_state, "error")
                    self.assertEqual(session.url, BR.START_URL)
            for external in (discover, driver_for, launch, alive, stop):
                external.assert_not_called()

    def test_clean_report_scrubs_query_fragment_and_credentials_from_all_url_metadata(self):
        raw_url = "https://user:password@" + self.host + ":443/frontend/evo/r3/?token=QUERY_SECRET#HASH_SECRET"
        report = dict(page=dict(url=raw_url, title="Baccarat", table_id="table_01:2"),
                      tables=[dict(label="Baccarat 01", letters=BASELINE)],
                      diag=dict(kind="road", scanned=15, roads=1, frame=raw_url))
        before = deepcopy(report)
        clean = BR._clean_report(report)
        expected = self.origin + "/frontend/evo/r3/"
        self.assertEqual(clean["page"]["url"], expected)
        self.assertEqual(clean["diag"]["frame"], expected)
        self.assertEqual(clean["page"]["table_id"], "table_01:2")
        self.assertEqual(clean["tables"], report["tables"])
        self.assertEqual(clean["diag"]["scanned"], 15)
        for secret in ("QUERY_SECRET", "HASH_SECRET", "user:password", "?", "#"):
            self.assertNotIn(secret, repr(clean))
        self.assertEqual(report, before)

    def test_clean_report_default_url_drops_its_fragment(self):
        clean = BR._clean_report(dict(page=dict(url=LOBBY_URL), tables=[]))
        parsed = urlsplit(LOBBY_URL)
        expected = self.origin + parsed.path
        self.assertEqual(clean["page"]["url"], expected)
        self.assertEqual(clean["diag"]["frame"], expected)

    def test_clean_report_discards_invalid_url_metadata(self):
        for url in ("file:///C:/private", "data:text/plain,private", "about:blank",
                    "https://[", self.origin + ":bad/", None, 123, []):
            with self.subTest(url=url):
                clean = BR._clean_report(dict(page=dict(url=url), tables=[],
                                              diag=dict(frame="https://example.invalid/?secret#secret")))
                self.assertEqual(clean["page"]["url"], "")
                self.assertEqual(clean["diag"]["frame"], "")

    def test_clean_report_rejects_malformed_report_shapes(self):
        for report in (None, [], "report", {}, {"tables": {}}, {"tables": None},
                       {"tables": [], "page": "invalid"},
                       {"tables": [], "diag": ["invalid"]}):
            with self.subTest(report=report):
                self.assertIsNone(BR._clean_report(report))


class BrowserCdpRegressionTests(unittest.TestCase):
    """Synthetic CDP transcripts only; never contact or modify a real browser."""

    def setUp(self):
        self.session = BR.BrowserSession()
        self.origin = "https://" + LOBBY_HOST
        self.page = dict(id="test-target", type="page", url=LOBBY_URL,
                         webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/test-target")
        self.root = dict(targetId=self.page["id"], type="page", url=self.page["url"])
        self.metadata = {"list": [self.page], "version": {"Browser": "Chrome/123.0"}}
        metadata = patch.object(BR.BrowserSession, "_cdp_json",
                                side_effect=lambda resource: self.metadata[resource])
        self.cdp_json = metadata.start()
        self.addCleanup(metadata.stop)

        self.websocket = Mock(spec=["create_connection"])
        self.websocket.create_connection.side_effect = AssertionError("Unscripted websocket")
        websocket = patch.dict(sys.modules, {"websocket": self.websocket})
        websocket.start()
        self.addCleanup(websocket.stop)
        reader = patch.object(self.session, "_reader_script", return_value="return window.__BCR_SCAN();")
        self.reader_script = reader.start()
        self.addCleanup(reader.stop)

        # Guard the real I/O/launch boundaries even if a regression swallows errors.
        for target, name in ((BR, "urlopen"), (BR, "chrome_path"), (BR, "driver_for"),
                             (BR.subprocess, "Popen"), (BR.subprocess, "run"),
                             (BR.BrowserSession, "_start_driver")):
            guard = patch.object(target, name, side_effect=AssertionError("Forbidden " + name))
            blocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(blocked.assert_not_called)

    def report(self):
        raw_url = ("https://user:password@" + LOBBY_HOST
                   + ":443/frontend/evo/r3/?token=QUERY_SECRET#HASH_SECRET")
        return dict(page=dict(url=raw_url, title="Baccarat " + "x" * 100,
                              table_id="table_01:2", extra="PAGE_SECRET"),
                    tables=[dict(label="Baccarat 01", letters=BASELINE)],
                    diag=dict(kind="road", scanned=15, roads=1, frame=raw_url,
                              extra="DIAG_SECRET"), extra="REPORT_SECRET")

    def context_event(self, context_id, default=True):
        return dict(method="Runtime.executionContextCreated",
                    params=dict(context=dict(id=context_id, auxData=dict(isDefault=default))))

    def transcript(self, reports=None, context_ids=(11,), first_root=None, last_root=None):
        reports = [self.report()] if reports is None else reports
        messages = [self.context_event(context_id) for context_id in context_ids]
        messages.extend([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"targetInfo": self.root if first_root is None else first_root}},
        ])
        for sequence, report in enumerate(reports, start=3):
            messages.append({"id": sequence, "result": {"result": {"value": report}}})
        messages.append({"id": len(reports) + 3,
                         "result": {"targetInfo": self.root if last_root is None else last_root}})
        return messages

    def scripted_socket(self, messages):
        ws = Mock(spec=["send", "recv", "settimeout", "close"])
        ws.recv.side_effect = [json.dumps(message) for message in messages]
        self.websocket.create_connection.reset_mock()
        self.websocket.create_connection.side_effect = None
        self.websocket.create_connection.return_value = ws
        return ws

    def assert_commands(self, ws, methods):
        commands = [json.loads(args.args[0]) for args in ws.send.call_args_list]
        self.assertEqual([command["method"] for command in commands], methods)
        self.assertEqual([command["id"] for command in commands], list(range(1, len(methods) + 1)))
        for command in commands:
            if command["method"] != "Runtime.evaluate":
                self.assertEqual(command["params"], {})
        ws.close.assert_called_once_with()
        self.assertEqual(ws.method_calls[-1], call.close())
        return commands

    def test_connected_tracks_owned_driver_or_cdp_attachment_without_io(self):
        driver = Mock(spec=["get", "quit", "close"])
        for owned, attached in ((None, False), (driver, False), (None, True), (driver, True)):
            with self.subTest(owned=owned is not None, attached=attached):
                self.session.driver = owned
                self.session.cdp_attached = attached
                self.assertEqual(self.session.connected, owned is not None or attached)
        self.assertEqual(driver.mock_calls, [])
        self.cdp_json.assert_not_called()
        self.websocket.create_connection.assert_not_called()

    def test_attach_only_reads_metadata_without_launching_or_navigating(self):
        self.session.url = self.origin + "/unchanged"
        self.session.last_frames = 4
        self.session.last_diag = {"old": True}
        self.session.last_error = "old error"
        self.assertTrue(self.session.attach())
        self.assertTrue(self.session.connected)
        self.assertTrue(self.session.cdp_attached)
        self.assertIsNone(self.session.driver)
        self.assertEqual(self.session.mode, "cdp")
        self.assertEqual(self.session.url, self.origin + "/unchanged")
        self.assertEqual(self.session.connection_state, "waiting")
        self.assertEqual(self.session.driver_info, "Chrome/123.0 · CDP 9222")
        self.assertEqual((self.session.last_frames, self.session.last_diag, self.session.last_error),
                         (0, {}, ""))
        self.assertEqual(self.cdp_json.call_args_list, [call("list"), call("version")])
        self.websocket.create_connection.assert_not_called()
        self.reader_script.assert_not_called()

    def test_attach_refuses_to_mix_cdp_with_owned_webdriver(self):
        driver = Mock(spec=["get", "quit", "close", "execute_cdp_cmd"])
        self.session.driver = driver
        self.session.mode = "driver"
        self.session.connection_state = "reading"
        self.assertFalse(self.session.attach())
        self.assertTrue(self.session.connected)
        self.assertFalse(self.session.cdp_attached)
        self.assertIs(self.session.driver, driver)
        self.assertEqual(self.session.mode, "driver")
        self.assertEqual(self.session.connection_state, "reading")
        self.assertIn("WebDriver", self.session.note)
        self.assertEqual(driver.mock_calls, [])
        self.cdp_json.assert_not_called()
        self.websocket.create_connection.assert_not_called()

    def test_start_does_not_replace_live_or_paused_cdp_with_webdriver(self):
        self.assertTrue(self.session.attach())
        for pages, expected in (([self.page], True), ([], False)):
            with self.subTest(approved_page=bool(pages)):
                self.metadata["list"] = pages
                self.cdp_json.reset_mock()
                with patch.object(self.session, "stop") as stop:
                    self.assertEqual(self.session.start(self.origin + "/do-not-navigate"), expected)
                    stop.assert_not_called()
                self.assertTrue(self.session.connected)
                self.assertTrue(self.session.cdp_attached)
                self.assertIsNone(self.session.driver)
                self.assertEqual(self.session.mode, "cdp")
                self.assertEqual(self.session.url, BR.START_URL)
                self.cdp_json.assert_called_once_with("list")
        self.websocket.create_connection.assert_not_called()

    def test_stop_only_detaches_cdp_and_clears_cached_state(self):
        self.assertTrue(self.session.attach())
        self.session.last_frames = 3
        self.session.last_diag = {"kind": "road"}
        self.session.last_error = "stale error"
        self.session.connection_state = "reading"
        self.session._selected_handle = "stale-handle"
        self.cdp_json.reset_mock()
        note = self.session.stop()
        self.assertEqual(note, self.session.note)
        self.assertIn("CDP", note)
        self.assertFalse(self.session.connected)
        self.assertFalse(self.session.cdp_attached)
        self.assertIsNone(self.session.driver)
        self.assertIsNone(self.session._selected_handle)
        self.assertEqual(self.session.mode, "")
        self.assertEqual(self.session.driver_info, "Chưa kết nối Chrome.")
        self.assertEqual(self.session.connection_state, "closed")
        self.assertEqual((self.session.last_frames, self.session.last_diag, self.session.last_error),
                         (0, {}, ""))
        self.session.stop()  # Detaching twice must not try to close the user's browser.
        self.cdp_json.assert_not_called()
        self.websocket.create_connection.assert_not_called()

    def test_attach_requires_a_public_https_page_not_an_iframe_or_worker(self):
        for pages in ([], [None, "invalid", {}, dict(self.page, type="iframe"),
                           dict(self.page, type="service_worker"),
                           dict(self.page, url="http://" + LOBBY_HOST)]):
            with self.subTest(pages=pages):
                self.metadata["list"] = pages
                self.cdp_json.reset_mock()
                self.session.last_frames = 4
                self.session.last_error = "old error"
                self.assertFalse(self.session.attach())
                self.assertFalse(self.session.connected)
                self.assertFalse(self.session.cdp_attached)
                self.assertEqual(self.session.mode, "")
                self.assertEqual(self.session.connection_state, "waiting")
                self.assertEqual(self.session.last_diag, {"reason": "no_approved_tab"})
                self.assertEqual((self.session.last_frames, self.session.last_error), (0, ""))
                self.cdp_json.assert_called_once_with("list")
        self.websocket.create_connection.assert_not_called()

    def test_attach_reports_entry_handoff_as_an_auth_problem(self):
        self.metadata["list"] = [dict(self.page, url="https://mirror.example.net/entry")]
        self.assertFalse(self.session.attach())
        self.assertEqual(self.session.last_diag, {"reason": "evo_entry_auth_error"})
        self.assertIn("/entry", self.session.note)
        self.assertEqual(self.cdp_json.call_args_list, [call("list")])
        self.websocket.create_connection.assert_not_called()

    def test_attach_skips_unapproved_targets_and_uses_first_approved_page(self):
        self.metadata["list"] = [dict(self.page, url="https://example.invalid/"),
                                 dict(self.page, type="iframe"), self.page]
        self.assertEqual(self.session._cdp_page(), self.page)
        self.assertTrue(self.session.attach())
        self.assertTrue(self.session.connected)
        self.websocket.create_connection.assert_not_called()

    def test_lobby_tab_is_preferred_over_the_entry_page(self):
        portal = dict(self.page, id="portal", url=BR.START_URL,
                      webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/portal")
        table = dict(self.page, id="table", url=self.origin + BR.LOBBY_PATH
                     + "/#category=all_games&table_id=t01")
        self.metadata["list"] = [portal, self.page, table]
        self.assertEqual(self.session._cdp_page(), self.page)
        self.metadata["list"] = [portal, table]
        self.assertEqual(self.session._cdp_page(), table)
        self.metadata["list"] = [portal]
        self.assertEqual(self.session._cdp_page(), portal)
        self.metadata["list"] = [dict(portal, url="http://example.invalid/")]
        self.assertIsNone(self.session._cdp_page())

    def test_optional_version_metadata_failure_does_not_prevent_attach(self):
        for version in (OSError("METADATA_SECRET"), None, [], {}, {"Browser": None},
                        {"Browser": 123}, {"Browser": ""}):
            with self.subTest(version=version):
                self.cdp_json.reset_mock()
                self.cdp_json.side_effect = [[self.page], version]
                self.assertTrue(self.session.attach())
                self.assertTrue(self.session.connected)
                self.assertEqual(self.session.mode, "cdp")
                self.assertEqual(self.session.driver_info, "Chrome hiện có · CDP 9222")
                self.assertEqual(self.session.connection_state, "waiting")
                self.assertEqual(self.session.last_error, "")
                self.assertNotIn("METADATA_SECRET", self.session.note)
                self.assertEqual(self.cdp_json.call_args_list, [call("list"), call("version")])
        self.websocket.create_connection.assert_not_called()

    def test_attach_caps_version_metadata_length(self):
        self.metadata["version"] = {"Browser": "Chrome/" + "x" * 100}
        self.assertTrue(self.session.attach())
        self.assertEqual(self.session.driver_info, self.metadata["version"]["Browser"][:80] + " · CDP 9222")

    def test_attach_page_metadata_failure_fails_closed_and_hides_raw_error(self):
        self.cdp_json.side_effect = OSError("https://example.invalid/?METADATA_SECRET")
        self.assertFalse(self.session.attach())
        self.assertFalse(self.session.connected)
        self.assertIsNone(self.session.driver)
        self.assertEqual(self.session.mode, "")
        self.assertEqual(self.session.connection_state, "error")
        self.assertEqual(self.session.last_error, self.session.note)
        self.assertIn("OSError", self.session.last_error)
        self.assertNotIn("METADATA_SECRET", self.session.last_error)
        self.cdp_json.assert_called_once_with("list")
        self.websocket.create_connection.assert_not_called()

    def test_a_chrome_without_the_debug_port_is_named_as_the_wrong_window(self):
        # Chrome mở tay (không có --remote-debugging-port) làm /json/list ném
        # ConnectionRefusedError; báo chung chung "URLError" khiến người dùng đi tìm lỗi
        # đăng nhập, trong khi app chưa cắm được vào cửa sổ nào và tài khoản đang có
        # hai phiên cùng lúc — nguyên nhân site đá "Bạn đã đăng xuất".
        self.cdp_json.side_effect = URLError(ConnectionRefusedError(10061, "secret detail"))
        self.assertFalse(self.session.attach())
        self.assertEqual(self.session.connection_state, "error")
        self.assertIn("Mở Chrome", self.session.note)
        self.assertIn("Bạn đã đăng xuất", self.session.note)
        self.assertNotIn("secret detail", self.session.note)

    def test_alive_missing_page_pauses_but_keeps_attachment(self):
        self.assertTrue(self.session.attach())
        self.metadata["list"] = []
        self.session.last_frames = 5
        self.session.last_error = "old error"
        self.assertFalse(self.session.alive())
        self.assertTrue(self.session.connected)
        self.assertEqual(self.session.mode, "cdp")
        self.assertEqual(self.session.connection_state, "waiting")
        self.assertEqual(self.session.last_diag, {"reason": "no_approved_tab"})
        self.assertEqual((self.session.last_frames, self.session.last_error), (0, ""))
        self.metadata["list"] = [self.page]
        self.assertTrue(self.session.alive())
        self.websocket.create_connection.assert_not_called()

    def test_alive_metadata_failure_is_sanitized_and_can_recover(self):
        self.assertTrue(self.session.attach())
        self.cdp_json.side_effect = [OSError("METADATA_SECRET"), [self.page]]
        self.assertFalse(self.session.alive())
        self.assertTrue(self.session.connected)
        self.assertEqual(self.session.connection_state, "error")
        self.assertEqual((self.session.last_frames, self.session.last_diag), (0, {}))
        self.assertIn("OSError", self.session.last_error)
        self.assertNotIn("METADATA_SECRET", self.session.last_error)
        self.assertTrue(self.session.alive())
        self.assertEqual(self.session.connection_state, "waiting")
        self.assertEqual(self.session.last_error, "")
        self.websocket.create_connection.assert_not_called()

    def test_reader_src_caches_until_nanosecond_mtime_changes(self):
        source = mock_open()
        source.return_value.read.side_effect = ["reader version 1", "reader version 2", ""]
        mtimes = [1_000_000_001, 1_000_000_001, 1_000_000_002, 1_000_000_002,
                  1_000_000_003, 1_000_000_003]
        with patch.object(BR.os, "stat", side_effect=[SimpleNamespace(st_mtime_ns=n) for n in mtimes]) as stat, \
                patch("builtins.open", source):
            for expected in ("reader version 1", "reader version 1", "reader version 2",
                             "reader version 2", "", ""):
                self.assertEqual(self.session.reader_src(), expected)
        self.assertEqual(stat.call_args_list, [call(BR.READER_JS)] * len(mtimes))
        self.assertEqual(source.call_args_list, [call(BR.READER_JS, "r", encoding="utf-8")] * 3)
        self.assertEqual(source.return_value.read.call_count, 3)
        self.assertEqual(source.return_value.__exit__.call_count, 3)
        self.assertEqual(self.session._reader_mtime_ns, mtimes[-1])

    def test_scan_without_approved_page_waits_without_opening_websocket(self):
        self.assertTrue(self.session.attach())
        self.metadata["list"] = []
        self.session.last_frames = 2
        self.session.last_diag = {"kind": "old"}
        self.session.last_error = "old error"
        self.assertEqual(self.session._scan_cdp(), [])
        self.assertTrue(self.session.connected)
        self.assertEqual(self.session.connection_state, "waiting")
        self.assertEqual(self.session.last_diag, {"reason": "no_approved_tab"})
        self.assertEqual((self.session.last_frames, self.session.last_error), (0, ""))
        self.websocket.create_connection.assert_not_called()
        self.reader_script.assert_not_called()

    def test_scan_page_metadata_failure_never_opens_websocket(self):
        self.assertTrue(self.session.attach())
        self.cdp_json.side_effect = OSError("METADATA_SECRET")
        self.assertEqual(self.session._scan_cdp(), [])
        self.assertTrue(self.session.connected)
        self.assertEqual(self.session.connection_state, "error")
        self.assertEqual((self.session.last_frames, self.session.last_diag), (0, {}))
        self.assertIn("OSError", self.session.last_error)
        self.assertNotIn("METADATA_SECRET", self.session.last_error)
        self.websocket.create_connection.assert_not_called()

    def test_scan_rejects_nonloopback_and_malformed_websocket_endpoints(self):
        endpoints = (
            "ws://example.invalid:9222/devtools/page/test-target",
            "ws://192.0.2.1:9222/devtools/page/test-target",
            "ws://127.0.0.1.example.invalid:9222/devtools/page/test-target",
            "ws://localhost.example.invalid:9222/devtools/page/test-target",
            "ws://[2001:db8::1]:9222/devtools/page/test-target",
            "wss://127.0.0.1:9222/devtools/page/test-target",
            "http://127.0.0.1:9222/devtools/page/test-target",
            "ws://127.0.0.1:9223/devtools/page/test-target",
            "ws://127.0.0.1/devtools/page/test-target",
            "ws://127.0.0.1:bad/devtools/page/test-target",
            "ws://127.0.0.1:65536/devtools/page/test-target",
            "ws://user:password@127.0.0.1:9222/devtools/page/test-target",
            "ws://@127.0.0.1:9222/devtools/page/test-target",
            self.page["webSocketDebuggerUrl"] + "#fragment",
            self.page["webSocketDebuggerUrl"] + "\\path",
            "\n" + self.page["webSocketDebuggerUrl"],
            self.page["webSocketDebuggerUrl"] + "\x00",
            "ws://[", "", None, 9222, [], {},
        )
        self.assertTrue(self.session.attach())
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.metadata["list"] = [dict(self.page, webSocketDebuggerUrl=endpoint)]
                self.assertEqual(self.session._scan_cdp(), [])
                self.assertEqual(self.session.connection_state, "error")
                self.assertEqual((self.session.last_frames, self.session.last_diag), (0, {}))
                self.assertIn("ValueError", self.session.last_error)
                self.assertTrue(self.session.connected)
                self.websocket.create_connection.assert_not_called()
                self.reader_script.assert_not_called()

    def test_scan_valid_loopback_responses_sanitize_reports_and_close_only_socket(self):
        self.assertTrue(self.session.attach())
        raw = self.report()
        before = deepcopy(raw)
        empty = dict(page=dict(url=self.origin), tables=[], diag=dict(kind="empty", scanned=1))
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                endpoint = "ws://%s:9222/devtools/page/test-target" % host
                self.metadata["list"] = [dict(self.page, webSocketDebuggerUrl=endpoint)]
                messages = self.transcript([empty, raw], context_ids=(11, 12))
                # Isolated worlds and repeated context announcements are not scanned.
                messages[0:0] = [self.context_event(99, default=False), self.context_event(11)]
                ws = self.scripted_socket(messages)
                reports = self.session.scan()  # Exercise public CDP dispatch as well.
                safe_url = self.origin + "/frontend/evo/r3/"
                diag = dict(kind="road", scanned=15, roads=1, frame=safe_url, auth_required=False)
                self.assertEqual(reports, [dict(page=dict(url=safe_url, title=raw["page"]["title"][:80],
                                                         table_id="table_01:2", wallet_balance=""),
                                               tables=raw["tables"], diag=diag)])
                self.assertEqual(raw, before)
                for secret in ("QUERY_SECRET", "HASH_SECRET", "user:password", "PAGE_SECRET",
                               "DIAG_SECRET", "REPORT_SECRET"):
                    self.assertNotIn(secret, repr(reports))
                self.assertEqual(self.session.last_diag, dict(diag, frames_limited=False))
                self.assertEqual(self.session.last_frames, 2)
                self.assertEqual(self.session.last_error, "")
                self.assertEqual(self.session.connection_state, "reading")
                self.assertTrue(self.session.connected)
                self.assertTrue(self.session.cdp_attached)
                self.assertIsNone(self.session.driver)
                self.assertEqual(self.session.mode, "cdp")
                self.websocket.create_connection.assert_called_once_with(
                    endpoint, suppress_origin=True, timeout=BR.SCRIPT_TIMEOUT, redirect_limit=0,
                    http_no_proxy=["127.0.0.1", "localhost", "::1"])
                commands = self.assert_commands(ws, ["Runtime.enable", "Target.getTargetInfo",
                                                     "Runtime.evaluate", "Runtime.evaluate",
                                                     "Target.getTargetInfo"])
                for command, context_id in zip(commands[2:4], (11, 12)):
                    self.assertEqual(command["params"], dict(
                        expression="(function(){\nreturn window.__BCR_SCAN();\n})()",
                        contextId=context_id, returnByValue=True, silent=True,
                        timeout=BR.SCRIPT_TIMEOUT * 1000))
                self.assertEqual(ws.recv.call_count, len(messages))
                for timeout in ws.settimeout.call_args_list:
                    self.assertGreater(timeout.args[0], 0)
                    self.assertLessEqual(timeout.args[0], BR.SCRIPT_TIMEOUT)

    def test_scan_rejects_changed_or_unapproved_root_before_reading(self):
        # Chỉ loại trang KHÔNG phải HTTPS công khai; URL HTTPS bất kỳ giờ đều hợp lệ.
        for changes in ({"url": "http://example.invalid/"}, {"type": "iframe"},
                        {"targetId": "different-target"}):
            with self.subTest(changes=changes):
                messages = self.transcript(first_root=dict(self.root, **changes))[:3]
                ws = self.scripted_socket(messages)
                self.assertEqual(self.session._scan_cdp(), [])
                self.assertEqual(self.session.connection_state, "waiting")
                self.assertEqual(self.session.last_diag, {"reason": "root_left_site"})
                self.assertEqual(self.session.last_frames, 0)
                self.reader_script.assert_not_called()
                self.assert_commands(ws, ["Runtime.enable", "Target.getTargetInfo"])

    def test_scan_without_default_context_waits_and_closes_socket(self):
        messages = [self.context_event(99, default=False)] + self.transcript([], context_ids=())[:2]
        ws = self.scripted_socket(messages)
        self.assertEqual(self.session._scan_cdp(), [])
        self.assertEqual(self.session.connection_state, "waiting")
        self.assertEqual(self.session.last_diag, {"reason": "no_default_context"})
        self.assertEqual(self.session.last_frames, 0)
        self.reader_script.assert_not_called()
        self.assert_commands(ws, ["Runtime.enable", "Target.getTargetInfo"])

    def test_scan_discards_partial_reports_when_final_root_leaves_https_or_changes_target(self):
        for changes in ({"url": "http://example.invalid/?PRIVATE"}, {"type": "iframe"},
                        {"targetId": "different-target"}):
            with self.subTest(changes=changes):
                ws = self.scripted_socket(self.transcript(last_root=dict(self.root, **changes)))
                reports = self.session._scan_cdp()
                self.assert_commands(ws, ["Runtime.enable", "Target.getTargetInfo",
                                          "Runtime.evaluate", "Target.getTargetInfo"])
                self.assertEqual(reports, [])
                self.assertEqual(self.session.last_frames, 1)
                self.assertEqual(self.session.connection_state, "waiting")
                self.assertEqual(self.session.last_diag, {"reason": "root_left_site"})
                self.assertEqual(self.session.last_error, "")

    def test_scan_discards_partial_reports_when_root_url_changes_within_approved_site(self):
        changed = dict(self.root, url=self.origin + BR.LOBBY_PATH + "/#category=all_games&table_id=table_02")
        self.assertTrue(BR.valid_site_url(changed["url"]))
        ws = self.scripted_socket(self.transcript(last_root=changed))
        reports = self.session._scan_cdp()
        self.assert_commands(ws, ["Runtime.enable", "Target.getTargetInfo",
                                  "Runtime.evaluate", "Target.getTargetInfo"])
        self.assertEqual(self.session.last_frames, 1)
        self.assertEqual(reports, [], "A root navigation must not publish a report from the old page")
        self.assertEqual(self.session.connection_state, "waiting")

    def test_scan_context_destruction_discards_partial_reports_even_during_final_check(self):
        for method in ("Runtime.executionContextDestroyed", "Runtime.executionContextsCleared"):
            for during_final_check in (False, True):
                with self.subTest(method=method, during_final_check=during_final_check):
                    context_ids = (11,) if during_final_check else (11, 12)
                    reports = [self.report()] if during_final_check else [self.report(), self.report()]
                    messages = self.transcript(reports, context_ids=context_ids)
                    # Replace the next reply only after the first report has been received.
                    interrupt_at = len(context_ids) + 3
                    messages[interrupt_at:] = [dict(method=method, params=dict(executionContextId=11))]
                    ws = self.scripted_socket(messages)
                    result = self.session._scan_cdp()
                    last_command = "Target.getTargetInfo" if during_final_check else "Runtime.evaluate"
                    self.assert_commands(ws, ["Runtime.enable", "Target.getTargetInfo",
                                              "Runtime.evaluate", last_command])
                    self.assertEqual(result, [])
                    self.assertEqual(self.session.last_frames, len(context_ids))
                    self.assertEqual(self.session.connection_state, "waiting")
                    self.assertEqual(self.session.last_diag, {"reason": "frame_context_changed"})
                    self.assertTrue(self.session.last_error)
                    self.assertEqual(ws.recv.call_count, len(messages))

    def test_scan_context_or_navigation_command_errors_discard_partial_reports(self):
        for detail in ("Cannot find context with specified id", "Target navigated", "Target closed"):
            with self.subTest(detail=detail):
                messages = self.transcript([self.report(), self.report()], context_ids=(11, 12))
                messages[-2:] = [{"id": 4, "error": {"message": detail + " PRIVATE_DETAIL"}}]
                ws = self.scripted_socket(messages)
                self.assertEqual(self.session._scan_cdp(), [])
                self.assertEqual(self.session.last_frames, 2)
                self.assertEqual(self.session.connection_state, "waiting")
                self.assertEqual(self.session.last_diag, {"reason": "frame_context_changed"})
                self.assertNotIn("PRIVATE_DETAIL", self.session.last_error)
                self.assert_commands(ws, ["Runtime.enable", "Target.getTargetInfo",
                                          "Runtime.evaluate", "Runtime.evaluate"])

    def test_scan_socket_failure_discards_partial_reports_and_closes_socket(self):
        ws = self.scripted_socket(self.transcript()[:-1])
        ws.recv.side_effect = [json.dumps(message) for message in self.transcript()[:-1]] + [
            OSError("SOCKET_SECRET")]
        self.assertEqual(self.session._scan_cdp(), [])
        self.assertEqual(self.session.connection_state, "error")
        self.assertEqual(self.session.last_diag, {})
        self.assertIn("OSError", self.session.last_error)
        self.assertNotIn("SOCKET_SECRET", self.session.last_error)
        self.assert_commands(ws, ["Runtime.enable", "Target.getTargetInfo",
                                  "Runtime.evaluate", "Target.getTargetInfo"])


class ChromeLaunchFailureNoteTests(unittest.TestCase):
    """chromedriver báo lỗi thô (SessionNotCreatedError, message dài) — người dùng chỉ
    cần biết một việc: có cửa sổ Chrome khác đang giữ profile của app."""

    def test_a_locked_profile_is_named_as_another_chrome_window(self):
        for message in ("session not created: Chrome instance exited. Ex: ChromeDriver "
                        "failed to parse response with success: 0",
                        "user data directory is already in use, exiting",
                        "DevToolsActivePort file doesn't exist"):
            with self.subTest(message=message):
                self.assertIn("cửa sổ Chrome khác", BR.chrome_launch_failure_note(Exception(message)))

    def test_other_launch_failures_keep_the_short_generic_note(self):
        note = BR.chrome_launch_failure_note(FileNotFoundError("chrome.exe"))
        self.assertIn("FileNotFoundError", note)
        self.assertNotIn("chrome.exe", note, "đường dẫn thô không được lên UI")

    def test_start_surfaces_the_locked_profile_note_and_launches_nothing_else(self):
        session = BR.BrowserSession()
        error = Exception("session not created: Chrome instance exited")
        with patch.object(BR, "chrome_path", return_value="chrome.exe"), \
             patch.object(BR, "driver_for", return_value=("local", "ok")), \
             patch.object(BR.BrowserSession, "_start_driver", side_effect=error) as launch:
            self.assertFalse(session.start())
        self.assertEqual(launch.call_count, 1)
        self.assertIsNone(session.driver)
        self.assertEqual(session.mode, "")
        self.assertEqual(session.connection_state, "error")
        self.assertIn("cửa sổ Chrome khác", session.note)
        self.assertEqual(session.last_error, session.note)

    def test_wait_for_cdp_answers_as_soon_as_the_port_replies(self):
        session = BR.BrowserSession()
        with patch.object(BR.BrowserSession, "_cdp_json", return_value={"Browser": "Chrome/155"}) as probe:
            self.assertTrue(session.wait_for_cdp(seconds=6))
        self.assertEqual(probe.call_args_list, [call("version")])

    def test_wait_for_cdp_gives_up_without_sleeping_when_the_port_never_opens(self):
        session = BR.BrowserSession()
        sleeper = patch("browser.time.sleep", side_effect=AssertionError("khong duoc nghi"))
        sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        with patch.object(BR.BrowserSession, "_cdp_json", side_effect=ConnectionRefusedError):
            self.assertFalse(session.wait_for_cdp(seconds=0))
        sleep.assert_not_called()


class ChipConfirmationTests(unittest.TestCase):
    """[data-role="selected-chip"] đổi MUỘN HƠN cú bấm một nhịp (càng muộn khi dealer
    đang mở bài), nên một lần đọc ngay sau click_delay thường còn nguyên mệnh giá của
    chip trước — đó là nguồn báo nhầm "Bấm chọn chip 100 nhưng khay đang chọn 20" và
    dừng một lượt đặt đúng tiền."""

    def run_confirm(self, readings, denom=100, wait=None, poll=0.2):
        """Đồng hồ/nghỉ giả: mỗi lần đọc tốn đúng một nhịp, không chờ thật trong test."""
        if wait is None:
            wait = TB.CHIP_CONFIRM_TIMEOUT
        seen = list(readings)
        slept = []
        now = [0.0]

        def read():
            value = seen.pop(0) if seen else readings[-1]
            now[0] += poll
            return value

        def nap(seconds):
            slept.append(seconds)
            now[0] += seconds

        ok, value = TB.confirm_selected_chip(denom, read, wait=wait, poll=poll,
                                             clock=lambda: now[0], nap=nap)
        return ok, value, len(slept)

    def test_a_matching_first_read_does_not_wait_at_all(self):
        ok, value, waits = self.run_confirm([100])
        self.assertTrue(ok)
        self.assertEqual(value, 100)
        self.assertEqual(waits, 0)

    def test_a_stale_read_is_re_read_instead_of_stopping_the_bet(self):
        for readings in ([20, 100], [20, 40, 100], [None, 20, 100]):
            with self.subTest(readings=readings):
                ok, value, waits = self.run_confirm(readings)
                self.assertTrue(ok, readings)
                self.assertEqual(value, 100)
                self.assertGreaterEqual(waits, 1)

    def test_a_value_that_stays_wrong_stops_after_the_window(self):
        ok, value, waits = self.run_confirm([20])
        self.assertFalse(ok)
        self.assertEqual(value, 20)
        self.assertGreaterEqual(waits, 3, "phải đọc lại nhiều lần trong cửa sổ, không kết luận một nhịp")

    def test_an_unreadable_tray_never_blocks_the_bet(self):
        ok, value, _waits = self.run_confirm([None])
        self.assertTrue(ok)
        self.assertIsNone(value)

    def test_the_window_length_is_what_buys_the_retries(self):
        ok, value, waits = self.run_confirm([20], wait=0)
        self.assertEqual((ok, value, waits), (False, 20, 0))

    def test_the_shipped_window_is_wide_enough_for_a_dealer_render(self):
        self.assertGreaterEqual(TB.CHIP_CONFIRM_TIMEOUT, 2.0)
        self.assertGreaterEqual(TB.CHIP_CONFIRM_TIMEOUT / TB.CHIP_CONFIRM_POLL, 5)

    def test_place_bet_uses_the_windowed_read(self):
        self.assertIn("confirm_selected_chip(", inspect.getsource(TB.place_bet))


class WindowConfigTests(unittest.TestCase):
    """Cấu hình cửa sổ Chrome do người dùng nhập; transcript CDP giả lập, không mở Chrome thật."""

    def test_defaults_to_a_normal_window_at_80_percent(self):
        window = BR.normalize_window()
        self.assertEqual((window["fullscreen"], window["percent"]), (False, 80))

    def test_clamps_user_values_and_accepts_comma_decimal(self):
        flags = BR.build_flags(None, False, BR.normalize_window(
            ("1280,5", "720,5"), "150", False))
        self.assertIn("--window-size=1280,720", flags)
        self.assertIn("--force-device-scale-factor=1.5", flags)
        self.assertNotIn("--start-fullscreen", flags)
        oversized = BR.normalize_window(("10", "10", ), "250", False)
        self.assertEqual(oversized["percent"], BR.SCALE_MAX)
        tiny = BR.normalize_window(("10", "10"), "1", False)
        self.assertEqual((tiny["width"], tiny["height"], tiny["percent"]),
                         (BR.SIZE_MIN[0], BR.SIZE_MIN[1], BR.SCALE_MIN))

    def test_keepalive_flags_are_always_passed(self):
        """Tab trang vào phải giữ nhịp JS đầy đủ, nếu không phiên EVO hết hạn giữa chừng."""
        flags = BR.build_flags(None, False, BR.normalize_window())
        for flag in BR.KEEPALIVE_FLAGS:
            self.assertIn(flag, flags)

    def test_garbage_input_falls_back_to_the_default_geometry(self):
        window = BR.normalize_window(("abc", ""), "d", False)
        self.assertEqual((window["width"], window["height"], window["percent"]),
                         (BR.WIN_W, BR.WIN_H, 80))

    def test_fullscreen_uses_window_state_and_never_a_position_flag(self):
        window = BR.normalize_window((3000, 3000), 150, True)
        self.assertIn("--start-fullscreen", BR.build_flags((100, 20), False, window))
        self.assertNotIn("--window-position=100,20", BR.build_flags((100, 20), False, window))
        self.assertIn("--window-position=100,20",
                      BR.build_flags((100, 20), False, BR.normalize_window(fullscreen=False)))

    class Socket:
        def __init__(self, replies):
            self.replies = replies
            self.sent = []

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self):
            return json.dumps(self.replies[self.sent[-1]["id"]])

        def settimeout(self, _seconds):
            pass

        def close(self):
            pass

    def session_with_socket(self):
        session = BR.BrowserSession()
        socket = self.Socket({1: {"id": 1, "result": {"windowId": 42}},
                              2: {"id": 2, "result": {}}, 3: {"id": 3, "result": {}}})
        websocket = Mock(spec=["create_connection"])
        websocket.create_connection.return_value = socket
        page = dict(id="t1", url=LOBBY_URL,
                    webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/t1")
        for patcher in (patch.object(session, "_cdp_page", return_value=page),
                        patch.object(session, "_cdp_json", return_value={
                            "Browser": "Chrome/123.0",
                            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"}),
                        patch.dict(sys.modules, {"websocket": websocket})):
            patcher.start()
            self.addCleanup(patcher.stop)
        return session, socket

    def test_resize_sends_a_normal_window_for_the_default_config(self):
        session, socket = self.session_with_socket()
        ok, note = session.resize_window()
        self.assertTrue(ok, note)
        self.assertEqual([message["params"]["bounds"] for message in socket.sent[1:]],
                         [{"windowState": "normal"},
                          {"width": BR.WIN_W, "height": BR.WIN_H, "windowState": "normal"}])
        self.assertIn("%d × %d" % (BR.WIN_W, BR.WIN_H), note)
        self.assertNotIn("toàn màn hình", note)

    def test_resize_to_fullscreen_uses_window_state_then_reports_scale(self):
        session, socket = self.session_with_socket()
        ok, note = session.resize_window(BR.normalize_window(fullscreen=True))
        self.assertTrue(ok, note)
        self.assertEqual([message["params"]["bounds"] for message in socket.sent[1:]],
                         [{"windowState": "normal"}, {"windowState": "fullscreen"}])
        self.assertIn("toàn màn hình", note)

    def test_resize_applies_the_custom_size_and_keeps_scale_as_a_restart_hint(self):
        session, socket = self.session_with_socket()
        ok, note = session.resize_window(BR.normalize_window((1280, 720), 80, False), pos=(40, 30))
        self.assertTrue(ok, note)
        self.assertEqual(socket.sent[-1]["params"]["bounds"],
                         {"width": 1280, "height": 720, "windowState": "normal", "left": 40, "top": 30})
        self.assertIn("1280 × 720", note)
        self.assertIn("mở lại Chrome", note)


class ChipMatchingTests(unittest.TestCase):
    """Chip dùng đúng thang số của sàn (VN2); phải ghép đúng mệnh giá thật trên khay."""

    TRAY = [20, 50, 100, 500, 2000, 10000]

    def live(self, banker=0, player=0, tie=0, unit=1, denominations=None):
        spots = {}
        for side, units in (("B", banker), ("P", player), ("T", tie)):
            spots[side] = {"amount": units, "amount_vnd": float(units * unit),
                           "chips": [], "chips_vnd": []}
        return {"ok": True, "unit": unit,
                "denominations": self.TRAY if denominations is None else denominations,
                "spots": spots, "total": float((banker + player + tie) * unit),
                "total_units": banker + player + tie}

    def test_chip_value_is_the_number_shown_on_the_table(self):
        # Đơn vị mặc định = 1: số trên chip chính là số tiền theo VN2 của sàn.
        self.assertEqual(TB.decompose(20)["chips"], [20])
        self.assertEqual(TB.decompose(100)["chips"], [100])
        self.assertEqual(TB.decompose(500)["chips"], [500])

    def test_120_needs_one_100_and_one_20(self):
        split = TB.decompose(120)
        self.assertTrue(split["exact"])
        self.assertEqual(sorted(split["chips"]), [20, 100])
        self.assertEqual(split["total_vnd"], 120)

    def test_140_needs_100_plus_two_20s(self):
        split = TB.decompose(140)
        self.assertTrue(split["exact"])
        self.assertEqual(split["chips"], [100, 20, 20])

    def test_decomposition_reports_an_ungroupable_amount(self):
        # Khay không có mệnh giá 10 nên 10 không ghép được.
        split = TB.decompose(10, 1, [20, 50, 100])
        self.assertFalse(split["exact"])
        self.assertEqual(split["remainder_vnd"], 10)

    def test_compare_match_uses_the_table_amount(self):
        result = ME.compare_chips("B", 120, self.live(banker=120))
        self.assertEqual(result["status"], "match")
        self.assertEqual(sorted(result["suggested_chips"]), [20, 100])

    def test_compare_flags_wrong_amount_and_still_suggests_chips(self):
        result = ME.compare_chips("B", 140, self.live(banker=120))
        self.assertEqual(result["status"], "wrong_amount")
        self.assertEqual(result["suggested_chips"], [100, 20, 20])

    def test_compare_flags_wrong_side(self):
        result = ME.compare_chips("P", 100, self.live(banker=100))
        self.assertEqual(result["status"], "wrong_side")

    def test_compare_flags_extra_bets(self):
        result = ME.compare_chips("B", 100, self.live(banker=100, player=50))
        self.assertEqual(result["status"], "extra_bets")

    def test_compare_no_bet_still_offers_the_chip_recipe(self):
        result = ME.compare_chips("B", 100, self.live())
        self.assertEqual(result["status"], "no_bet")
        self.assertEqual(result["suggested_chips"], [100])

    def test_compare_unknown_when_the_table_was_not_read(self):
        result = ME.compare_chips("B", 100, {"ok": False})
        self.assertEqual(result["status"], "unknown")

    def test_compare_rejects_an_amount_the_tray_cannot_make(self):
        result = ME.compare_chips("B", 10, self.live(banker=0, denominations=[20, 50, 100]))
        self.assertEqual(result["status"], "ungroupable")

    def test_custom_unit_still_supported_for_other_tables(self):
        # Bàn khác có thể đặt 1 đơn vị = 10: chip "20" là 200.
        split = TB.decompose(200, 10)
        self.assertEqual(split["chips"], [20])


class FrameOffsetAndSpotConfirmTests(unittest.TestCase):
    """Đo lệch khung và đối chiếu chip đã lên ô cược — hàm thuần, không cần bàn thật."""

    def quad(self, x, y):
        return [x, y, x + 100, y, x + 100, y + 40, x, y + 40]

    def command(self, boxes, fail=None):
        tree = {"frame": {"id": "top"}, "childFrames": [
            {"frame": {"id": "mid"}, "childFrames": [{"frame": {"id": "table"}}]}]}
        owners = {"table": 11, "mid": 22}

        def command(method, params=None):
            if method == fail:
                raise RuntimeError("cdp_command_failed")
            if method == "DOM.getDocument":
                return {}
            if method == "Page.getFrameTree":
                return {"frameTree": tree}
            if method == "DOM.getFrameOwner":
                return {"backendNodeId": owners[params["frameId"]]}
            if method == "DOM.getBoxModel":
                return {"model": boxes[params["backendNodeId"]]}
            raise AssertionError("CDP giả không biết lệnh " + method)
        return command

    def test_page_point_adds_the_frame_offset(self):
        self.assertEqual(TB.page_point({"x": 400, "y": 300}, {"x": 8, "y": 64}),
                         {"x": 408.0, "y": 364.0})
        self.assertEqual(TB.page_point({"x": 400, "y": 300}, None), {"x": 400.0, "y": 300.0})
        self.assertIsNone(TB.page_point(None, {"x": 0, "y": 0}))
        self.assertIsNone(TB.page_point({"x": 1}, {"x": 0, "y": 0}))

    def test_a_top_level_frame_needs_no_offset(self):
        for frame in ("top", None, "không có trong cây"):
            with self.subTest(frame=frame):
                self.assertEqual(TB.frame_offset_via_cdp(self.command({}), frame),
                                 {"x": 0.0, "y": 0.0, "depth": 0})

    def test_nested_frames_sum_the_content_box_of_each_iframe(self):
        boxes = {
            11: {"content": self.quad(13, 23), "padding": self.quad(10, 20), "border": self.quad(8, 18)},
            22: {"content": self.quad(0, 64), "padding": self.quad(0, 64), "border": self.quad(0, 64)},
        }
        # (13,23) là ô content của iframe trong khung mid, đã nằm trong viền 2 + padding 3.
        # Cộng thêm padding − border sẽ đội thành (15,25): bấm lệch đúng bề dày viền.
        self.assertEqual(TB.frame_offset_via_cdp(self.command(boxes), "table"),
                         {"x": 13.0, "y": 87.0, "depth": 2})

    def test_an_unmeasurable_frame_returns_none_instead_of_guessing(self):
        for boxes, fail in (({11: {"content": self.quad(13, 23)}}, "DOM.getBoxModel"),
                            ({}, "Page.getFrameTree"),
                            ({11: {}}, None)):
            with self.subTest(fail=fail, boxes=boxes):
                self.assertIsNone(TB.frame_offset_via_cdp(self.command(boxes, fail=fail), "table"))

    def test_spot_amount_confirms_as_soon_as_the_chips_land(self):
        reads, clock = iter([0, 0, 20]), iter([0, 0.1, 0.2, 0.3])
        ok, value = TB.confirm_spot_amount(0, 20, lambda: next(reads), wait=2, poll=0,
                                           clock=lambda: next(clock), nap=lambda _: None)
        self.assertTrue(ok)
        self.assertEqual(value, 20)

    def test_spot_amount_reports_what_really_landed_when_the_window_expires(self):
        clock = iter([0, 0.1, 5.0])
        ok, value = TB.confirm_spot_amount(100, 20, lambda: 100, wait=2, poll=0,
                                           clock=lambda: next(clock), nap=lambda _: None)
        self.assertFalse(ok)
        self.assertEqual(value, 100)

    def test_an_unreadable_spot_is_not_a_confirmation(self):
        clock = iter([0, 3.0])
        ok, value = TB.confirm_spot_amount(0, 20, lambda: None, wait=2, poll=0,
                                           clock=lambda: next(clock), nap=lambda _: None)
        self.assertFalse(ok)
        self.assertEqual(value, 0)


class FakeTableCdp:
    """CDP giả của màn hình bàn: khay render dần theo kịch bản, mọi cú bấm được ghi lại.

    Chỉ mô phỏng đọc DOM và ghi nhận sự kiện chuột, không chạm tới bàn cược thật. Bấm
    trúng khay thì đổi mệnh giá đang chọn, bấm trúng ô cược thì cộng chip đang chọn vào
    ô — nên nếu toạ độ lệch khung (thiếu frame_offset) thì không chip nào lên bàn và
    place_bet phải báo thất bại, giống hệt lỗi trên bàn thật.
    """

    def __init__(self, tray_snapshots, offset=None):
        self.tray_snapshots = tray_snapshots
        self.offset = {"x": 0, "y": 64, "depth": 1} if offset is None else offset
        self.spot = {"x": 400, "y": 300}
        self.tray_reads = 0
        self.clicks = []
        self.selected = None
        self.on_spot = 0.0
        self.outbox = []

    def settimeout(self, value):
        pass

    def close(self):
        pass

    def send(self, payload):
        request = json.loads(payload)
        method, params = request.get("method"), request.get("params") or {}
        if method == "Runtime.enable":
            self.outbox.append({"method": "Runtime.executionContextCreated", "params": {"context": {
                "id": 7, "auxData": {"isDefault": True, "frameId": "frame-table"}}}})
            result = {}
        elif method == "Runtime.evaluate":
            result = {"result": {"value": self._evaluate(params.get("expression") or "")}}
        elif method == "Input.dispatchMouseEvent":
            self._mouse(params)
            result = {}
        else:
            result = {}
        self.outbox.append({"id": request.get("id"), "result": result})

    def recv(self):
        if not self.outbox:
            raise AssertionError("CDP giả hết dữ liệu: place_bet hỏi nhiều hơn kịch bản")
        return json.dumps(self.outbox.pop(0))

    def _tray(self):
        return self.tray_snapshots[min(self.tray_reads, len(self.tray_snapshots) - 1)]

    def _evaluate(self, expression):
        if "frameOffset" in expression:
            tray = {str(k): dict(v) for k, v in self._tray().items()}
            self.tray_reads += 1
            return {"spot": dict(self.spot), "chips": tray,
                    "frame_offset": dict(self.offset) if self.offset is not None else None,
                    "is_top": False}
        if "amount += v" in expression:
            return {"amount": self.on_spot, "chips": []}
        return self.selected

    def _mouse(self, params):
        self.clicks.append(dict(params))
        if params.get("type") != "mouseReleased":
            return
        offset = self.offset or {}
        local = (params.get("x") - (offset.get("x") or 0),
                 params.get("y") - (offset.get("y") or 0))
        for denom, chip in self._tray().items():
            if (chip["x"], chip["y"]) == local:
                self.selected = int(denom)
                return
        if (self.spot["x"], self.spot["y"]) == local:
            self.on_spot += float(self.selected or 0)


class PlaceBetLiveTrayTests(unittest.TestCase):
    """Mệnh giá phải chốt theo khay THẬT lúc bấm, không theo bản đọc lúc chốt kế hoạch."""

    def setUp(self):
        for target, name, value in ((TB.time, "sleep", lambda *_: None),
                                    (TB, "TARGETS_WAIT_POLL", 0.0)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def place(self, fake, chips, amount=None, human=False):
        session = SimpleNamespace(_cdp_page=Mock(return_value={
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"}))
        with patch("websocket.create_connection", return_value=fake):
            return TB.place_bet(session, "B", chips, unit=1, human_mouse=human,
                                click_delay=0 if not human else 0.10, amount=amount)

    def tray(self, *denoms):
        return {d: {"x": 420 + 40 * i, "y": 700, "area": 400} for i, d in enumerate(denoms)}

    def test_a_tray_that_renders_after_the_plan_is_still_used(self):
        fake = FakeTableCdp([{}, self.tray(100), self.tray(20, 100)])
        ok, note, placed = self.place(fake, [], amount=20)
        self.assertTrue(ok, note)
        self.assertEqual(placed, {20: 1})
        self.assertEqual(fake.on_spot, 20)
        self.assertGreaterEqual(fake.tray_reads, 3)
        released = [(c["x"], c["y"]) for c in fake.clicks if c["type"] == "mouseReleased"]
        # Khay 20 ở (420,700) trong khung bàn, ô cược ở (400,300): cả hai phải cộng lệch 64.
        self.assertEqual(released, [(420, 764), (400, 364)])

    def test_the_sites_20_40_50_tray_places_a_20_stake_with_one_chip(self):
        """Đúng khay của bàn người dùng báo: 20/40/50 và mức cược 20 VN2 = một chip 20."""
        fake = FakeTableCdp([self.tray(20, 40, 50)])
        ok, note, placed = self.place(fake, [], amount=20)
        self.assertTrue(ok, note)
        self.assertEqual(placed, {20: 1})
        self.assertEqual(fake.on_spot, 20)
        released = [(c["x"], c["y"]) for c in fake.clicks if c["type"] == "mouseReleased"]
        self.assertEqual(released, [(420, 764), (400, 364)])

    def test_placement_keeps_the_fast_pacing_so_the_bet_window_is_not_missed(self):
        """Cửa sổ đặt cược chỉ vài giây: nhịp lướt chuột và nghỉ giữa các bấm phải nhanh."""
        fake = FakeTableCdp([self.tray(20, 40, 50)])
        seen = {}
        naps = []
        real_mouse = TB.HM.HumanMouse

        def spy(command, **kwargs):
            seen.update(kwargs)
            return real_mouse(command, **kwargs)

        with patch.object(TB.HM, "HumanMouse", side_effect=spy), \
             patch.object(TB.HM.time, "sleep", lambda *_: None), \
             patch.object(TB.time, "sleep", side_effect=lambda s: naps.append(s)):
            ok, note, placed = self.place(fake, [], amount=20, human=True)
        self.assertTrue(ok, note)
        self.assertEqual(placed, {20: 1})
        self.assertEqual(seen["glide_seconds"], 0.25)
        self.assertIn(0.10, naps)

    def test_a_tray_that_cannot_form_the_stake_reports_its_real_denominations(self):
        fake = FakeTableCdp([self.tray(100, 500)])
        with patch.object(TB, "TARGETS_WAIT_TIMEOUT", 0.05):
            ok, note, placed = self.place(fake, [], amount=20)
        self.assertFalse(ok)
        self.assertIn("100, 500", note)
        self.assertIn("không ghép được 20", note)
        self.assertEqual(placed, {})
        self.assertEqual(fake.clicks, [])

    def test_the_live_tray_overrides_a_stale_chip_suggestion(self):
        fake = FakeTableCdp([self.tray(100)])
        ok, note, placed = self.place(fake, [20], amount=100)
        self.assertTrue(ok, note)
        self.assertEqual(placed, {100: 1})
        self.assertEqual(fake.selected, 100)

    def test_without_an_amount_the_given_chips_are_used_as_before(self):
        fake = FakeTableCdp([self.tray(20)])
        ok, note, placed = self.place(fake, [20, 20])
        self.assertTrue(ok, note)
        self.assertEqual(placed, {20: 2})
        self.assertEqual(fake.on_spot, 40)

    def test_a_missing_frame_offset_still_refuses_to_click(self):
        fake = FakeTableCdp([self.tray(20)])
        fake.offset = None
        with patch.object(TB, "frame_offset_via_cdp", return_value=None):
            ok, note, placed = self.place(fake, [20], amount=20)
        self.assertFalse(ok)
        self.assertIn("không đo được", note.lower())
        self.assertEqual(fake.clicks, [])


class BetGateTests(unittest.TestCase):
    """Gate "chờ ván mới": chỉ tự đặt chip khi cả 3 cửa đang trống (ván trước đã gom chip)."""

    def live(self, banker=0, player=0, tie=0, ok=True):
        return {"ok": ok, "spots": {
            "B": {"amount": banker}, "P": {"amount": player}, "T": {"amount": tie}}}

    def test_all_spots_empty_is_clear(self):
        self.assertTrue(app.table_is_clear(self.live()))

    def test_any_spot_with_chips_is_not_clear(self):
        self.assertFalse(app.table_is_clear(self.live(banker=100)))
        self.assertFalse(app.table_is_clear(self.live(tie=20)))

    def test_unread_table_is_never_considered_clear(self):
        self.assertFalse(app.table_is_clear({"ok": False}))
        self.assertFalse(app.table_is_clear(None))
        self.assertFalse(app.table_is_clear(self.live(ok=False)))

    def test_wait_returns_true_as_soon_as_the_table_clears(self):
        host = SimpleNamespace(exit_requested=False,
                               _read_chip_state=Mock(side_effect=[
                                   {"ok": True, "spots": {"B": {"amount": 100}, "P": {"amount": 0},
                                                          "T": {"amount": 0}}},
                                   {"ok": True, "spots": {"B": {"amount": 0}, "P": {"amount": 0},
                                                          "T": {"amount": 0}}}]))
        with patch.object(app.time, "sleep"):
            ok, note = app.BCRApp._wait_for_next_hand(host, 1, timeout=5, poll=0)
        self.assertTrue(ok)
        self.assertEqual(note, "")
        self.assertEqual(host._read_chip_state.call_count, 2)

    def test_wait_gives_up_after_timeout_with_chips_still_on_table(self):
        host = SimpleNamespace(exit_requested=False,
                               _read_chip_state=Mock(return_value={
                                   "ok": True, "spots": {"B": {"amount": 100}, "P": {"amount": 0},
                                                         "T": {"amount": 0}}}))
        clock = iter([0, 0.1, 0.1, 10.0])
        with patch.object(app.time, "sleep"), \
             patch.object(app.time, "monotonic", side_effect=lambda: next(clock)):
            ok, note = app.BCRApp._wait_for_next_hand(host, 1, timeout=5, poll=0)
        self.assertFalse(ok)
        self.assertIn("còn chip ván trước", note)

    def test_wait_aborts_immediately_when_the_app_is_closing(self):
        host = SimpleNamespace(exit_requested=True, _read_chip_state=Mock())
        ok, note = app.BCRApp._wait_for_next_hand(host, 1, timeout=5, poll=0)
        self.assertFalse(ok)
        self.assertTrue(note)
        host._read_chip_state.assert_not_called()


class PlacementGatedHandCountingTests(unittest.TestCase):
    """Vào bàn từ sảnh phải chờ tải trang: tay chưa bấm được chip thật thì chưa được tính.

    Đây là lỗi "chưa đặt được nhưng vẫn tính tay đầu tiên" — bật Tự đặt chip thì mỗi tay
    phải có xác nhận đặt chip thật trước khi tiền trong sổ giả lập nhúc nhích.
    """

    def setUp(self):
        self.now = 1_800_000_000.0
        clock = patch.object(app.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def row(self, letters, **changes):
        row = dict(key="baccarat01", label="Baccarat 01", letters=letters,
                   ts=self.now, stable=2, conflict=False)
        row.update(changes)
        return row

    def simulation(self, baseline=BASELINE):
        book = M.MoneyBook(capital=CAPITAL, unit=UNIT, take_profit_pct=100)
        sim = app.LiveSimulation(book, ladder=[UNIT * m for m in (1, 3, 7, 15, 32, 64)])
        sim.bind(self.row(baseline))
        self.assertTrue(sim.start(self.row(baseline), verified=True, now=self.now))
        return sim

    def next_hand(self, sim, letters, delay=1):
        """Một chữ mới trên cầu, đưa vào đúng luồng process() của poll."""
        self.now += delay
        return sim.process(self.row(letters, key=sim.table_key, ts=self.now), now=self.now)

    def test_hand_is_held_while_the_placement_is_not_confirmed(self):
        sim = self.simulation()
        sim.mark_placing()
        self.assertFalse(self.next_hand(sim, BASELINE + "B"))
        self.assertEqual(sim.letters, BASELINE)
        self.assertEqual(sim.results, list(BASELINE))
        self.assertEqual(sim.money.balance, CAPITAL)
        self.assertEqual(sim.log, [])
        self.assertIn("Chờ xác nhận", sim.note)

    def test_prompt_confirmation_settles_the_next_hand(self):
        sim = self.simulation()
        plan = sim.pending
        sim.mark_placing(plan)
        self.assertTrue(sim.mark_placed(True, "Đã đặt 1 đơn vị chip", plan))
        self.assertTrue(self.next_hand(sim, BASELINE + "B"))
        self.assertEqual(sim.letters, BASELINE + "B")
        self.assertEqual(len(sim.log), 1)
        self.assertEqual(sim.log[-1]["stake"], plan["stake"])
        self.assertIn(sim.log[-1]["outcome"], ("win", "loss", "push"))
        self.assertEqual(sim.place_skips, 0)

    def test_failed_placement_consumes_the_letter_without_touching_the_book(self):
        sim = self.simulation()
        plan = sim.pending
        sim.mark_placing(plan)
        sim.mark_placed(False, "ô cược không phản hồi", plan)
        method_state = deepcopy(sim.method_state)
        self.assertFalse(self.next_hand(sim, BASELINE + "B"))
        self.assertEqual(sim.letters, BASELINE + "B")
        self.assertEqual(sim.money.balance, CAPITAL)
        self.assertEqual(sim.money.hands_played, 0)
        self.assertEqual(sim.money.profit, 0)
        self.assertEqual(sim.method_state, method_state)
        self.assertEqual(sim.block_hands, 0)
        self.assertEqual(len(sim.log), 1)
        self.assertEqual(sim.log[-1]["outcome"], "not_placed")
        self.assertEqual(sim.log[-1]["stake"], 0)
        self.assertEqual(sim.log[-1]["delta"], 0.0)
        self.assertEqual(sim.log[-1]["source"], "chip-not-placed")
        self.assertTrue(sim.running)
        # Kế hoạch tay kế tiếp giữ nguyên bậc cược và chưa có dấu vết lượt bấm cũ.
        self.assertIsNot(sim.pending, plan)
        self.assertEqual(sim.pending["baseline"], BASELINE + "B")
        self.assertEqual(sim.pending["ladder_step"], plan["ladder_step"])
        self.assertNotIn("placed", sim.pending)
        self.assertIn("không được tính", sim.note)

    def test_stuck_pending_placement_fails_after_the_confirmation_timeout(self):
        sim = self.simulation()
        plan = sim.pending
        sim.mark_placing(plan)
        self.assertFalse(self.next_hand(sim, BASELINE + "B", delay=app.PLACE_CONFIRM_TIMEOUT + 1))
        self.assertEqual(plan["placed"], "failed")
        self.assertEqual(sim.letters, BASELINE + "B")
        self.assertEqual(sim.log[-1]["outcome"], "not_placed")
        self.assertEqual(sim.money.balance, CAPITAL)

    def test_unplaced_hands_stop_the_session_after_the_skip_limit(self):
        sim = self.simulation()
        road = BASELINE
        for expected in range(1, app.PLACE_SKIP_LIMIT + 1):
            plan = sim.pending
            sim.mark_placing(plan)
            sim.mark_placed(False, "chưa bấm được chip", plan)
            road += "P" if expected % 2 else "B"
            self.assertFalse(self.next_hand(sim, road))
            self.assertEqual(sim.place_skips, expected)
        self.assertFalse(sim.running)
        self.assertIn("kiểm tra khay chip", sim.note)
        self.assertEqual(sim.money.balance, CAPITAL)
        self.assertEqual(len(sim.log), app.PLACE_SKIP_LIMIT)

    def test_late_successful_confirmation_still_counts_the_held_hand(self):
        """Xác nhận chip về cùng nhịp poll với chữ kết quả vẫn phải tính tay.

        Cùng một mốc thời gian thì không phân biệt được trước/sau; dừng cả phiên ở đây
        chính là lỗi "tay đầu đặt được, các tay sau không đặt được nữa". Xác nhận muộn
        HẲN (giờ bấm thật sau lúc kết quả về) thì bỏ tay — xem test bên dưới.
        """
        sim = self.simulation()
        plan = sim.pending
        sim.mark_placing(plan)
        self.assertFalse(self.next_hand(sim, BASELINE + "B"))
        self.assertTrue(sim.mark_placed(True, "ô cược xác nhận +20", plan))
        self.assertTrue(self.next_hand(sim, BASELINE + "B"))
        self.assertTrue(sim.running)
        self.assertEqual(sim.letters, BASELINE + "B")
        self.assertEqual(len(sim.log), 1)
        self.assertIn(sim.log[-1]["outcome"], ("win", "loss", "push"))

    def test_late_failed_confirmation_skips_the_hand_and_keeps_running(self):
        sim = self.simulation()
        plan = sim.pending
        sim.mark_placing(plan)
        self.assertFalse(self.next_hand(sim, BASELINE + "B"))
        self.assertTrue(sim.mark_placed(False, "ô cược không phản hồi", plan))
        self.assertFalse(self.next_hand(sim, BASELINE + "B"))
        self.assertTrue(sim.running)
        self.assertEqual(sim.letters, BASELINE + "B")
        self.assertEqual(sim.money.balance, CAPITAL)
        self.assertEqual(len(sim.log), 1)
        self.assertEqual(sim.log[-1]["outcome"], "not_placed")

    def test_confirmation_after_the_result_skips_the_hand_and_keeps_the_ladder(self):
        """Chip bấm xong SAU khi kết quả tay này đã về: chip thuộc cửa sổ tay sau.

        Đúng lỗi người dùng báo: tay 1 chưa đặt được nhưng vẫn bị tính lỗ, tay 2 đáng
        lẽ đặt 40 (bậc đầu) thì kế hoạch lại báo 120 (bậc 2). Tay chưa đặt phải bị bỏ,
        vốn và bậc cược không nhúc nhích, phiên vẫn chạy.
        """
        sim = self.simulation()
        plan = sim.pending
        sim.mark_placing(plan)
        self.assertFalse(self.next_hand(sim, BASELINE + "B"))
        self.now += 5
        self.assertTrue(sim.mark_placed(True, "ô cược xác nhận +40", plan))
        method_state = deepcopy(sim.method_state)
        self.assertFalse(self.next_hand(sim, BASELINE + "B"))
        self.assertTrue(sim.running)
        self.assertEqual(sim.letters, BASELINE + "B")
        self.assertEqual(sim.money.balance, CAPITAL)
        self.assertEqual(sim.money.hands_played, 0)
        self.assertEqual(sim.money.profit, 0)
        self.assertEqual(sim.method_state, method_state)
        self.assertEqual(sim.block_hands, 0)
        self.assertEqual(len(sim.log), 1)
        self.assertEqual(sim.log[-1]["outcome"], "not_placed")
        self.assertEqual(sim.log[-1]["stake"], 0)
        self.assertEqual(sim.log[-1]["source"], "chip-not-placed")
        self.assertEqual(sim.pending["ladder_step"], plan["ladder_step"])
        self.assertEqual(sim.pending["stake"], plan["stake"])
        self.assertEqual(sim.pending["baseline"], BASELINE + "B")
        self.assertIn("không được tính", sim.note)

    def test_click_timestamped_before_the_result_settles_even_if_reported_late(self):
        """Giờ bấm THẬT trong thread nền mới là căn cứ, không phải giờ báo cáo về UI.

        Chip đã lên ô cược trước khi kết quả về (placed_at sớm) thì dù lượt xác nhận
        đọc được muộn, tay này vẫn là tay có cược thật và phải được tính.
        """
        sim = self.simulation()
        plan = sim.pending
        sim.mark_placing(plan)
        plan["placed_at"] = self.now
        self.assertFalse(self.next_hand(sim, BASELINE + "B", delay=10))
        self.now += 3
        self.assertTrue(sim.mark_placed(True, "ô cược xác nhận +20", plan))
        self.assertTrue(self.next_hand(sim, BASELINE + "B"))
        self.assertTrue(sim.running)
        self.assertEqual(sim.letters, BASELINE + "B")
        self.assertEqual(len(sim.log), 1)
        self.assertIn(sim.log[-1]["outcome"], ("win", "loss", "push"))
        self.assertEqual(sim.place_skips, 0)

    def test_hands_are_still_counted_when_no_placement_is_tracked(self):
        """Giả lập thuần (Tự đặt chip tắt): không có mark_placing thì mọi tay vẫn tính."""
        sim = self.simulation()
        self.assertTrue(self.next_hand(sim, BASELINE + "B"))
        self.assertEqual(sim.letters, BASELINE + "B")
        self.assertEqual(len(sim.log), 1)
        self.assertNotEqual(sim.money.balance, CAPITAL)

    def test_a_late_result_cannot_label_the_plan_that_replaced_it(self):
        sim = self.simulation()
        old = sim.pending
        sim.mark_placing(old)
        fresh = {key: value for key, value in old.items() if key not in ("placed", "placing_at")}
        sim.pending = dict(fresh, armed_at=self.now)
        self.assertFalse(sim.mark_placed(True, "kết quả của lượt cũ", old))
        self.assertNotIn("placed", sim.pending)
        self.assertEqual(old["placed"], "pending")


class AutoPlaceReportingTests(unittest.TestCase):
    """Lượt tự đặt chip phải báo kết quả về sim ở MỌI nhánh thoát, kể cả nhánh lỗi."""

    class InlineThread:
        def __init__(self, target=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    def setUp(self):
        self.now = 1_800_000_000.0
        clock = patch.object(app.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def host(self, sim, **changes):
        host = SimpleNamespace(
            sim=sim,
            tables={sim.table_key: dict(key=sim.table_key, letters=sim.letters)},
            auto_place_var=Mock(get=Mock(return_value=True)),
            human_mouse_var=Mock(get=Mock(return_value=False)),
            place_busy=False, exit_requested=False,
            chip_live={"denominations": [20, 40, 50]},
            place_status=Mock(),
            events=queue.Queue(), browser=Mock(),
            _wait_for_next_hand=Mock(return_value=(True, "")),
        )
        # place_armed_bet gọi các helper qua self; host rút gọn phải có đúng method thật.
        for name in ("_set_place_status", "_finish_place", "_on_placed"):
            setattr(host, name, MethodType(getattr(app.BCRApp, name), host))
        for key, value in changes.items():
            setattr(host, key, value)
        return host

    def simulation(self):
        book = M.MoneyBook(capital=CAPITAL, unit=UNIT, take_profit_pct=100)
        row = dict(key="baccarat01", label="Baccarat 01", letters=BASELINE,
                   ts=self.now, stable=2, conflict=False)
        sim = app.LiveSimulation(book, ladder=[20, 60, 140, 300, 640, 1280])
        sim.bind(row)
        self.assertTrue(sim.start(row, verified=True, now=self.now))
        return sim

    def run_placement(self, host, place_bet=None, threads=True):
        thread = self.InlineThread if threads else Mock()
        with patch.object(app.threading, "Thread", thread), \
             patch.object(TB, "place_bet", place_bet or Mock(return_value=(True, "Đã đặt", {1: 1}))):
            app.BCRApp.place_armed_bet(host, host.sim.pending)
            while not host.events.empty():
                kind, data = host.events.get_nowait()
                self.assertEqual(kind, "placed")
                app.BCRApp._on_placed(host, *data)
        return thread

    def test_success_is_reported_against_the_plan_that_started_it(self):
        host = self.host(self.simulation())
        plan = host.sim.pending
        self.run_placement(host)
        self.assertEqual(plan["placed"], "ok")
        self.assertEqual(plan["baseline"], BASELINE)
        self.assertFalse(host.place_busy)

    def test_gate_timeout_is_reported_as_not_placed(self):
        host = self.host(self.simulation(),
                         _wait_for_next_hand=Mock(return_value=(False, "bàn vẫn còn chip ván trước")))
        plan = host.sim.pending
        self.run_placement(host)
        self.assertEqual(plan["placed"], "failed")
        self.assertIn("chip ván trước", plan["place_note"])

    def test_result_arriving_before_the_click_aborts_instead_of_betting_next_window(self):
        """Kết quả tay này đã về trong lúc chờ gate: cửa sổ đặt cược đã đóng.

        Bấm tiếp là chip rơi sang cửa sổ tay sau — đúng lỗi "tay 1 chưa đặt được, tay 2
        mang chip 40 trong khi kế hoạch báo 120". Phải bỏ lượt, không gọi place_bet.
        """
        sim = self.simulation()
        host = self.host(sim)
        host.tables[sim.table_key]["letters"] = BASELINE + "B"
        plan = sim.pending
        place_bet = Mock(return_value=(True, "Đã đặt", {1: 1}))
        self.run_placement(host, place_bet=place_bet)
        place_bet.assert_not_called()
        self.assertEqual(plan["placed"], "failed")
        self.assertIn("trước khi bấm chip", plan["place_note"])

    def test_missing_table_row_does_not_block_the_click(self):
        """Chưa đọc được hàng cầu (mới vào bàn) thì không có căn cứ bỏ lượt: cứ xuống bàn."""
        sim = self.simulation()
        host = self.host(sim, tables={})
        plan = sim.pending
        self.run_placement(host)
        self.assertEqual(plan["placed"], "ok")

    def test_placement_timestamp_is_the_click_commit_not_the_slow_confirmation(self):
        """Giờ đặt = lúc cửa sổ còn mở (trước place_bet), không phải lúc đọc xác nhận xong.

        Vòng xác nhận chip trên ô cược có thể hoàn tất sau khi kết quả đã về; nếu ghi giờ
        đó thành giờ bấm thì tay đặt thật bị bỏ oan (lỗi "đặt được nhưng không tính tay").
        """
        sim = self.simulation()
        host = self.host(sim)
        plan = sim.pending
        click_time = self.now

        def slow_confirm(*args, **kwargs):
            self.now += 30
            return True, "Đã đặt · ô cược xác nhận +20", {20: 1}

        self.run_placement(host, place_bet=slow_confirm)
        self.assertEqual(plan["placed"], "ok")
        self.assertEqual(plan["placed_at"], click_time)
        self.assertEqual(plan["confirmed_at"], click_time)
        self.assertLess(plan["confirmed_at"], self.now)

    def test_a_crash_inside_the_placement_thread_still_reports(self):
        host = self.host(self.simulation())
        plan = host.sim.pending
        self.run_placement(host, place_bet=Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(plan["placed"], "failed")
        self.assertIn("Lỗi khi tự đặt chip", plan["place_note"])

    def test_busy_placer_reports_instead_of_staying_silent(self):
        host = self.host(self.simulation(), place_busy=True)
        plan = host.sim.pending
        threads = self.run_placement(host, threads=False)
        threads.assert_not_called()
        self.assertEqual(plan["placed"], "failed")
        self.assertIn("chưa xong", plan["place_note"])
        self.assertTrue(host.place_busy)

    def test_chip_print_is_the_money_so_no_multiplier_rejects_a_small_stake(self):
        """Khay 20/40/50 và mức cược 20 VN2: phải bấm đúng một chip 20, không nhân hệ số.

        Đây là lỗi "20 VN2 nhỏ hơn 1 đơn vị chip (1.000 VN2)": một hệ số nhân cũ giữa số
        in trên chip và số tiền đã chặn oan mọi mức cược nhỏ hơn hệ số đó.
        """
        host = self.host(self.simulation())
        plan = host.sim.pending
        self.assertEqual(plan["stake"], 20)
        place_bet = Mock(return_value=(True, "Đã đặt 1 chip 20", {20: 1}))
        self.run_placement(host, place_bet=place_bet)
        self.assertEqual(plan["placed"], "ok")
        args, kwargs = place_bet.call_args
        self.assertEqual(args[2], [])
        self.assertEqual(kwargs["amount"], 20)
        self.assertEqual(kwargs["unit"], TB.DEFAULT_CHIP_UNIT)
        self.assertNotIn("nhỏ hơn 1 đơn vị chip", plan.get("place_note", ""))

    def test_a_stale_tray_does_not_cancel_the_hand_before_the_table_opens(self):
        """Khay chưa render xong lúc chốt kế hoạch: vẫn phải xuống tận bàn đọc lại.

        Đây là lỗi "bàn đã mở cửa sổ chọn chip lên nhưng lại không chọn được": app kết
        luận "không ghép được" từ bản đọc chip lưu tạm rồi bỏ tay, trước cả khi place_bet
        kịp chờ khay hiện ra.
        """
        host = self.host(self.simulation(), chip_live={"denominations": [100, 500]})
        plan = host.sim.pending
        place_bet = Mock(return_value=(True, "Đã đặt 1 đơn vị chip", {1: 1}))
        self.run_placement(host, place_bet=place_bet)
        self.assertEqual(plan["placed"], "ok")
        args, kwargs = place_bet.call_args
        self.assertEqual(args[2], [])
        self.assertEqual(kwargs["amount"], plan["stake"])

    def test_the_live_tray_failure_is_reported_as_not_placed(self):
        host = self.host(self.simulation(), chip_live={"denominations": [100, 500]})
        plan = host.sim.pending
        note = "Khay chip của bàn chỉ có mệnh giá 100, 500 — không ghép được 20."
        self.run_placement(host, place_bet=Mock(return_value=(False, note, {})))
        self.assertEqual(plan["placed"], "failed")
        self.assertIn("không ghép được", plan["place_note"])

    def test_closing_the_app_reports_the_cancellation(self):
        host = self.host(self.simulation(), exit_requested=True)
        plan = host.sim.pending
        threads = self.run_placement(host, threads=False)
        threads.assert_not_called()
        self.assertEqual(plan["placed"], "failed")

    def test_auto_place_off_marks_the_plan_not_placed(self):
        host = self.host(self.simulation(), auto_place_var=Mock(get=Mock(return_value=False)))
        plan = host.sim.pending
        self.run_placement(host, threads=False)
        self.assertEqual(plan["placed"], "failed")
        self.assertIn("bật", plan["place_note"])

    def test_a_result_for_another_plan_does_not_touch_the_current_one(self):
        host = self.host(self.simulation())
        current = host.sim.pending
        other = dict(current, armed_at=self.now)
        app.BCRApp._on_placed(host, True, "kết quả lượt cũ", other)
        self.assertFalse(host.place_busy)
        self.assertNotIn("placed", current)
        self.assertNotIn("placed", other)


if __name__ == "__main__":
    unittest.main(verbosity=2)
