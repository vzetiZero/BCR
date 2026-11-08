# -*- coding: utf-8 -*-
"""Kiểm thử chiến thuật Đua ngựa (race.py) — độc lập hoàn toàn với Baccarat."""

import unittest

import race


class RaceStrategyTests(unittest.TestCase):
    def test_default_ladder_uses_lobby_units_starting_at_twenty(self):
        self.assertEqual(race.DEFAULT_BASE, 20)
        self.assertEqual(race.DEFAULT_LADDER[0], 20)
        self.assertEqual(len(race.DEFAULT_LADDER), 12)
        for i in range(1, 12):
            self.assertEqual(race.DEFAULT_LADDER[i], race.DEFAULT_LADDER[i - 1] * 2)
        self.assertEqual(race.DEFAULT_LADDER, race.multiply_ladder(20, 12))

    def test_multiply_ladder_builds_doubling_from_a_base(self):
        self.assertEqual(race.multiply_ladder(20, 4), [20, 40, 80, 160])
        self.assertEqual(race.multiply_ladder("50", 3), [50, 100, 200])
        self.assertEqual(race.multiply_ladder("x", 2), [20, 40])

    def test_only_bets_the_hand_after_a_one(self):
        s = race.RaceStrategy([100_000, 200_000, 400_000])
        self.assertEqual(s.plan()["action"], "wait")
        s.settle("2")
        self.assertEqual(s.plan()["action"], "wait", "cửa 2 không kích hoạt")
        s.settle("1")
        plan = s.plan()
        self.assertEqual(plan["action"], "bet")
        self.assertEqual(plan["side"], "1")
        self.assertEqual(plan["stake"], 100_000)

    def test_loss_advances_the_ladder_and_win_resets_it(self):
        s = race.RaceStrategy([100_000, 200_000, 400_000])
        s.settle("1")                 # tay 1 = cửa 1 -> kích hoạt
        self.assertEqual(s.arm()["stake"], 100_000)
        s.settle("3")                 # thua
        self.assertEqual(s.step, 1)
        s.settle("1")                 # lại cửa 1 -> kích hoạt
        self.assertEqual(s.arm()["stake"], 200_000)
        s.settle("1")                 # thắng
        self.assertEqual(s.step, 0)
        s.settle("1")
        self.assertEqual(s.arm()["stake"], 100_000, "thắng xong về mức gốc")

    def test_matches_the_walkthrough_example(self):
        s = race.RaceStrategy([100_000, 200_000, 400_000])
        stakes = []
        for result in ["2", "3", "2", "1", "3", "2", "1", "1", "3", "1", "1"]:
            plan = s.plan()
            if plan["action"] == "bet":
                stakes.append((result, plan["stake"]))
                s.arm()
            s.settle(result)
        self.assertEqual(stakes, [("3", 100_000), ("1", 200_000), ("1", 100_000)])

    def test_ladder_is_clamped_and_bad_input_falls_back(self):
        self.assertEqual(race.normalize_ladder(["1.000", "2.000"]), [1000, 2000])
        self.assertEqual(race.normalize_ladder(["abc"]), race.DEFAULT_LADDER)
        self.assertEqual(race.normalize_ladder([]), race.DEFAULT_LADDER)

    def test_unknown_result_is_ignored(self):
        s = race.RaceStrategy([100_000])
        self.assertEqual(s.settle("9"), (None, 0))
        self.assertEqual(s.results, [])

    def test_reset_clears_pending_and_step(self):
        s = race.RaceStrategy([100_000, 200_000])
        s.settle("1")
        s.arm()
        s.reset()
        self.assertIsNone(s.pending)
        self.assertEqual(s.step, 0)


class RaceLobbyProfileTests(unittest.TestCase):
    """Bước 1: sảnh tách theo trò; Baccarat giữ nguyên hành vi cũ."""

    def test_game_profile_fragments(self):
        import lobby_stream as LS
        self.assertEqual(LS.game_profile("baccarat")["fragment"], LS.LOBBY_LIST_FRAGMENT)
        race = LS.game_profile("race")
        self.assertIn("category=instant_games", race["fragment"])
        self.assertIn("game=rng-racetrack", race["fragment"])
        self.assertEqual(LS.game_profile("khác")["key"], "baccarat")

    def test_lobby_list_url_uses_the_race_fragment_when_none_kept(self):
        import lobby_stream as LS
        url = "https://host/frontend/evo/r3/#table_id=t1"
        self.assertEqual(LS.lobby_list_url(url, fragment=LS.game_profile("race")["fragment"]),
                         "https://host/frontend/evo/r3/#category=instant_games&game=rng-racetrack")

    def test_race_fragment_keeps_existing_view(self):
        import lobby_stream as LS
        url = "https://host/frontend/evo/r3/#category=instant_games&game=rng-racetrack&table_id=t1"
        self.assertEqual(LS.lobby_list_url(url),
                         "https://host/frontend/evo/r3/#category=instant_games&game=rng-racetrack")


