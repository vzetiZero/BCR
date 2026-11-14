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
import time
import unittest

from bcr.adapters import browser as BR
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


class BaccaratTableFilterTests(unittest.TestCase):
    """Lọc bàn Baccarat: hoà điểm ưu tiên bên đỏ, chặn lệch B/P quá lớn (4-5 tay)."""

    @staticmethod
    def road(*runs):
        """Ghép cầu từ các vệt (ký tự, số tay) để dễ kiểm soát số B/P và bệt."""
        return "".join(side * count for side, count in runs)

    # B 13 · P 9 (lệch 4), không bệt quá 3 tay, không có hai vệt 3 liền nhau.
    LEAD4 = road(("B", 3), ("P", 2), ("B", 3), ("P", 2), ("B", 2), ("P", 3),
                 ("B", 2), ("P", 2), ("B", 3))
    # B 20 · P 10 (lệch 10) — vượt mức tối đa mặc định.
    LEAD10 = road(("B", 3), ("P", 2), ("B", 2), ("P", 1), ("B", 3), ("P", 2),
                  ("B", 2), ("P", 1), ("B", 3), ("P", 2), ("B", 2), ("P", 1),
                  ("B", 3), ("P", 1), ("B", 2))
    # Hoà điểm B 8 · P 8, vệt cuối là Player.
    TIE = road(("B", 2), ("P", 3), ("B", 2), ("P", 2), ("B", 2), ("P", 2),
               ("B", 2), ("P", 1))

    def service(self, letters, config=None):
        emit = FakeEmitter()
        service = engine.BaccaratService(pathlib.Path(tempfile.mkdtemp()) / "b.json", emit)
        if config:
            result = service.config_set(config)
            self.assertTrue(result["ok"], result)
        # table_rows chỉ trả bàn khi còn Chrome: giả lập một phiên đang kết nối.
        service.browser = type("FakeBrowser", (), {"connected": True, "note": "",
                                                   "mode": "cdp", "stop": lambda self: None})()
        now = __import__("time").time()
        service.tables = {"id:t1": {
            "key": "id:t1", "label": "Bàn 1", "letters": letters, "ts": now,
            "table_id": "t1", "game_type": "", "read_error": "", "source": "evo-ws",
            "conflict": False, "stable": 3, "ws_confirmed_letters": letters,
        }}
        return service

    def test_default_window_accepts_a_four_hand_gap(self):
        row = self.service(self.LEAD4).table_rows()[0]
        self.assertEqual((row["banker"], row["player"], row["lead"]), (13, 9, 4))
        self.assertTrue(row["eligible"], row["verdict"])
        self.assertTrue(row["verdict"].startswith("Đạt 5/5"), row["verdict"])

    def test_gap_bigger_than_max_lead_is_rejected(self):
        row = self.service(self.LEAD10).table_rows()[0]
        self.assertEqual((row["banker"], row["player"], row["lead"]), (20, 10, 10))
        self.assertFalse(row["eligible"])
        self.assertIn("chỉ nhận ≤ 5", row["verdict"])

    def test_config_can_widen_or_drop_the_upper_bound(self):
        wide = self.service(self.LEAD10, {"max_lead": 10}).table_rows()[0]
        self.assertTrue(wide["eligible"], wide["verdict"])
        unlimited = self.service(self.LEAD10, {"max_lead": None}).table_rows()[0]
        self.assertTrue(unlimited["eligible"], unlimited["verdict"])
        self.assertEqual(len(unlimited["checks"]), 4)   # không còn điều kiện trần

    def test_tie_prefers_red_banker_by_default(self):
        row = self.service(self.TIE).table_rows()[0]
        self.assertEqual((row["banker"], row["player"], row["lead"]), (8, 8, 0))
        self.assertTrue(row["tie_break"])
        self.assertEqual(row["strong"], "B")
        self.assertEqual(row["tie_side"], "B")
        self.assertFalse(row["eligible"])            # lệch 0 vẫn dưới mức tối thiểu

    def test_tie_can_follow_the_running_streak(self):
        row = self.service(self.TIE, {"tie_side": "run"}).table_rows()[0]
        self.assertEqual(row["strong"], "P")        # vệt cuối là Player
        self.assertEqual(row["tie_side"], "run")

    def test_new_session_keeps_the_lead_window(self):
        service = self.service(self.LEAD4, {"max_lead": 4, "tie_side": "P"})
        self.assertTrue(service.session_new({"capital": 2500})["ok"])
        self.assertEqual((service.sim.min_lead, service.sim.max_lead), (3, 4))
        self.assertEqual(service.sim.tie_side, "P")


