# -*- coding: utf-8 -*-
"""Kiểm thử chiến thuật LONG-LONG (bcr/models/longlong.py) — thay hẳn Đua ngựa.

Luật bàn: hai bát, mỗi bát VÀNG (V) hoặc ĐỎ (D); một tay cho ra một bộ
VV / DD / VD / DV. Ô "1 vàng 1 đỏ" (VD hoặc DV) trả 0.95 : 1.

Luật đánh:
    - Tay trước ra VV hoặc DD  -> bỏ, chờ.
    - Tay trước ra VD hoặc DV  -> tay sau đặt ô "1 vàng 1 đỏ".
    - Thắng -> đánh tiếp ở vốn 1. Thua -> chờ bộ vàng-đỏ kế tiếp rồi gấp thếp.
"""

import unittest

from bcr.adapters import lobby_stream as LS
from bcr.adapters import longlong_table
from bcr.models import longlong


class LongLongPayoutTests(unittest.TestCase):
    """Bảng trả thưởng của sàn và ô đánh duy nhất."""

    def test_payout_table_matches_the_board(self):
        self.assertEqual(longlong.PAYOUT["M"], 0.95)     # 1 vàng 1 đỏ
        self.assertEqual(longlong.PAYOUT["VV"], 2.90)    # 2 vàng
        self.assertEqual(longlong.PAYOUT["DD"], 2.90)    # 2 đỏ
        self.assertEqual(longlong.DOOR, "M")
        self.assertEqual(longlong.payout_of(), 0.95)

    def test_the_mixed_door_carries_the_same_house_edge(self):
        # Hai bát độc lập 50/50: P(bộ vàng-đỏ) = 50% -> 0.50 × 0.95 − 0.50 = −2.5%.
        self.assertAlmostEqual(0.50 * longlong.PAYOUT["M"] - 0.50, -0.025)
        self.assertAlmostEqual(0.25 * longlong.PAYOUT["VV"] - 0.75, -0.025)

    def test_mixed_covers_both_orders(self):
        self.assertTrue(longlong.is_mixed("VD"))
        self.assertTrue(longlong.is_mixed("DV"))
        self.assertFalse(longlong.is_mixed("VV"))
        self.assertFalse(longlong.is_mixed("DD"))


class LongLongLadderTests(unittest.TestCase):
    def test_default_ladder_is_the_capital_allocation_with_the_default_multiplier(self):
        # Bảng vốn chốt: gốc 10, ×2, 9 tay -> 5.110 đv (5.110.000đ).
        self.assertEqual(longlong.DEFAULT_BASE, 10)
        self.assertEqual(longlong.DEFAULT_STEPS, 9)
        self.assertEqual(longlong.DEFAULT_CAPITAL, 5110)       # 5.110 đv = 5.110.000đ
        self.assertEqual(longlong.DEFAULT_LADDER_MULT, 2.0)
        self.assertEqual(longlong.DEFAULT_LADDER,
                         [10, 20, 40, 80, 160, 320, 640, 1280, 2560])
        self.assertEqual(longlong.DEFAULT_LADDER,
                         longlong.ladder_for_capital(longlong.DEFAULT_CAPITAL))
        self.assertEqual(longlong.ladder_total(longlong.DEFAULT_LADDER), 5110)

    def test_ladder_for_capital_uses_the_default_multiplier(self):
        # Hệ số mặc định là ×2: bảng vốn mặc định dùng đủ 9 tay.
        self.assertEqual(longlong.ladder_for_capital(5110),
                         [10, 20, 40, 80, 160, 320, 640, 1280, 2560])

    def test_every_rung_follows_the_multiplier(self):
        ladder = longlong.ladder_for_capital(5110, 10, 9, mult=2)
        self.assertLessEqual(longlong.ladder_total(ladder), 5110)
        for prev, cur in zip(ladder, ladder[1:]):
            self.assertEqual(cur, longlong.ceil_step(prev * 2, longlong.ROUND_STEP))

    def test_ladder_never_exceeds_the_capital(self):
        for capital in (25, 40, 50, 200, 1000, 5000, 5110, 10000, 12345):
            ladder = longlong.ladder_for_capital(capital)
            self.assertLessEqual(longlong.ladder_total(ladder), capital, capital)
            self.assertTrue(all(stake > 0 for stake in ladder), capital)

    def test_zero_capital_falls_back_to_the_full_ladder(self):
        self.assertEqual(longlong.ladder_for_capital(0, 10, 4), [10, 20, 40, 80])
        self.assertEqual(longlong.ladder_for_capital(0, 40, 4, mult=2), [40, 80, 160, 320])

    def test_multiply_ladder_builds_doubling_from_a_base(self):
        self.assertEqual(longlong.multiply_ladder(20, 4), [20, 40, 80, 160])
        self.assertEqual(longlong.multiply_ladder("50", 3), [50, 100, 200])
        self.assertEqual(longlong.multiply_ladder("x", 2), [10, 20])


