# -*- coding: utf-8 -*-
"""Kiểm thử bộ đọc luồng WebSocket của bàn LONG-LONG (game twocoins).

Dữ liệu dưới đây là message THẬT bắt được trên bàn DragonDragon0001 (Evolution), nên
test khoá luôn định dạng mà sàn đang dùng.
"""

import unittest

from bcr.adapters import longlong_stream as S


def state_msg(phase, game_id="18e81b44", **extra):
    args = {"gameId": game_id, "number": "03:45:03", "phase": dict({"type": phase}, **extra),
            "chips": {}}
    return {"id": "1-2", "type": "twocoins.state", "args": args, "time": 1794714231015}


class CoinsTests(unittest.TestCase):
    def test_maps_the_two_coins_to_the_app_result(self):
        self.assertEqual(S.coins_to_raw(["Gold", "Gold"]), "VV")
        self.assertEqual(S.coins_to_raw(["Red", "Red"]), "DD")
        self.assertEqual(S.coins_to_raw(["Gold", "Red"]), "VD")
        self.assertEqual(S.coins_to_raw(["Red", "Gold"]), "DV")

    def test_does_not_guess_on_unknown_coins(self):
        for value in (None, [], ["Gold"], ["Blue", "Gold"], "Gold"):
            self.assertIsNone(S.coins_to_raw(value), value)


class ParseMessageTests(unittest.TestCase):
    def test_bets_open_carries_round_and_remaining_time(self):
        snap = S.parse_message(state_msg("BetsOpen", timeRemaining=14986, history=[],
                                         previousChips={"JNS_C": 80000}))
        self.assertEqual(snap["round"], "18e81b44")
        self.assertEqual(snap["phase"], "BetsOpen")
        self.assertEqual(snap["time_remaining"], 14986)
        self.assertEqual(snap["previous_chips"], {"JNS_C": 80000})
        self.assertIsInstance(snap["received_at"], float)

    def test_game_resolved_carries_the_result(self):
        snap = S.parse_message(state_msg("GameResolved", coins=["Gold", "Red"]))
        self.assertEqual(snap["phase"], "GameResolved")
        self.assertEqual(snap["result"], "VD")
        self.assertEqual(snap["coins"], ["Gold", "Red"])

    def test_intermediate_phases_have_no_result(self):
        for phase in ("BetsClosedAnnounced", "BetsClosed", "BowlOneShook", "BowlTwoShook",
                      "CoinOneReveal", "CoinTwoReveal", "CoinsRevealed"):
            snap = S.parse_message(state_msg(phase))
            self.assertEqual(snap["phase"], phase, phase)
            self.assertIsNone(snap["result"], phase)

    def test_coin_statistics_gives_the_history(self):
        msg = {"type": "twocoins.coinStatistics",
               "args": {"lastResults": [{"coins": ["Gold", "Gold"]},
                                        {"coins": ["Red", "Red"]}]}}
        self.assertEqual(S.parse_message(msg)["history"], ["VV", "DD"])

    def test_ignores_unrelated_messages(self):
        for obj in (None, {}, {"type": "metrics.ping", "args": {}},
                    {"type": "twocoins.state", "args": {"phase": {"type": "Khac"}}},
                    {"type": "twocoins.playerBetStats", "args": {}}):
            self.assertIsNone(S.parse_message(obj), obj)


class StreamSnapshotTests(unittest.TestCase):
    """Ảnh chụp giữ kết quả gần nhất và tự đếm lùi thời gian còn lại."""

    def test_counts_down_and_keeps_the_last_result(self):
        stream = S.LongLongStream()
        stream._update(S.parse_message(state_msg("GameResolved", game_id="r1",
                                                 coins=["Red", "Red"])))
        stream._update(S.parse_message(state_msg("BetsOpen", game_id="r2",
                                                 timeRemaining=15000, previousChips={})))
        snap = stream.snapshot()
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["round"], "r2")
        self.assertEqual(snap["phase"], "BetsOpen")
        self.assertEqual(snap["result"], "DD")          # kết quả ván trước vẫn còn
        self.assertEqual(snap["result_round"], "r1")
        self.assertLessEqual(snap["remaining_ms"], 15000)
        self.assertGreater(snap["remaining_ms"], 0)
        self.assertIsInstance(snap["age_ms"], int)

    def test_empty_snapshot_is_not_ok(self):
        self.assertFalse(S.LongLongStream().snapshot()["ok"])

    def test_thread_survives_a_closed_chrome(self):
        from bcr.adapters import browser as BR
        stream = S.LongLongStream()
        original = BR.BrowserSession._cdp_json
        BR.BrowserSession._cdp_json = classmethod(
            lambda cls, resource, **kw: (_ for _ in ()).throw(OSError("refused")))
        try:
            stream.start()
            for _ in range(30):
                import time
                time.sleep(0.02)
                if stream.last_error:
                    break
            self.assertTrue(stream.running, "cổng CDP tắt thì thread phải còn sống để thử lại")
            self.assertFalse(stream.snapshot()["ok"])
            self.assertTrue(stream.last_error)
        finally:
            stream.stop()
            BR.BrowserSession._cdp_json = original
