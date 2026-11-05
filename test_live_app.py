"""In-memory live simulation and URL regressions; no UI, browser, or state files."""

import sys

sys.dont_write_bytecode = True

from copy import deepcopy
import json
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, mock_open, patch
from urllib.parse import urlsplit

import app
import browser as BR
import engine as E
import lobby_stream as LS
import method as ME
import money as M


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
        host = SimpleNamespace(tables={row["key"]: row}, sim=app.LiveSimulation(book), verified=Mock(),
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
        self.assertTrue(host.verified.set.called)
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
        sim = app.LiveSimulation(book)
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
        for letters in (BASELINE[:-1], BASELINE + "BPBPBP", "BBBBBBBPPBPPBPB",
                        "BBBPPPBPBPBPBPB"):
            with self.subTest(letters=letters):
                self.assertFalse(ME.check_table(list(letters))["eligible"])
                sim = app.LiveSimulation()
                row = self.row(letters)
                sim.bind(row)
                self.assertFalse(sim.start(row, verified=True, now=self.now))
                self.assertFalse(sim.running)
                self.assertIsNone(sim.pending)
                self.assert_pristine_book(sim)

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
        self.assertFalse(sim.running)
        self.assertTrue(sim.cycle_done)
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
                self.assertFalse(sim.running)
                self.assertIsNone(sim.pending)
                self.assertEqual(sim.block_hands, sim.cycle_hands)
                self.assertTrue(sim.cycle_done)

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
        self.assertGreater(sim.money.balance, sum((1, 3, 7, 15, 31, 64)) * UNIT)
        for side, result, multiplier in zip("BBBPPP", "PPPBBB", (1, 3, 7, 15, 31, 64)):
            self.assertEqual(sim.pending["side"], side)
            self.assertEqual(sim.pending["stake"], multiplier * UNIT)
            self.settle_next(sim, result)
        self.assertEqual([entry["stake"] for entry in sim.log],
                         [UNIT * n for n in (1, 3, 7, 15, 31, 64)])
        self.assertEqual(sim.money.profit, -121 * UNIT)
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

    def test_six_hands_leave_the_table_and_the_next_table_recalculates_strong(self):
        for baseline, old, new in ((BASELINE, "B", "P"), (PLAYER_BASELINE, "P", "B")):
            with self.subTest(old=old, new=new):
                sim = self.simulation(baseline)
                for position, side in enumerate(old * 3 + new * 3):
                    self.assertEqual(sim.pending["side"], side)
                    self.assertEqual(sim.pending["strong"], old)
                    self.assertEqual(sim.pending["block_pos"], position)
                    self.settle_next(sim, new)
                self.assertEqual(ME.strong_side(sim.results)[0], new)
                self.assertTrue(sim.method_state["started"])
                self.assertFalse(sim.running)
                self.assertIsNone(sim.pending)
                self.assertTrue(sim.cycle_done)
                self.assertIn("đủ 6 tay", sim.note)
                # Khối mới chỉ diễn ra ở bàn khác, tính bên mạnh theo cầu của bàn đó.
                row = self.row(PLAYER_BASELINE if new == "P" else BASELINE)
                sim.bind(row)
                self.assertTrue(sim.start(row, verified=True, now=self.now))
                self.assertEqual(sim.pending["strong"], new)
                self.assertEqual(sim.pending["side"], new)
                self.assertEqual(sim.pending["block_pos"], 0)
                self.assertEqual(sim.pending["stake"], UNIT)
                self.assertEqual(sim.block_hands, 0)

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
        self.assertFalse(sim.running)
        self.assertTrue(sim.cycle_done)
        self.assertIn("đủ 5 tay của khối 3-2", sim.note)

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
        self.assertEqual(sim.pending["stake"], UNIT)
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
                 {"id": 3, "result": {}}, {"id": 4, "result": {}}, {"id": 5, "result": {}}]]

    def test_clicks_the_evolution_button_then_sets_the_baccarat_route(self):
        box = {"x": 320.5, "y": 640.25, "label": "Chơi game"}
        portal = self.click_transcript(box)
        lobby = [[{"method": "Runtime.executionContextCreated",
                   "params": {"context": {"id": 9, "auxData": {"isDefault": True}}}},
                  {"id": 1, "result": {}},
                  {"id": 2, "result": {}}]]
        self.scripted = portal + lobby
        self.session._cdp_pages.side_effect = [[self.PORTAL], [self.PORTAL, self.LOBBY]]
        ok, note = self.stream.open_lobby_from_portal()
        self.assertTrue(ok, note)
        self.assertIn("danh sách bàn Baccarat", note)
        portal_socket, lobby_socket = self.sockets
        clicks = [message for message in portal_socket.sent
                  if message["method"] == "Input.dispatchMouseEvent"]
        self.assertEqual([message["params"]["type"] for message in clicks],
                         ["mouseMoved", "mousePressed", "mouseReleased"])
        self.assertEqual({message["params"]["x"] for message in clicks}, {box["x"]})
        self.assertTrue(lobby_socket.closed and portal_socket.closed)
        evaluate = [message for message in lobby_socket.sent if message["method"] == "Runtime.evaluate"]
        self.assertEqual(evaluate[0]["params"]["expression"],
                         'location.hash = "category=all_games&game=baccarat&see=baccarat"')
        self.assertEqual(evaluate[0]["params"]["contextId"], 9)

    def test_uses_the_portal_tab_and_keeps_a_tab_that_is_already_routed(self):
        self.session._cdp_pages.return_value = [self.PORTAL, dict(self.LOBBY, url=LOBBY_URL)]
        self.scripted = self.click_transcript({"x": 10.0, "y": 20.0})
        ok, note = self.stream.open_lobby_from_portal()
        self.assertTrue(ok, note)
        self.assertEqual(len(self.sockets), 1, "Sảnh đã đúng route thì không sửa hash nữa")

    def test_reports_why_the_evolution_button_was_not_clickable(self):
        for reason in ("evo_item_not_found", "evo_button_not_found", "evo_button_offscreen",
                       "not_top_frame"):
            with self.subTest(reason=reason):
                self.sockets, self.scripted = [], self.click_transcript({"reason": reason})
                self.session._cdp_pages.return_value = [self.PORTAL]
                ok, note = self.stream.open_lobby_from_portal()
                self.assertFalse(ok)
                self.assertIn(reason, note)
                self.assertNotIn("Input.dispatchMouseEvent",
                                 [message["method"] for message in self.sockets[0].sent])

    def test_needs_an_approved_portal_tab_before_clicking_anything(self):
        self.session._cdp_pages.return_value = [dict(self.PORTAL, url="https://example.invalid/")]
        ok, note = self.stream.open_lobby_from_portal()
        self.assertFalse(ok)
        self.assertIn(BR.PORTAL_HOST, note)
        self.websocket.create_connection.assert_not_called()

    def test_reports_when_the_lobby_tab_never_appears(self):
        self.scripted = self.click_transcript({"x": 10.0, "y": 20.0})
        self.session._cdp_pages.side_effect = [[self.PORTAL]] * 5
        ok, note = self.stream.open_lobby_from_portal(wait_seconds=0)
        self.assertFalse(ok)
        self.assertIn("chưa thấy tab sảnh", note)

    def test_accepts_a_lobby_embedded_in_the_entry_page(self):
        portal = self.click_transcript({"x": 10.0, "y": 20.0})
        portal[0].append({"id": 6, "result": {"result": {"value": {
            "tiles": [{"id": "t1", "title": "Baccarat A", "top": 40}],
            "scroll": {"top": 0, "max": 900, "client": 600}}}}})
        self.scripted = portal
        self.session._cdp_pages.return_value = [self.PORTAL]
        ok, note = self.stream.open_lobby_from_portal()
        self.assertTrue(ok, note)
        self.assertIn("Quét toàn sảnh", note)
        self.assertEqual(len(self.sockets), 1, "Sảnh trong iframe thì không mở tab nào thêm")


