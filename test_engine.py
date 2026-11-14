# -*- coding: utf-8 -*-
"""Kiểm thử engine JSON của Electron (bcr/service/engine.py).

Không cần Chrome: bàn được thay bằng bàn giả, Chrome bằng đối tượng giả. Mục tiêu là
khoá lại hợp đồng giữa UI Electron và tiến trình Python:

    - mỗi trò một service riêng, dispatch đúng method
    - một ván chỉ chốt một lần (kể cả khi sàn giữ ô thắng nhiều nhịp)
    - đặt chip thật chỉ chạy khi bật, và chỉ chốt sổ sau khi chip vào
    - cấu hình được lưu ra file riêng của từng trò
"""

import json
import pathlib
import tempfile
import unittest

from bcr.service import engine
from bcr.models import longlong


class FakeTable:
    """Bàn giả: trả lần lượt các state, ghi lại lệnh đặt chip."""

    def __init__(self, states=None):
        self.states = list(states or [])
        self.places = []
        self.closed = False
        self.place_result = {"ok": True, "placed": [40], "total": 40.0}

    def read(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return dict(state)

    def place(self, amount, door=None, **kwargs):
        self.places.append(amount)
        return dict(self.place_result)

    def close(self):
        self.closed = True


class FakeEmitter:
    def __init__(self):
        self.events = []

    def event(self, name, data):
        self.events.append((name, data))

    def reply(self, request_id, result=None, error=None):
        self.events.append(("reply", {"id": request_id, "result": result, "error": error}))

    def send(self, payload):
        self.events.append(("send", payload))


def state(winner=None, raw=None, balance=1000.0, at=1000.0):
    return {"ok": True, "winner": winner, "winner_raw": raw, "balance_vn2": balance,
            "total_bet_vn2": 0.0, "status": "ĐẶT CƯỢC CỦA BẠN", "doors": {}, "read_at": at}


def make_service(states=None, config=None, table=None):
    folder = pathlib.Path(tempfile.mkdtemp())
    service = engine.LongLongService(folder / "state.json", FakeEmitter())
    service.table = table or FakeTable(states or [state()])
    service.browser = type("FakeBrowser", (), {"connected": True, "note": "", "mode": "cdp",
                                               "stop": lambda self: None})()
    if config:
        service.config.update(config)
        service.sim.set_ladder(config["ladder"]) if "ladder" in config else None
    return service


class DispatchTests(unittest.TestCase):
    def test_ping_and_state_are_answered(self):
        service = make_service()
        result, error = engine.dispatch(service, "ping", {})
        self.assertIsNone(error)
        self.assertTrue(result["pong"])
        snapshot, error = engine.dispatch(service, "state", {})
        self.assertIsNone(error)
        self.assertEqual(snapshot["game"], "longlong")
        self.assertEqual(snapshot["ladder"], longlong.DEFAULT_LADDER)
        self.assertFalse(snapshot["tool_running"])

    def test_unknown_method_reports_an_error(self):
        service = make_service()
        result, error = engine.dispatch(service, "khong.co", {})
        self.assertIsNone(result)
        self.assertIn("không hỗ trợ", error)

    def test_both_games_have_their_own_service(self):
        emit = FakeEmitter()
        baccarat = engine.build_service("baccarat", emit, pathlib.Path(tempfile.mkdtemp()) / "b.json")
        longlong_service = engine.build_service("longlong", emit, pathlib.Path(tempfile.mkdtemp()) / "l.json")
        self.assertIsInstance(baccarat, engine.BaccaratService)
        self.assertIsInstance(longlong_service, engine.LongLongService)
        self.assertIsNot(type(baccarat), type(longlong_service))


class TickTests(unittest.TestCase):
    def test_one_hand_per_round_even_if_winner_repeats(self):
        table = FakeTable([state("JNS_C", "VD", at=1000.0),
                           state("JNS_C", "VD", at=1001.0),
                           state("JNS_C", "VD", at=1002.0)])
        service = make_service(table=table)
        for _ in range(3):
            service.tick()
        self.assertEqual(service.tracker.hands, 1)
        self.assertEqual(service.sim.strategy.results, ["VD"])

    def test_new_winner_counts_as_a_new_hand(self):
        table = FakeTable([state("JNS_C", "VD", at=1000.0),
                           state("JNS_A", "DD", at=1010.0)])
        service = make_service(table=table)
        service.tick()
        service.tick()
        self.assertEqual(service.tracker.hands, 2)
        self.assertEqual(service.sim.strategy.results, ["VD", "DD"])

    def test_simulation_mode_arms_on_paper_and_never_places(self):
        table = FakeTable([state("JNS_C", "VD", at=1000.0)])
        service = make_service(table=table, config={"place_real": False})
        service.tick()
        self.assertEqual(table.places, [])
        self.assertIsNotNone(service.sim.strategy.pending, "mô phỏng chốt trên giấy")

    def test_real_mode_places_40_and_only_then_arms(self):
        table = FakeTable([state("JNS_C", "VD", at=1000.0, balance=500.0)])
        service = make_service(table=table, config={"place_real": True})
        service.tick()
        self.assertEqual(table.places, [40])
        self.assertIsNotNone(service.sim.strategy.pending, "chip vào rồi mới chốt sổ")

    def test_low_balance_skips_placing_and_keeps_the_ladder(self):
        table = FakeTable([state("JNS_C", "VD", at=1000.0, balance=10.0)])
        service = make_service(table=table, config={"place_real": True})
        service.tick()
        self.assertEqual(table.places, [])
        self.assertIsNotNone(service.sim.strategy.pending)

    def test_failed_placement_does_not_arm_the_plan(self):
        table = FakeTable([state("JNS_C", "VD", at=1000.0, balance=500.0)])
        table.place_result = {"ok": False, "error": "Ván đã có kết quả"}
        service = make_service(table=table, config={"place_real": True})
        service.tick()
        self.assertIsNone(service.sim.strategy.pending, "đặt hỏng thì không được tính là đã cược")


class ConfigTests(unittest.TestCase):
    def test_config_set_persists_and_updates_the_ladder(self):
        service = make_service()
        service.config_set({"ladder": [20, 40, 80], "place_real": True, "min_gap": 20})
        saved = json.loads(service.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["ladder"], [20, 40, 80])
        self.assertTrue(saved["place_real"])
        self.assertEqual(saved["min_gap"], 20)
        self.assertEqual(service.sim.strategy.ladder, [20, 40, 80])
        self.assertEqual(service.tracker.min_gap, 20)

    def test_ladder_from_capital_matches_the_board(self):
        service = make_service()
        snapshot = service.ladder_from_capital({"capital": 10000, "base": 40})
        self.assertEqual(snapshot["ladder"], [40, 80, 160, 320, 640, 1280, 2560, 4920])
        self.assertEqual(sum(snapshot["ladder"]), 10000)

    def test_session_reset_clears_strategy_and_tracker(self):
        service = make_service()
        service.tick()
        service.session_reset()
        self.assertEqual(service.sim.strategy.results, [])
        self.assertEqual(service.tracker.hands, 0)

    def test_manual_hand_rejects_bad_input(self):
        service = make_service()
        self.assertFalse(service.manual_hand({"result": "x"})["ok"])
        self.assertTrue(service.manual_hand({"result": "VD"})["ok"])
        self.assertEqual(service.sim.strategy.results, ["VD"])


class ToolLifecycleTests(unittest.TestCase):
    def test_tool_refuses_to_start_without_chrome(self):
        service = make_service()
        service.browser = type("FakeBrowser", (), {"connected": False})()
        result = service.tool_start()
        self.assertFalse(result["ok"])
        self.assertIn("Chưa mở Chrome", result["error"])


class BaccaratEngineTests(unittest.TestCase):
    def test_scan_maps_browser_reports_to_rows(self):
        emit = FakeEmitter()
        service = engine.BaccaratService(pathlib.Path(tempfile.mkdtemp()) / "b.json", emit)
        service.browser = type("FakeBrowser", (), {
            "connected": True,
            "scan": lambda self: [{"tables": [{"id": "t1", "title": "Bàn 1", "letters": "BBBPPP"}]}],
        })()
        snapshot = service.scan()
        self.assertEqual(snapshot["tables"][0]["id"], "t1")
        self.assertEqual(snapshot["tables"][0]["letters"], "BBBPPP")


if __name__ == "__main__":
    unittest.main(verbosity=2)