class RaceDecodeTests(unittest.TestCase):
    """Bước 2: đọc kết quả Đua ngựa từ các dãy lịch sử."""

    def test_concatenates_and_picks_the_longest_grid(self):
        import lobby_stream as LS
        self.assertEqual(LS.decode_race_results([["1", "2"], ["1", "2", "1", "3"]]),
                         ("1213", ""))
        self.assertEqual(LS.single_grid_results(["1", "1", "2"]), ("112", ""))

    def test_rejects_unknown_symbols_instead_of_guessing(self):
        import lobby_stream as LS
        self.assertEqual(LS.decode_race_results([["1", "x", "3"]])[0], "")
        self.assertEqual(LS.decode_race_results([])[0], "")

    def test_race_profile_marks_race_tables_only(self):
        import lobby_stream as LS
        LobbyStream = LS.LobbyStream
        session = type("S", (), {"_cdp_page": lambda self: None})()
        stream = LobbyStream(session=session, game="race")
        self.assertEqual(stream.game, "race")
        self.assertIn("rng-racetrack", stream.profile["game_types"])


class RaceSimulationTests(unittest.TestCase):
    """Bước 3+5: nạp dần kết quả live và giữ sổ vốn riêng theo đơn vị sảnh."""

    def test_ingest_only_processes_new_hands(self):
        import race as R
        sim = R.RaceSimulation([20, 40, 80], capital=1_000_000)
        self.assertEqual(sim.ingest("121"), 3)
        self.assertEqual(sim.ingest("1213"), 1)
        self.assertEqual(sim.ingest("1213"), 0)

    def test_win_and_loss_move_the_balance_in_lobby_units(self):
        import race as R
        sim = R.RaceSimulation([20, 40, 80], capital=1_000_000)
        # 1 -> kích hoạt, tay sau cược 20 vào cửa 1 và thua (ra 3)
        sim.ingest("13")
        self.assertEqual(sim.strategy.step, 1)
        self.assertEqual(len(sim.log), 1)
        self.assertEqual(sim.profit, -20_000)
        # 1 kích hoạt lại, cược 40, thắng (ra 1) -> về bậc 0
        sim.ingest("1311")
        self.assertEqual(sim.strategy.step, 0)
        self.assertEqual(sim.profit, -20_000 + 40_000)

    def test_history_reset_restarts_cleanly(self):
        import race as R
        sim = R.RaceSimulation([20, 40], capital=0)
        sim.ingest("131")
        sim.ingest("222")   # lịch sử khác hẳn -> bắt đầu lại, không tính lại tay cũ
        self.assertEqual(len(sim.log), 1)
        self.assertEqual(sim.balance, -20_000)


class RaceLadderMigrationTests(unittest.TestCase):
    """State cũ ghi thang bằng đồng (2.000...) phải hạ 1000 về đơn vị sảnh (2...)."""

    def test_old_currency_ladder_is_divided_by_one_thousand(self):
        import types
        from unittest.mock import Mock
        import app

        vars_ = [Mock(set=Mock()) for _ in range(12)]
        host = types.SimpleNamespace(
            race_ladder_vars=vars_, race_base_var=Mock(set=Mock()),
            _race_saved={"base": "20",
                         "ladder": [2000, 4000, 8000, 16000, 32000, 64000,
                                    128000, 256000, 512000, 1024000, 2048000, 4096000]})
        app.BCRApp._apply_saved_race_ladder(host)
        written = [call.args[0] for call in vars_[0].set.call_args_list]
        self.assertEqual(written[-1], "2")
        written_last = [v.set.call_args_list[-1].args[0] for v in vars_]
        self.assertEqual(written_last, ["2", "4", "8", "16", "32", "64", "128",
                                        "256", "512", "1024", "2048", "4096"])

    def test_lobby_unit_ladder_is_kept_as_is(self):
        import types
        from unittest.mock import Mock
        import app

        vars_ = [Mock(set=Mock()) for _ in range(3)]
        host = types.SimpleNamespace(
            race_ladder_vars=vars_, race_base_var=Mock(set=Mock()),
            _race_saved={"base": "20", "ladder": [20, 40, 80]})
        app.BCRApp._apply_saved_race_ladder(host)
        written = [v.set.call_args_list[-1].args[0] for v in vars_]
        self.assertEqual(written, ["20", "40", "80"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