class LongLongResultCodeTests(unittest.TestCase):
    """Đọc kết quả từ ô lịch sử: nhận bộ, nhãn chữ và mã số của sàn."""

    def test_accepts_pairs_and_lobby_labels(self):
        self.assertEqual(longlong.normalize_result("VD"), "VD")
        self.assertEqual(longlong.normalize_result("dv"), "DV")
        self.assertEqual(longlong.normalize_result("vàng vàng"), "VV")
        self.assertEqual(longlong.normalize_result("đỏ đỏ"), "DD")
        self.assertEqual(longlong.normalize_result("vàng đỏ"), "VD")
        self.assertEqual(longlong.normalize_result("đỏ vàng"), "DV")
        self.assertEqual(longlong.normalize_result("2 vàng"), "VV")
        self.assertEqual(longlong.normalize_result("1 vàng 1 đỏ"), "VD")

    def test_numeric_codes_follow_the_documented_order(self):
        self.assertEqual(longlong.normalize_result("1"), "VV")
        self.assertEqual(longlong.normalize_result("2"), "DD")
        self.assertEqual(longlong.normalize_result("3"), "VD")

    def test_a_single_die_is_not_enough_to_guess_the_pair(self):
        for value in ("V", "D", "vàng", "đỏ"):
            self.assertIsNone(longlong.normalize_result(value), value)
        self.assertEqual(longlong.die_of("V"), "V")
        self.assertEqual(longlong.die_of("đỏ"), "D")
        self.assertIsNone(longlong.die_of("vàng vàng"), "hai mặt là một bộ, không phải một con")

    def test_refuses_to_guess_on_unknown_symbols(self):
        for value in ("", None, "x", "hòa", 3.5):
            self.assertIsNone(longlong.normalize_result(value), value)

    def test_split_results_understands_separators_and_glued_pairs(self):
        self.assertEqual(longlong.split_results(["VV", "VD"]), ["VV", "VD"])
        self.assertEqual(longlong.split_results("VV|VD|DD"), ["VV", "VD", "DD"])
        self.assertEqual(longlong.split_results("VV,VD"), ["VV", "VD"])
        self.assertEqual(longlong.split_results("VV VD"), ["VV", "VD"])
        self.assertEqual(longlong.split_results("VVVD"), ["VV", "VD"])
        self.assertEqual(longlong.split_results(""), [])


