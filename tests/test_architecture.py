# -*- coding: utf-8 -*-
"""Kiểm thử kiến trúc: giữ dự án đúng MVC và không quay lại tkinter.

Luật được kiểm bằng AST (không cần chạy app, không cần Chrome):

    Model      -> không import tkinter/selenium/websocket, không import tầng trên
    Adapter    -> không import Controller/Service
    Controller -> không import Service (Service gọi Controller, không ngược lại)
    Service    -> chỉ là transport JSON-RPC, không chứa nghiệp vụ
    Toàn repo  -> không còn import tkinter, không còn package ``bcr.views``

Ngoài ra chặn Đua ngựa quay lại (không còn ``race``/``horse`` trong mã).
"""

import ast
import importlib
import inspect
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "bcr"

FORBIDDEN_FOR_MODEL = ("tkinter", "selenium", "websocket")
UPSTREAM_OF_MODEL = ("bcr.adapters", "bcr.controllers", "bcr.service")


def module_files(layer):
    return sorted((PKG / layer).glob("*.py"))


def source_files(*folders):
    files = []
    for folder in folders:
        files.extend(sorted((ROOT / folder).rglob("*.py")))
    return files


def imported_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append(base)
            names.extend("%s.%s" % (base, alias.name) for alias in node.names)
    return names


def defined_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    return names


def matches(name, prefixes):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


class LayerDependencyTests(unittest.TestCase):
    def test_models_are_pure_logic(self):
        for path in module_files("models"):
            names = imported_names(path)
            for forbidden in FORBIDDEN_FOR_MODEL:
                self.assertFalse(any(matches(name, (forbidden,)) for name in names),
                                 "%s: Model không được import %s" % (path.name, forbidden))
            for upstream in UPSTREAM_OF_MODEL:
                self.assertFalse(any(matches(name, (upstream,)) for name in names),
                                 "%s: Model không được import %s" % (path.name, upstream))

    def test_adapters_do_not_depend_on_controllers_or_service(self):
        for path in module_files("adapters"):
            names = imported_names(path)
            self.assertFalse(any(matches(name, ("bcr.controllers", "bcr.service"))
                                 for name in names),
                             "%s: Adapter chỉ được dùng Model/config" % path.name)

    def test_controllers_do_not_import_the_transport(self):
        for path in module_files("controllers"):
            names = imported_names(path)
            self.assertFalse(any(matches(name, ("bcr.service",)) for name in names),
                             "%s: Controller không được import tầng transport" % path.name)

    def test_controllers_are_the_composition_root(self):
        """Controller là nơi duy nhất nối Model + Adapter."""
        names = set()
        for path in module_files("controllers"):
            names.update(imported_names(path))
        for expected in ("bcr.models", "bcr.adapters"):
            self.assertTrue(any(matches(name, (expected,)) for name in names),
                            "thiếu import %s trong tầng Controller" % expected)

    def test_service_is_a_thin_transport(self):
        path = PKG / "service/engine.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
        classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
        self.assertIn("main", functions)
        self.assertIn("build_service", functions)
        self.assertIn("Emitter", classes)
        # Không được định nghĩa controller/luật cầu trong tầng transport.
        self.assertEqual([name for name in classes if name != "Emitter"], [])
        self.assertLess(len(path.read_text(encoding="utf-8").splitlines()), 200,
                        "engine.py chỉ nên là transport mỏng")


class NoTkinterAnymoreTests(unittest.TestCase):
    def test_no_python_file_imports_tkinter(self):
        checked = 0
        for path in source_files("bcr", "tests", "tools"):
            names = imported_names(path)
            checked += 1
            self.assertFalse(any(matches(name, ("tkinter",)) for name in names),
                             "%s còn import tkinter" % path.relative_to(ROOT))
        self.assertGreater(checked, 10)

    def test_views_package_is_gone(self):
        self.assertFalse((PKG / "views").exists(), "giao diện phải nằm ở ui/ (Electron)")
        self.assertFalse((ROOT / "app.py").exists(), "không còn entry tkinter ở gốc")

    def test_root_has_no_compatibility_shims(self):
        for shim in ("browser.py", "engine.py", "lobby_stream.py", "method.py",
                     "money.py", "table_bets.py", "human_mouse.py", "ghost_mouse.py",
                     "mouse_chromedriver.py"):
            self.assertFalse((ROOT / shim).exists(), "%s phải dùng bcr.* trực tiếp" % shim)