class LongLongChromeConfigTests(unittest.TestCase):
    """Tab LONG-LONG cũng cấu hình Chrome như Baccarat: link mở đầu + cửa sổ."""

    def service(self, path=None):
        folder = pathlib.Path(tempfile.mkdtemp())
        return engine.LongLongService(path or (folder / "state.json"), FakeEmitter())

    def test_config_has_window_and_start_url_by_default(self):
        service = self.service()
        window = service.config["chrome_window"]
        self.assertEqual(sorted(window), ["fullscreen", "height", "percent", "width"])
        self.assertTrue(service.config["start_url"].startswith("http"))

    def test_window_is_clamped_persisted_and_applied(self):
        service = self.service()
        reply = service.apply_window({"width": 99999, "height": 10, "percent": 500,
                                      "fullscreen": True})
        self.assertTrue(reply["ok"])
        window = service.config["chrome_window"]
        self.assertTrue(window["fullscreen"])
        # Kẹp đúng biên của adapter (SIZE_MAX/SIZE_MIN), không nhận số vô lý.
        self.assertEqual(window["width"], BR.SIZE_MAX[0])
        self.assertEqual(window["height"], BR.SIZE_MIN[1])
        self.assertEqual(window["percent"], BR.SCALE_MAX)
        self.assertIn("toàn màn hình", reply["note"].lower())
        saved = json.loads(service.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["chrome_window"], window)

    def test_config_set_accepts_window_and_url_then_snapshot_exposes_them(self):
        service = self.service()
        reply = service.config_set({"chrome_window": {"width": 1600, "height": 900,
                                                     "percent": 80, "fullscreen": False},
                                    "start_url": "https://example.com/lobby"})
        self.assertTrue(reply.get("ok"))
        self.assertEqual(reply["config"]["chrome_window"]["width"], 1600)
        self.assertEqual(reply["config"]["start_url"], "https://example.com/lobby")
        vm = service.dispatch("chrome.window", {"width": 1280, "height": 720, "percent": 100})[0]
        self.assertTrue(vm["ok"])

    def test_invalid_start_url_is_rejected(self):
        service = self.service()
        reply = service.config_set({"start_url": "khong-phai-link"})
        self.assertFalse(reply["ok"])
        self.assertIn("không hợp lệ", reply["error"])

    def test_state_file_with_bad_values_is_sanitized(self):
        folder = pathlib.Path(tempfile.mkdtemp())
        path = folder / "state.json"
        path.write_text(json.dumps({"start_url": "ftp://x", "chrome_window": {"width": 0}}),
                        encoding="utf-8")
        service = self.service(path)
        self.assertTrue(service.config["start_url"].startswith("http"))
        self.assertGreaterEqual(service.config["chrome_window"]["width"], 400)


class LongLongStatsWiringTests(unittest.TestCase):
    """Tab Thống kê lấy số liệu từ sổ tay của controller (không tự tính ở UI)."""

    def test_snapshot_carries_stats_block(self):
        service = make_service()
        stats = service.snapshot()["stats"]
        for key in ("hands", "wins", "losses", "win_rate", "max_loss_streak",
                    "current_loss_streak", "streak_histogram", "recent", "curve"):
            self.assertIn(key, stats)

    def test_manual_hands_are_settled_into_the_stats(self):
        service = make_service()
        # VD mở lệnh -> VV thua -> VD (chờ) -> VD thắng
        for raw in ("VD", "VV", "VD", "VD"):
            reply = service.manual_hand({"result": raw})
            self.assertTrue(reply.get("ok"), reply)
        stats = service.snapshot()["stats"]
        self.assertEqual(stats["hands"], 2)
        self.assertEqual((stats["wins"], stats["losses"]), (1, 1))
        self.assertEqual(stats["max_loss_streak"], 1)
        self.assertEqual(stats["net_units"], 36)          # -40 (bậc 1) + 76 (bậc 2 thắng)
        self.assertEqual(stats["recent"], ["L", "W"])