class LongLongStrategyTests(unittest.TestCase):
    """Điều kiện vào tiền: chỉ đánh ô 1 vàng 1 đỏ, và chỉ sau một tay ra bộ vàng-đỏ."""

    def test_waits_while_the_previous_hand_was_two_gold_or_two_red(self):
        s = longlong.LongLongStrategy([40, 80, 160])
        self.assertEqual(s.plan()["action"], "wait")
        s.settle("VV")
        self.assertEqual(s.plan()["action"], "wait", "VV thì bỏ")
        s.settle("DD")
        self.assertEqual(s.plan()["action"], "wait", "DD thì bỏ")

    def test_bets_the_mixed_door_only_after_a_mixed_hand(self):
        s = longlong.LongLongStrategy([40, 80, 160])
        s.settle("VV")
        s.settle("VD")
        plan = s.plan()
        self.assertEqual(plan["action"], "bet")
        self.assertEqual(plan["side"], longlong.DOOR)
        self.assertEqual(plan["stake"], 40)

    def test_both_orders_of_the_mixed_hand_trigger(self):
        for trigger in ("VD", "DV"):
            with self.subTest(trigger=trigger):
                s = longlong.LongLongStrategy([40, 80])
                s.settle(trigger)
                self.assertEqual(s.plan()["action"], "bet")

    def test_win_returns_to_the_first_stake_and_keeps_betting(self):
        s = longlong.LongLongStrategy([40, 80, 160])
        s.settle("VD")
        self.assertEqual(s.arm()["stake"], 40)
        side, delta = s.settle("DV")            # thắng bộ vàng-đỏ
        self.assertEqual(side, "DV")
        self.assertAlmostEqual(delta, 40 * 0.95)
        self.assertEqual(s.step, 0)
        plan = s.plan()
        self.assertEqual(plan["action"], "bet", "thắng thì đánh tiếp tay sau")
        self.assertEqual(plan["stake"], 40, "và quay về vốn 1")

    def test_loss_waits_for_the_next_mixed_hand_then_doubles(self):
        s = longlong.LongLongStrategy([40, 80, 160])
        s.settle("VD")
        s.arm()
        side, delta = s.settle("VV")             # thua
        self.assertEqual(side, "VV")
        self.assertAlmostEqual(delta, -40)
        self.assertEqual(s.step, 1)
        self.assertEqual(s.plan()["action"], "wait", "thua xong phải chờ")
        s.settle("DD")
        self.assertEqual(s.plan()["action"], "wait", "DD vẫn không phải bộ vàng-đỏ")
        s.settle("DV")
        plan = s.plan()
        self.assertEqual(plan["action"], "bet")
        self.assertEqual(plan["stake"], 80, "gấp thếp đúng bậc đã cấu hình")

    def test_both_orders_of_the_loss_wait_for_the_next_trigger(self):
        for loss in ("VV", "DD"):
            with self.subTest(loss=loss):
                s = longlong.LongLongStrategy([40, 80])
                s.settle("VD")
                s.arm()
                s.settle(loss)
                self.assertEqual(s.plan()["action"], "wait")
                s.settle("VD")
                self.assertEqual(s.plan()["stake"], 80)

    def test_losing_at_the_last_step_stops_the_session(self):
        s = longlong.LongLongStrategy([40, 80])
        s.settle("VD")
        s.arm()
        s.settle("VV")                     # thua bậc 1 -> lên bậc 2
        self.assertEqual(s.step, 1)
        self.assertFalse(s.stopped)
        s.settle("VD")
        self.assertEqual(s.arm()["stake"], 80, "đánh bậc cuối")
        s.settle("DD")                     # thua ngay bậc cuối -> chạm đáy thang
        self.assertTrue(s.stopped)
        self.assertEqual(s.step, s.bottom)
        self.assertIn("DỪNG PHIÊN", s.note)

    def test_stopped_session_never_bets_again(self):
        s = longlong.LongLongStrategy([40])
        s.settle("VD")
        s.arm()
        s.settle("VV")                     # thua ở bậc duy nhất = bậc cuối
        self.assertTrue(s.stopped)
        self.assertEqual(s.plan()["action"], "stop")
        s.settle("VD")                     # có bộ vàng-đỏ cũng không đánh nữa
        self.assertEqual(s.plan()["action"], "stop")
        self.assertIsNone(s.arm())
        self.assertEqual(len(s.log), 1)

    def test_reset_restarts_a_session_that_hit_the_bottom(self):
        s = longlong.LongLongStrategy([40])
        s.settle("VD")
        s.arm()
        s.settle("VV")
        self.assertTrue(s.stopped)
        s.reset()
        self.assertFalse(s.stopped)
        self.assertEqual(s.step, 0)
        s.settle("VD")
        self.assertEqual(s.plan()["action"], "bet")

    def test_winning_at_the_last_step_does_not_stop_the_session(self):
        s = longlong.LongLongStrategy([40, 80])
        s.settle("VD")
        s.arm()
        s.settle("VV")
        s.settle("VD")
        s.arm()
        s.settle("DV")                     # thắng ở bậc cuối
        self.assertFalse(s.stopped)
        self.assertEqual(s.step, 0)

    def test_losing_the_whole_ladder_costs_exactly_the_ladder_total(self):
        """Thua hết thang = mất đúng tổng các bậc; phần vốn chưa dùng còn nguyên."""
        sim = longlong.LongLongSimulation(longlong.DEFAULT_LADDER, capital=10_000_000)
        hands = []
        for _ in range(len(longlong.DEFAULT_LADDER)):
            hands += ["VD", "VV"]          # bộ vàng-đỏ kích hoạt -> tay sau thua
        sim.ingest(hands)
        spent = longlong.ladder_total(longlong.DEFAULT_LADDER) * 1000.0
        self.assertTrue(sim.strategy.stopped)
        self.assertAlmostEqual(sim.balance, 10_000_000.0 - spent)
        self.assertAlmostEqual(sim.balance - sim.start_capital, -spent)
        self.assertEqual(len(sim.log), len(longlong.DEFAULT_LADDER))

    def test_matches_the_walkthrough_from_the_rule_book(self):
        """VV bỏ · VD -> đánh 40 · DV thắng · đánh tiếp 40 · VV thua · DD bỏ · VD -> 80."""
        s = longlong.LongLongStrategy([40, 80, 160])
        bets = []
        results = ["VV", "VD", "DV", "VV", "DD", "VD", "DV"]
        for index, result in enumerate(results, start=1):
            plan = s.plan()
            if plan["action"] == "bet":
                bets.append((index, plan["stake"]))
                s.arm()
            s.settle(result)
        self.assertEqual(bets, [(3, 40), (4, 40), (7, 80)])
        self.assertEqual(s.step, 0, "tay 7 thắng nên về vốn 1")

    def test_ignores_unknown_symbols(self):
        s = longlong.LongLongStrategy([40])
        self.assertEqual(s.settle("x"), (None, 0))
        self.assertEqual(s.results, [])

    def test_reset_clears_pending_and_step(self):
        s = longlong.LongLongStrategy([40, 80])
        s.settle("VD")
        s.arm()
        s.reset()
        self.assertIsNone(s.pending)
        self.assertEqual(s.step, 0)


