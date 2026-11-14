# -*- coding: utf-8 -*-
"""Kiểm thử kiến trúc: giữ cho dự án ở đúng MVC về sau này.

Các luật được kiểm tra bằng AST (không cần chạy app):

    Model      -> không import tkinter/selenium/websocket, không import View/Controller/Adapter
    View       -> không import Adapter, không import Controller
    Adapter    -> không import View/Controller
    Controller -> được phép import cả ba tầng còn lại (composition root)
    app.py     -> chỉ là shim alias ``bcr.controllers.app``

Ngoài ra chặn Đua ngựa quay lại (không còn ``race``/``horse`` trong mã).
"""

import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
PKG = ROOT / "bcr"

FORBIDDEN_FOR_MODEL = ("tkinter", "selenium", "websocket")
UI_MODULES = ("bcr.adapters", "bcr.views", "bcr.controllers")


def module_files(layer):
    return sorted((PKG / layer).glob("*.py"))


def imported_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append(base)
            names.extend("%s.%s" % (base, alias.name) for alias in node.names)
    return names


def matches(name, prefixes):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


class LayerDependencyTests(unittest.TestCase):
    def test_models_do_not_reach_out_to_ui_browsers_or_each_other_layers(self):
        for path in module_files("models"):
            names = imported_names(path)
            for forbidden in FORBIDDEN_FOR_MODEL:
                self.assertFalse(
                    any(matches(name, (forbidden,)) for name in names),
                    "%s: Model không được import %s" % (path.name, forbidden))
            for ui in UI_MODULES:
                self.assertFalse(
                    any(matches(name, (ui,)) for name in names),
                    "%s: Model không được import %s" % (path.name, ui))

    def test_views_do_not_talk_to_adapters_or_controllers(self):
        for path in module_files("views"):
            names = imported_names(path)
            self.assertFalse(any(matches(name, ("bcr.adapters",)) for name in names),
                             "%s: View phải đi qua Controller, không gọi Adapter" % path.name)
            self.assertFalse(any(matches(name, ("bcr.controllers",)) for name in names),
                             "%s: View không được import Controller" % path.name)

    def test_adapters_do_not_depend_on_views_or_controllers(self):
        for path in module_files("adapters"):
            names = imported_names(path)
            self.assertFalse(any(matches(name, ("bcr.views", "bcr.controllers")) for name in names),
                             "%s: Adapter chỉ được dùng Model" % path.name)

    def test_controllers_are_the_only_composition_root(self):
        """Controller được import View + Model + Adapter: đó là chỗ duy nhất nối ba tầng."""
        names = set()
        for path in module_files("controllers"):
            names.update(imported_names(path))
        for expected in ("bcr.views", "bcr.models", "bcr.adapters"):
            self.assertTrue(any(matches(name, (expected,)) for name in names),
                            "thiếu import %s trong tầng Controller" % expected)


class CompositionTests(unittest.TestCase):
    def test_entry_point_is_a_thin_alias(self):
        text = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("sys.modules[__name__] = _module", text)
        self.assertNotIn("class BCRApp", text)
        self.assertLess(len(text.splitlines()), 40, "app.py chỉ nên là shim mỏng")

    def test_root_modules_are_compatibility_shims(self):
        for name in ("browser.py", "lobby_stream.py", "table_bets.py", "human_mouse.py",
                     "ghost_mouse.py", "mouse_chromedriver.py", "engine.py", "method.py",
                     "money.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("sys.modules[__name__] = _module", text, name)
            self.assertLess(len(text.splitlines()), 40, "%s chỉ nên là shim mỏng" % name)

    def test_bcrapp_mixes_view_and_controller_but_not_model(self):
        import app

        bases = [base.__name__ for base in app.BCRApp.__mro__]
        self.assertIn("MainWindowMixin", bases)
        self.assertIn("LongLongPanelMixin", bases)
        self.assertIn("HuntMixin", bases)
        for model_class in ("LiveSimulation", "LongLongSimulation", "MoneyBook"):
            self.assertNotIn(model_class, bases, "Model không được là base của BCRApp")

    def test_shared_state_file_lives_at_project_root(self):
        import app

        self.assertEqual(pathlib.Path(app.STATE_FILE).parent, ROOT)


class NoHorseRacingLeftoversTests(unittest.TestCase):
    """Đua ngựa đã bị thay hẳn bằng LONG-LONG."""

    PATTERNS = (
        r"\bimport race\b", r"\bfrom bcr\.models import race\b", r"\brace\.",
        r"\bRaceStrategy\b", r"\bRaceSimulation\b", r"\bRacePanelMixin\b",
        r"\bdecode_race_results\b", r"\bsweep_race\b", r"\bread_race_results\b",
        r"\bGAME_RACE\b", r"\braceGrids\b", r"\b_build_horse_panel\b", r"_race_saved",
        r"rng-racetrack", r"\brace_ladder\b", r"\brace_base\b",
    )

    def test_no_race_symbols_in_python_or_js(self):
        files = [p for p in list(ROOT.glob("*.py")) + list(PKG.rglob("*.py"))
                 if p.name not in ("test_architecture.py",)]
        files += list(PKG.rglob("*.js"))
        for path in files:
            text = path.read_text(encoding="utf-8")
            for pattern in self.PATTERNS:
                self.assertIsNone(
                    re.search(pattern, text),
                    "%s còn sót %r" % (path.relative_to(ROOT), pattern))


class LongLongWiringTests(unittest.TestCase):
    def test_the_game_menu_offers_baccarat_and_longlong(self):
        text = (PKG / "views/main_window.py").read_text(encoding="utf-8")
        self.assertIn('("baccarat", "Baccarat")', text)
        self.assertIn('("longlong", "LONG-LONG")', text)

    def test_state_keys_use_longlong_names(self):
        text = (PKG / "controllers/state_store.py").read_text(encoding="utf-8")
        for key in ('"longlong_base"', '"longlong_ladder"'):
            self.assertIn(key, text)
        self.assertIn('("baccarat", "longlong")', text)
        self.assertNotIn("longlong_side", text, "LONG-LONG chỉ đánh một ô, không có cửa để chọn")

    def test_report_carries_a_longlong_section(self):
        text = (PKG / "adapters/browser.py").read_text(encoding="utf-8")
        self.assertIn('"longlong": longlong', text)
        reader = (PKG / "adapters/reader.js").read_text(encoding="utf-8")
        self.assertIn("longlong: longlongGrids()", reader)

    def test_live_balance_is_wired_through_controller_not_view(self):
        """Số dư thật: Adapter đọc CDP, Controller đẩy sang View — View không tự gọi."""
        import app

        self.assertIn("LongLongLiveMixin", [base.__name__ for base in app.BCRApp.__mro__])
        panel = (PKG / "views/longlong_panel.py").read_text(encoding="utf-8")
        self.assertNotIn("longlong_table", panel)
        self.assertIn("_longlong_render_live", panel)
        controller = (PKG / "controllers/app.py").read_text(encoding="utf-8")
        self.assertIn("longlong_live_autostart()", controller)
        self.assertIn("longlong_live_stop()", controller)
        live = (PKG / "controllers/longlong_live.py").read_text(encoding="utf-8")
        self.assertIn("from bcr.adapters import longlong_table", live)


if __name__ == "__main__":
    unittest.main(verbosity=2)