class ChromeClosedRecoveryTests(unittest.TestCase):
    """Đóng Chrome bằng tay: app phải thấy mất kết nối và còn đường mở lại."""

    @staticmethod
    def fake_browser(connected=False, note=""):
        return type("FakeBrowser", (), {"connected": connected, "note": note,
                                        "mode": "cdp" if connected else "",
                                        "stop": lambda self: None})()

    def baccarat(self):
        emit = FakeEmitter()
        service = engine.BaccaratService(pathlib.Path(tempfile.mkdtemp()) / "b.json", emit)
        now = time.time()
        service.tables = {"id:x": {"key": "id:x", "label": "Bàn X", "letters": "BBBP",
                                   "ts": now, "table_id": "x", "game_type": "",
                                   "read_error": "", "source": "evo-ws",
                                   "conflict": False, "stable": 2}}
        service.tool_running = True
        return service

    def longlong(self):
        folder = pathlib.Path(tempfile.mkdtemp())
        return engine.LongLongService(folder / "state.json", FakeEmitter())

    def test_baccarat_clears_tables_and_pauses_when_chrome_closes(self):
        service = self.baccarat()
        service.browser = self.fake_browser(connected=True)
        service._on_chrome_lost()
        self.assertEqual(service.tables, {})
        self.assertIn("Mở Chrome", service.sweep_note)
        self.assertTrue(service.offline_noted)

    def test_baccarat_snapshot_reports_disconnected(self):
        service = self.baccarat()
        service.browser = self.fake_browser(connected=False, note="Chrome đã đóng")
        snapshot = service.snapshot()
        self.assertFalse(snapshot["chrome_connected"])
        self.assertEqual(snapshot["tables"], [])

    def test_baccarat_ready_flag_resets_after_reconnect(self):
        service = self.baccarat()
        service._on_chrome_lost()
        self.assertTrue(service.offline_noted)
        service._on_chrome_ready()
        self.assertFalse(service.offline_noted)

    def test_longlong_hides_stale_numbers_when_chrome_closed(self):
        service = self.longlong()
        service.last_state = {"ok": True, "balance_vn2": 500.0, "read_at": 1.0}
        service.browser = self.fake_browser(connected=False, note="Chrome đã đóng")
        snapshot = service.snapshot()
        self.assertFalse(snapshot["chrome_connected"])
        self.assertIsNone(snapshot["balance_vn2"])
        self.assertEqual(snapshot["chrome_note"], "Chrome đã đóng")

    def test_longlong_reopens_chrome_after_being_closed(self):
        service = self.longlong()
        opened = []
        service.browser = type("FakeBrowser", (), {
            "connected": False, "note": "", "mode": "",
            "start": lambda self, **kwargs: opened.append(kwargs) or True,
            "resize_window": lambda self, *a, **k: (True, "ok"),
            "stop": lambda self: None,
        })()
        reply = service.open_chrome()
        self.assertTrue(reply["ok"])
        time.sleep(0.4)                     # luồng mở Chrome chạy nền
        self.assertTrue(opened, "phải gọi mở Chrome lại được")
        self.assertEqual(opened[0]["url"], service.config["start_url"])

    def test_open_chrome_is_idempotent_while_alive(self):
        service = self.longlong()
        service.browser = self.fake_browser(connected=True)
        reply = service.open_chrome()
        self.assertTrue(reply["ok"])
        self.assertIn("đã kết nối", reply["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