class LongLongLobbyProfileTests(unittest.TestCase):
    """Sảnh tách theo trò: Baccarat giữ nguyên hành vi cũ, LONG-LONG có hash riêng."""

    def test_game_profile_fragments(self):
        self.assertEqual(LS.game_profile("baccarat")["fragment"], LS.LOBBY_LIST_FRAGMENT)
        profile = LS.game_profile("longlong")
        self.assertIn("category=instant_games", profile["fragment"])
        self.assertIn("game=rng-longlong", profile["fragment"])
        self.assertEqual(LS.game_profile("khác")["key"], "baccarat")

    def test_lobby_list_url_uses_the_longlong_fragment_when_none_kept(self):
        url = "https://host/frontend/evo/r3/#table_id=t1"
        self.assertEqual(
            LS.lobby_list_url(url, fragment=LS.game_profile("longlong")["fragment"]),
            "https://host/frontend/evo/r3/#category=instant_games&game=rng-longlong")

    def test_longlong_fragment_keeps_existing_view(self):
        url = "https://host/frontend/evo/r3/#category=instant_games&game=rng-longlong&table_id=t1"
        self.assertEqual(LS.lobby_list_url(url),
                         "https://host/frontend/evo/r3/#category=instant_games&game=rng-longlong")

    def test_longlong_profile_marks_longlong_tables_only(self):
        session = type("S", (), {"_cdp_page": lambda self: None})()
        stream = LS.LobbyStream(session=session, game="longlong")
        self.assertEqual(stream.game, "longlong")
        self.assertIn("rng-longlong", stream.profile["game_types"])