class StructureTests(unittest.TestCase):
    def test_mvc_modules_are_present(self):
        for rel in ("bcr/config.py", "bcr/__main__.py",
                    "bcr/models/method.py", "bcr/models/money.py", "bcr/models/live_sim.py",
                    "bcr/adapters/browser.py", "bcr/adapters/lobby_stream.py",
                    "bcr/adapters/table_bets.py",
                    "bcr/controllers/baccarat.py", "bcr/controllers/longlong.py",
                    "bcr/service/engine.py",
                    "README.md"):
            self.assertTrue((ROOT / rel).exists(), rel)

    def test_electron_ui_files_are_present(self):
        for rel in ("ui/package.json", "ui/main.js", "ui/preload.js",
                    "ui/renderer/index.html", "ui/renderer/app.js", "ui/renderer/styles.css"):
            self.assertTrue((ROOT / rel).exists(), rel)

    def test_games_use_separate_ports_profiles_and_state_files(self):
        from bcr.service import engine

        baccarat, longlong = engine.GAMES["baccarat"], engine.GAMES["longlong"]
        self.assertNotEqual(baccarat[0], longlong[0], "mỗi trò một cổng CDP riêng")
        self.assertNotEqual(baccarat[1], longlong[1], "mỗi trò một profile Chrome riêng")
        self.assertNotEqual(baccarat[2], longlong[2], "mỗi trò một file trạng thái riêng")
        self.assertEqual(pathlib.Path(baccarat[2]).parent, pathlib.Path("."))

    def test_main_module_exposes_the_engine_entry(self):
        text = (PKG / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("from bcr.service.engine import main", text)


class ControllerApiTests(unittest.TestCase):
    def test_both_controllers_expose_dispatch_and_snapshot(self):
        for module_name, class_name in (("bcr.controllers.baccarat", "BaccaratController"),
                                        ("bcr.controllers.longlong", "LongLongController")):
            module = importlib.import_module(module_name)
            controller = getattr(module, class_name)
            self.assertTrue(callable(getattr(controller, "dispatch", None)),
                            "%s thiếu dispatch()" % class_name)
            self.assertTrue(callable(getattr(controller, "snapshot", None)),
                            "%s thiếu snapshot()" % class_name)
            self.assertFalse(any(matches(name, ("tkinter",))
                                 for name in imported_names(pathlib.Path(module.__file__))),
                             "%s không được import tkinter" % class_name)

    def test_engine_keeps_the_legacy_class_aliases(self):
        from bcr.service import engine

        self.assertIs(engine.BaccaratService, engine.BaccaratController)
        self.assertIs(engine.LongLongService, engine.LongLongController)

    def test_dispatch_reports_unknown_methods_without_raising(self):
        import pathlib as _path
        import tempfile

        from bcr.controllers.baccarat import BaccaratController

        class Emitter:
            def event(self, name, data):
                pass

        controller = BaccaratController(_path.Path(tempfile.mkdtemp()) / "s.json", Emitter())
        result, error = controller.dispatch("khong.ton.tai", {})
        self.assertIsNone(result)
        self.assertIn("không hỗ trợ", error)


class ElectronWiringTests(unittest.TestCase):
    def test_ui_never_touches_python_directly(self):
        """Renderer chỉ nói chuyện qua preload/contextBridge, không spawn tiến trình."""
        renderer = (ROOT / "ui/renderer/app.js").read_text(encoding="utf-8")
        self.assertNotIn("require('child_process')", renderer)
        self.assertNotIn("spawn(", renderer)
        preload = (ROOT / "ui/preload.js").read_text(encoding="utf-8")
        self.assertIn("contextBridge.exposeInMainWorld", preload)
        main = (ROOT / "ui/main.js").read_text(encoding="utf-8")
        self.assertNotIn("nodeIntegration: true", main)
        self.assertIn("bcr.service.engine", main, "main.js spawn engine qua -m bcr.service.engine")


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
                 + list((ROOT / "tests").rglob("*.py")) + list((ROOT / "tools").rglob("*.py"))
                 if p != pathlib.Path(__file__)]
        files += list(PKG.rglob("*.js")) + list((ROOT / "ui").rglob("*.js"))
        for path in files:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            for pattern in self.PATTERNS:
                self.assertIsNone(re.search(pattern, text),
                                  "%s còn sót %r" % (path.relative_to(ROOT), pattern))


if __name__ == "__main__":
    unittest.main(verbosity=2)