class LobbyListReturnTests(unittest.TestCase):
    """Rời màn hình bàn về danh sách bàn bằng cách đổi hash (CDP giả lập, không mở Chrome)."""

    TABLE = dict(id="table", type="page",
                 url="https://tmrw17k36as.evo-games.com/frontend/evo/r3/#category=all_games&table_id=t1",
                 webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/table")
    LIST = dict(id="table", type="page", url=LOBBY_URL,
                webSocketDebuggerUrl="ws://127.0.0.1:9222/devtools/page/table")
    TILES = {"tiles": [{"id": "t2", "title": "Baccarat A", "top": 40}],
             "scroll": {"top": 0, "max": 900, "client": 600}}

    def setUp(self):
        self.session = Mock()
        self.stream = LS.LobbyStream(self.session)
        self.sockets = []

        def connect(endpoint, **kwargs):
            socket = LobbyEntryTests.Socket(self.scripted[len(self.sockets)])
            self.sockets.append(socket)
            return socket

        patcher = patch.dict(sys.modules, {"websocket": Mock(create_connection=connect)})
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = patch("time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def transcript(self, tiles_seen=True):
        messages = [{"method": "Runtime.executionContextCreated",
                     "params": {"context": {"id": 9, "auxData": {"isDefault": True}}}},
                    {"id": 1, "result": {}}, {"id": 2, "result": {}}]
        value = self.TILES if tiles_seen else {"tiles": []}
        messages.append({"id": 3, "result": {"result": {"value": value}}})
        return [messages]

    def test_drops_table_id_and_waits_for_the_tiles_to_come_back(self):
        self.scripted = self.transcript()
        self.session._cdp_page.side_effect = [self.TABLE, self.LIST]
        ok, note = self.stream.back_to_lobby()
        self.assertTrue(ok, note)
        self.assertIn("danh sách bàn Baccarat", note)
        evaluate = [message for message in self.sockets[0].sent if message["method"] == "Runtime.evaluate"]
        self.assertEqual(evaluate[0]["params"]["expression"],
                         'location.hash = "category=all_games&game=baccarat&see=baccarat"')
        self.assertEqual(evaluate[0]["params"]["contextId"], 9)
        self.assertTrue(self.sockets[0].closed)

    def test_refuses_a_tab_that_is_not_the_lobby(self):
        self.scripted = self.transcript()
        self.session._cdp_page.return_value = LobbyEntryTests.PORTAL
        ok, note = self.stream.back_to_lobby()
        self.assertFalse(ok)
        self.assertIn("Không có tab sảnh", note)
        self.assertEqual(self.sockets, [], "Không nối CDP vào tab không phải sảnh")

    def test_reports_when_the_list_never_renders(self):
        self.scripted = self.transcript(tiles_seen=False)
        self.session._cdp_page.side_effect = [self.TABLE, self.LIST]
        ok, note = self.stream.back_to_lobby(wait_seconds=0)
        self.assertFalse(ok)
        self.assertIn("chưa thấy lại danh sách bàn", note)


class TableCycleTests(unittest.TestCase):
    """Chu trình của người dùng: đủ 6 tay thì rời bàn, quét sảnh và mở bàn đạt chuẩn khác."""

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
            cycle_waiting=False, cycle_attempts=0, cycle_table_key=None, auto_start_letters="")
        for key, value in changes.items():
            setattr(host, key, value)
        return host

    def started_thread(self):
        captured = []

        def fake_thread(target=None, daemon=False):
            captured.append(target)
            return Mock(start=lambda: target())

        return captured, patch("app.threading.Thread", side_effect=fake_thread)

    def test_six_hands_note_sends_the_tab_back_to_the_lobby(self):
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

    def test_cycle_stays_off_when_the_user_disabled_it(self):
        host = self.host(auto_cycle_var=Mock(get=lambda: False))
        captured, patcher = self.started_thread()
        with patcher:
            app.BCRApp.leave_table_for_lobby(host)
        self.assertFalse(host.cycle_waiting)
        self.assertEqual(captured, [], "Tắt chu trình thì không điều hướng Chrome")
        self.assertEqual(host.sweep_note_text, "")

    def test_cycle_waits_if_chrome_is_not_attached(self):
        host = self.host(_cdp_available=lambda: False)
        app.BCRApp.leave_table_for_lobby(host)
        self.assertFalse(host.cycle_waiting)
        self.assertIn("chưa gắn Chrome", host.sweep_note_text)

    def test_the_finished_table_is_not_offered_again(self):
        host = self.host(tables={"id:t1": self.row("t1"), "id:t2": self.row("t2")},
                         cycle_waiting=True, cycle_table_key="id:t1")
        captured, patcher = self.started_thread()
        with patcher:
            app.BCRApp.auto_open_table(host, allow_sweep=True, exclude_key="id:t1")
        self.assertTrue(host.open_table_busy)
        host.stream.open_table.assert_called_once_with("t2")

    def test_search_gives_up_after_the_allowed_retries(self):
        host = self.host(cycle_waiting=True, cycle_attempts=app.MAX_CYCLE_SWEEPS)
        app.BCRApp.auto_open_table(host, allow_sweep=True, exclude_key="id:t1")
        self.assertFalse(host.cycle_waiting)
        self.assertEqual(host.cycle_attempts, 0)
        host.sweep_lobby.assert_not_called()
        self.assertIn("Chưa tìm được bàn đạt 3 điều kiện", host.sweep_note_text)

    def test_search_retries_the_sweep_while_cycling(self):
        host = self.host(cycle_waiting=True, cycle_attempts=1)
        app.BCRApp.auto_open_table(host, allow_sweep=True, exclude_key="id:t1")
        self.assertEqual(host.cycle_attempts, 2)
        host.sweep_lobby.assert_called_once_with()


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
                self.assertEqual(plan["stake"], 7 * UNIT)
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

    def test_only_exact_https_host_with_default_port_is_allowed(self):
        self.assertTrue(BR.is_lobby_host(self.host))
        for url in (LOBBY_URL, self.origin + BR.LOBBY_PATH + "/",
                    self.origin + ":443/frontend/evo/r3/",
                    "https://other-mirror.evo-games.com/frontend/evo/r3/#category=all_games",
                    "https://" + self.host.upper() + BR.LOBBY_PATH + "/", BR.START_URL):
            with self.subTest(url=url):
                self.assertTrue(BR.valid_site_url(url))

    def test_lobby_needs_the_evo_host_suffix_and_the_lobby_path(self):
        for url in (self.origin + "/", self.origin + "/other/path?table_id=01#category=all_games",
                    self.origin + "/frontend/evo/", self.origin + "/frontend/other"):
            with self.subTest(url=url):
                self.assertFalse(BR.valid_site_url(url))
        self.assertTrue(BR.valid_site_url(self.origin + BR.LOBBY_PATH + "/"))
        self.assertTrue(BR.valid_site_url(self.origin + BR.LOBBY_PATH + "/x"))
        # Sảnh nằm trong iframe của trang vào: hai URL đều thuộc danh sách được phép.
        self.assertTrue(BR.is_portal_url(BR.START_URL))
        self.assertTrue(BR.is_lobby_url(LOBBY_URL))
        self.assertFalse(BR.is_lobby_url(BR.START_URL))
        self.assertFalse(BR.is_portal_url(LOBBY_URL))

    def test_unrelated_and_lookalike_hosts_are_rejected(self):
        for host in ("example.invalid", "localhost", "127.0.0.1", "[::1]",
                     "notevo-games.com", "evo-games.com.example.invalid",
                     "prefix-evo-games.com", BR.LOBBY_HOST_SUFFIX + ".",
                     self.host + ".example.invalid"):
            with self.subTest(host=host):
                self.assertFalse(BR.valid_site_url("https://" + host + BR.LOBBY_PATH + "/"))
        # Mirror là bản gốc của Evolution nên mọi tên con trong *.evo-games.com đều được đọc.
        self.assertTrue(BR.valid_site_url("https://sub." + self.host + BR.LOBBY_PATH + "/"))
        for url in ("https://" + BR.PORTAL_HOST + "/",
                    "https://" + BR.PORTAL_HOST + "/gamelobby",
                    "https://" + BR.PORTAL_HOST + "/other/page",
                    "https://neo179.com" + BR.PORTAL_PATH):
            with self.subTest(url=url):
                self.assertFalse(BR.valid_site_url(url))

    def test_non_https_file_and_relative_urls_are_rejected(self):
        for url in ("http://" + self.host, "ftp://" + self.host,
                    "file:///C:/Users/Administrator/Desktop/BCR/test_live_app.py",
                    "file://" + self.host + "/", "data:text/plain,BPT",
                    "javascript:void(0)", "about:blank", "chrome://settings",
                    "//" + self.host, self.host + "/frontend", "/frontend"):
            with self.subTest(url=url):
                self.assertFalse(BR.valid_site_url(url))

    def test_all_userinfo_is_rejected_even_on_approved_host(self):
        for userinfo in ("user@", "user:password@", "@", ":@", "example.invalid@"):
            with self.subTest(userinfo=userinfo):
                self.assertFalse(BR.valid_site_url("https://" + userinfo + self.host + "/"))
        self.assertFalse(BR.valid_site_url(self.origin + "@example.invalid/"))

    def test_nondefault_or_malformed_ports_are_rejected(self):
        for port in ("80", "444", "8443", "0", "-1", "65536", "", "0443", "+443", "bad"):
            with self.subTest(port=port):
                self.assertFalse(BR.valid_site_url(self.origin + ":" + port + "/"))

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

    def test_lobby_route_only_adds_baccarat_keys_to_the_lobby_page(self):
        redirect = self.origin + BR.LOBBY_PATH + "/#category=all_games"
        self.assertEqual(BR.lobby_url_with_route(redirect), LOBBY_URL)
        self.assertEqual(BR.lobby_url_with_route(self.origin + BR.LOBBY_PATH + "/"),
                         self.origin + BR.LOBBY_PATH + "/#" + BR.LOBBY_ROUTE)
        self.assertEqual(BR.lobby_url_with_route(LOBBY_URL), LOBBY_URL)
        # Màn hình bàn đã mở: giữ nguyên table_id, không quay lại danh sách.
        table = self.origin + BR.LOBBY_PATH + "/#category=all_games&table_id=suropcmnbqpabwko"
        self.assertEqual(BR.lobby_url_with_route(table), table)
        for url in (BR.START_URL, "https://example.invalid/frontend/evo/r3/", "", None):
            self.assertEqual(BR.lobby_url_with_route(url), "", url)

    def test_start_guard_rejects_urls_before_any_browser_discovery_or_launch(self):
        # BrowserSession construction is pure; every external entry point is mocked.
        with patch.object(BR, "chrome_path") as discover, \
                patch.object(BR, "driver_for") as driver_for, \
                patch.object(BR.BrowserSession, "_start_driver") as launch, \
                patch.object(BR.BrowserSession, "alive") as alive, \
                patch.object(BR.BrowserSession, "stop") as stop:
            for url in ("file:///C:/not-a-page", "https://example.invalid/",
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

    def test_attach_requires_an_approved_page_not_an_iframe_or_unrelated_target(self):
        for pages in ([], [None, "invalid", {}, dict(self.page, type="iframe"),
                           dict(self.page, type="service_worker"),
                           dict(self.page, url="https://example.invalid/"),
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
        self.metadata["list"] = [dict(portal, url="https://example.invalid/")]
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
                                                         table_id="table_01:2"),
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
                        expression="(function(){\nreturn window.__BCR_SCAN();\n})(%s)" % json.dumps(BR.LOBBY_HOST_SUFFIX),
                        contextId=context_id, returnByValue=True, silent=True,
                        timeout=BR.SCRIPT_TIMEOUT * 1000))
                self.assertEqual(ws.recv.call_count, len(messages))
                for timeout in ws.settimeout.call_args_list:
                    self.assertGreater(timeout.args[0], 0)
                    self.assertLessEqual(timeout.args[0], BR.SCRIPT_TIMEOUT)

    def test_scan_rejects_changed_or_unapproved_root_before_reading(self):
        for changes in ({"url": "https://example.invalid/"}, {"type": "iframe"},
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

    def test_scan_discards_partial_reports_when_final_root_leaves_site_or_changes_target(self):
        for changes in ({"url": "https://example.invalid/?PRIVATE"}, {"type": "iframe"},
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