class LongLongDecodeTests(unittest.TestCase):
    """Đọc kết quả LONG-LONG từ [data-role="history-grid"]."""

    def test_picks_the_longest_grid_of_pairs(self):
        self.assertEqual(LS.decode_longlong_results([["VV", "DD"], ["VV", "VD", "DV", "DD"]]),
                         (["VV", "VD", "DV", "DD"], ""))

    def test_reads_lobby_labels_as_pairs(self):
        self.assertEqual(
            LS.decode_longlong_results([["vàng vàng", "đỏ đỏ", "vàng đỏ"]]),
            (["VV", "DD", "VD"], ""))

    def test_pairs_two_single_dice_cells_into_one_hand(self):
        self.assertEqual(LS.single_grid_results(["V", "V", "D", "D"]), (["VV", "DD"], ""))
        self.assertEqual(LS.single_grid_results(["V", "D"]), (["VD"], ""))
        self.assertEqual(LS.single_grid_results(["V", "V", "D"]), (["VV"], ""),
                         "tay đang chơi chưa đủ hai bát thì bỏ")

    def test_rejects_unknown_symbols_instead_of_guessing(self):
        results, error = LS.decode_longlong_results([["VV", "x", "DD"]])
        self.assertEqual(results, [])
        self.assertTrue(error, "báo lỗi thay vì đoán")
        self.assertEqual(LS.decode_longlong_results([])[0], [])
        self.assertEqual(LS.decode_longlong_results([["VV", "DD"]]), (["VV", "DD"], ""))


class LongLongSimulationTests(unittest.TestCase):
    """Nạp dần kết quả live và giữ sổ vốn riêng theo đơn vị sảnh."""

    def test_ingest_only_processes_new_hands(self):
        sim = longlong.LongLongSimulation([40, 80, 160], capital=1_000_000)
        self.assertEqual(sim.ingest(["VD", "DV"]), 2)
        self.assertEqual(sim.ingest(["VD", "DV", "VV"]), 1)
        self.assertEqual(sim.ingest(["VD", "DV", "VV"]), 0)

    def test_ingest_accepts_a_glued_string_from_the_reader(self):
        sim = longlong.LongLongSimulation([40, 80], capital=0)
        self.assertEqual(sim.ingest("VD|DV"), 2)

    def test_win_and_loss_move_the_balance_in_lobby_units(self):
        sim = longlong.LongLongSimulation([40, 80], capital=1_000_000)
        sim.ingest(["VD", "DV"])            # tay 1 chờ, tay 2 cược 40 và thắng
        self.assertEqual(len(sim.log), 1)
        self.assertAlmostEqual(sim.profit, 40 * 0.95 * 1000)
        sim.ingest(["VD", "DV", "VV"])      # tay 3 cược 40 và thua
        self.assertEqual(sim.strategy.step, 1)
        self.assertAlmostEqual(sim.profit, 40 * 0.95 * 1000 - 40 * 1000)

    def test_append_records_a_manual_hand_and_keeps_the_book(self):
        sim = longlong.LongLongSimulation([40, 80], capital=0)
        self.assertEqual(sim.append("VD"), ("VD", 0))   # chỉ ghi lịch sử, chưa cược
        self.assertEqual(sim.log, [])
        self.assertEqual(sim.strategy.arm()["stake"], 40)
        self.assertAlmostEqual(sim.append("DV")[1], 40 * 0.95)
        self.assertEqual(len(sim.log), 1)
        self.assertAlmostEqual(sim.balance, 40 * 0.95 * 1000)
        self.assertIsNone(sim.append("x")[0], "kết quả lạ thì không ghi")

    def test_reset_then_reading_the_same_history_does_not_double_count(self):
        sim = longlong.LongLongSimulation([40, 80], capital=0)
        sim.ingest(["VD", "DV", "VD", "DV"])
        bets = len(sim.log)
        sim.reset()
        self.assertEqual(sim.log, [])
        self.assertFalse(sim.strategy.stopped)
        sim.ingest(["VD", "DV", "VD", "DV"])
        self.assertEqual(len(sim.log), bets, "phiên mới tính sổ lại từ đầu, không cộng trùng")

    def test_history_reset_restarts_cleanly(self):
        sim = longlong.LongLongSimulation([40, 80], capital=0)
        sim.ingest(["VD", "DV"])
        sim.ingest(["VV", "DD"])            # lịch sử khác hẳn -> bắt đầu lại
        self.assertEqual(len(sim.log), 1)
        self.assertAlmostEqual(sim.balance, 40 * 0.95 * 1000)


class LongLongStatsTests(unittest.TestCase):
    """Thống kê hiệu quả (tab Thống kê): tỉ lệ thắng/thua + chuỗi thua dài nhất."""

    def sample(self):
        return [
            {"won": True, "delta": 38},
            {"won": False, "delta": -40},
            {"won": False, "delta": -80},
            {"won": False, "delta": -160},
            {"won": True, "delta": 38},
            {"won": False, "delta": -40},
            {"won": False, "delta": -80},
        ]

    def test_empty_history_is_all_zero(self):
        stats = longlong.summarize_history([])
        self.assertEqual((stats["hands"], stats["wins"], stats["losses"]), (0, 0, 0))
        self.assertEqual(stats["win_rate"], 0.0)
        self.assertEqual(stats["max_loss_streak"], 0)
        self.assertEqual(stats["streak_histogram"], {})
        self.assertEqual(stats["curve"], [])

    def test_counts_ratio_and_longest_losing_run(self):
        stats = longlong.summarize_history(self.sample())
        self.assertEqual((stats["hands"], stats["wins"], stats["losses"]), (7, 2, 5))
        self.assertEqual(stats["win_rate"], 28.6)
        self.assertEqual(stats["loss_rate"], 71.4)
        self.assertEqual(stats["max_loss_streak"], 3)      # L-L-L ở giữa sổ
        self.assertEqual(stats["current_loss_streak"], 2)  # cuối sổ đang thua 2 tay
        self.assertEqual(stats["max_win_streak"], 1)
        self.assertEqual(stats["worst_loss"], -160)
        self.assertEqual(stats["net_units"], -324)
        self.assertEqual(stats["streak_histogram"], {"2": 1, "3": 1})
        self.assertEqual(stats["recent"], ["W", "L", "L", "L", "W", "L", "L"])

    def test_curve_is_cumulative_and_matches_net(self):
        stats = longlong.summarize_history(self.sample())
        self.assertEqual(stats["curve"], [38, -2, -82, -242, -204, -244, -324])
        self.assertEqual(stats["curve"][-1], stats["net_units"])

    def test_recent_is_capped(self):
        entries = [{"won": index % 2 == 0, "delta": 1} for index in range(60)]
        stats = longlong.summarize_history(entries, recent=10)
        self.assertEqual(len(stats["recent"]), 10)
        self.assertEqual(stats["hands"], 60)

    def test_session_ledger_feeds_the_stats(self):
        sim = longlong.LongLongSimulation([40], capital=0)
        sim.ingest(["VD", "VV", "VD", "VD"], auto_arm=True)
        stats = longlong.summarize_history(sim.log)
        self.assertEqual(stats["hands"], len(sim.log))
        self.assertEqual(stats["hands"], stats["wins"] + stats["losses"])
        self.assertGreaterEqual(stats["max_loss_streak"], 1)


class LongLongUnitModeTests(unittest.TestCase):
    """Đơn vị tiền trên sàn: VN2 (1 đv = 1 số) hay VND thật (10 đv = 10.000).

    Ca thật: sàn có khay chip [10.000, 20.000, 40.000, 100.000, 500.000, 2.000.000].
    Bậc 10 đv ở chế độ VN2 gửi "10" nên KHÔNG chia được (lỗi người dùng gặp), còn ở
    chế độ VND gửi "10.000" nên bấm đúng 1 chip.
    """

    REAL_TRAY = [10000, 20000, 40000, 100000, 500000, 2000000]

    def test_vn2_mode_keeps_the_board_scale(self):
        self.assertEqual(longlong.to_site_amount(40, longlong.UNIT_VN2), 40)
        self.assertEqual(longlong.from_site_amount(40, longlong.UNIT_VN2), 40.0)
        self.assertEqual(longlong.site_scale(longlong.UNIT_VN2), 1000.0)

    def test_vnd_mode_sends_real_money(self):
        self.assertEqual(longlong.to_site_amount(10, longlong.UNIT_VND), 10000)
        self.assertEqual(longlong.to_site_amount(4920, longlong.UNIT_VND), 4920000)
        self.assertEqual(longlong.from_site_amount(250000, longlong.UNIT_VND), 250.0)
        self.assertEqual(longlong.site_scale(longlong.UNIT_VND), 1.0)

    def test_default_and_unknown_modes_fall_back_to_vn2(self):
        self.assertEqual(longlong.DEFAULT_UNIT_MODE, longlong.UNIT_VN2)
        for value in (None, "", "gì đó", "usd"):
            self.assertEqual(longlong.normalize_unit_mode(value), longlong.UNIT_VN2, value)
        for value in ("vnd", "VND", "Vnd", "đ"):
            self.assertEqual(longlong.normalize_unit_mode(value), longlong.UNIT_VND, value)

    def test_ten_thousand_only_splits_in_vnd_mode(self):
        self.assertEqual(
            longlong_table.decompose_amount(
                longlong.to_site_amount(10, longlong.UNIT_VND), self.REAL_TRAY), [10000])
        self.assertIsNone(
            longlong_table.decompose_amount(
                longlong.to_site_amount(10, longlong.UNIT_VN2), self.REAL_TRAY),
            "chế độ VN2 gửi 10 nên khay toàn mệnh giá lớn không chia được")

    def test_bad_amount_is_not_rounded_into_a_bigger_bet(self):
        self.assertEqual(longlong.to_site_amount("x", longlong.UNIT_VND), 0)
        self.assertEqual(longlong.to_site_amount(None, longlong.UNIT_VND), 0)
        self.assertEqual(longlong.from_site_amount("x", longlong.UNIT_VND), 0.0)


class LadderFromCapitalTests(unittest.TestCase):
    """Chia vốn theo hệ số nhân: chỉ thêm bậc khi tổng còn trong vốn.

    Bảng vốn mặc định của bàn: gốc 10, ×2, làm tròn lên bội 10 →
    10 · 20 · 40 · 80 · 160 · 320 · 640 · 1.280 · 2.560 (tổng 5.110 đv).
    """

    BOARD = [10, 20, 40, 80, 160, 320, 640, 1280, 2560]

    def test_matches_the_real_allocation_table(self):
        self.assertEqual(longlong.ladder_for_capital(5110, base=10, mult=2),
                         self.BOARD)
        self.assertEqual(sum(self.BOARD), 5110)

    def test_stops_when_the_next_rung_would_exceed_the_capital(self):
        ladder = longlong.ladder_for_capital(5110, base=10, mult=2)
        self.assertEqual(len(ladder), 9)
        self.assertLessEqual(sum(ladder), 5110)
        next_rung = longlong.ceil_step(ladder[-1] * 2, 10)
        self.assertGreater(sum(ladder) + next_rung, 5110)

    def test_rounds_each_rung_up_to_the_step(self):
        self.assertEqual(longlong.ceil_step(22.5, 10), 30)
        self.assertEqual(longlong.ceil_step(472.5, 10), 480)
        self.assertEqual(longlong.ceil_step(1080, 10), 1080)
        self.assertEqual(longlong.ceil_step(5467.5, 10), 5470)

    def test_multiply_ladder_uses_the_multiplier(self):
        self.assertEqual(longlong.multiply_ladder(10, 4, 2, 10), [10, 20, 40, 80])
        self.assertEqual(longlong.multiply_ladder(40, 4, 2), [40, 80, 160, 320])

    def test_small_capital_keeps_one_affordable_rung(self):
        self.assertEqual(longlong.ladder_for_capital(5, base=10, mult=2), [5])
        self.assertEqual(longlong.ladder_for_capital(3000, base=10, mult=2),
                         [10, 20, 40, 80, 160, 320, 640, 1280])

    def test_bad_multiplier_falls_back_to_the_default(self):
        for value in (None, 0, 1, 99, "x"):
            self.assertEqual(longlong.normalize_mult(value), longlong.DEFAULT_LADDER_MULT, value)
        self.assertEqual(longlong.normalize_mult("2,5"), 2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
