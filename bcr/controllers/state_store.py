# -*- coding: utf-8 -*-
"""Tầng Controller: đọc/ghi/lưu trữ file trạng thái ``bcr_state.json``.

Tách khỏi View để việc lưu phiên không phụ thuộc widget nào.
"""
import json
import os
import time


from bcr.adapters import browser as BR
from bcr.config import *
from bcr.models import method as ME
from bcr.models import money as M
from bcr.models.live_sim import LiveSimulation


class StateStoreMixin:
    """Lưu/khôi phục phiên, lưu trữ state cũ và di trú thang điểm UI."""

    def _load_state(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            settings = data.get("settings", {})
            self._migrate_scale(settings)
            self.sim.money = M.MoneyBook(**{k: settings[k] for k in (
                "capital", "unit", "take_profit_pct") if k in settings})
            profile = settings.get("profile", ME.DEFAULT_PROFILE)
            if profile in ME.PROFILES:
                try:
                    self.sim.set_method(profile, settings.get("ladder"))
                except ValueError:
                    self.sim.set_method(profile, None)
                self.ladder_store[profile] = [M.fmt_input_money(s) for s in self.sim.ladder]
            block_mode = settings.get("block_mode", ME.BLOCK_MODE_RECALCULATE)
            if block_mode == "alternate":
                block_mode = ME.BLOCK_MODE_REPEAT
            if block_mode not in (ME.BLOCK_MODE_RECALCULATE, ME.BLOCK_MODE_REPEAT):
                block_mode = ME.BLOCK_MODE_RECALCULATE
            self.block_mode_var.set(block_mode)
            self.sim.block_mode = block_mode
            self.chrome_startup_var.set(bool(settings.get("chrome_startup", False)))
            self.cdp_var.set(bool(settings.get("chrome_cdp", True)))
            saved_url = settings.get("start_url")
            if isinstance(saved_url, str) and saved_url.strip() and BR.valid_site_url(saved_url.strip()):
                self.start_url_var.set(saved_url.strip())
            self.auto_cycle_var.set(bool(settings.get("auto_cycle", True)))
            self.auto_hunt_var.set(bool(settings.get("auto_hunt", True)))
            saved_tables = settings.get("tables_to_play", 1)
            if isinstance(saved_tables, (int, float)) and not isinstance(saved_tables, bool) and saved_tables >= 1:
                self.tables_var.set(str(int(saved_tables)))
            self.favorites_only.set(bool(settings.get("favorites_only", True)))
            self.human_mouse_var.set(bool(settings.get("human_mouse", True)))
            saved_game = settings.get("game_mode", "baccarat")
            self.game_mode = saved_game if saved_game in ("baccarat", "longlong") else "baccarat"
            self._longlong_saved = {"base": settings.get("longlong_base"),
                                    "ladder": settings.get("longlong_ladder")}
            self.auto_place_var.set(bool(settings.get("auto_place", True)))
            self.auto_recover_var.set(bool(settings.get("auto_recover", True)))
            saved_mode = settings.get("ladder_mode", "manual")
            self.ladder_mode_var.set(saved_mode if saved_mode in ("manual", "multiply") else "manual")
            saved_base = settings.get("ladder_base")
            if isinstance(saved_base, str) and saved_base.strip():
                self.ladder_base_var.set(saved_base)
            low, high, streak, min_lead = self.sim.set_road_filter(
                settings.get("hands_min", ME.MIN_HANDS),
                settings.get("hands_max", ME.MAX_HANDS),
                settings.get("max_streak", ME.MAX_STREAK_ALLOWED),
                settings.get("min_lead", ME.DEFAULT_MIN_LEAD))
            self.hands_min_var.set(str(low))
            self.hands_max_var.set(str(high))
            self.streak_var.set(str(streak))
            self.min_lead_var.set(str(min_lead))
            live_exit_streak = self.sim.set_live_exit_streak(
                settings.get("live_exit_streak", ME.DEFAULT_LIVE_EXIT_STREAK))
            self.live_exit_streak_var.set(str(live_exit_streak))
            saved_window = settings.get("chrome_window")
            if isinstance(saved_window, dict):
                loaded = BR.normalize_window((saved_window.get("width"), saved_window.get("height")),
                                             saved_window.get("percent"), saved_window.get("fullscreen"))
                self.chrome_fullscreen_var.set(loaded["fullscreen"])
                self.chrome_w_var.set(str(loaded["width"]))
                self.chrome_h_var.set(str(loaded["height"]))
                self.chrome_scale_var.set(str(loaded["percent"]))
            scale = settings.get("ui_scale", DEFAULT_UI_SCALE)
            if isinstance(scale, (int, float)) and not isinstance(scale, bool) and 50 <= scale <= 150:
                self.ui_scale = int(scale)
            if data.get("format") != "live-v2":
                self._archive_state()
                if data.get("log") or data.get("results"):
                    self.sim.note = "Phiên cũ đã được lưu riêng; không trộn dữ liệu cũ/thử nghiệm vào thống kê trực tiếp."
                return
            self.sim.log = data.get("log", [])
            for key in MONEY_FIELDS:
                if key in data.get("money", {}):
                    setattr(self.sim.money, key, data["money"][key])
            self.sim.note = "Đã khôi phục sổ vốn. Chọn bàn để chạy tiếp; không tự tính các tay lúc tắt app."
        except (OSError, ValueError, TypeError, AttributeError):
            self.sim = LiveSimulation()
            self.sim.on_arm = self.place_armed_bet
            self.state_read_error = True
            self.connection_text = "Không đọc được file phiên. Giữ nguyên file gốc; không tự ghi đè."

    def _save_state(self):
        if self.state_read_error:
            self.sim.pause("Không thể lưu phiên: file cũ chưa đọc được. Dữ liệu gốc được giữ nguyên.")
            return False
        book = self.sim.money
        window = self._window_config()
        data = dict(format="live-v2", scale="vn2", log=self.sim.log,
                    settings=dict(capital=book.start_capital, unit=book.unit,
                                  take_profit_pct=book.take_profit_pct,
                                  profile=self.sim.profile, ladder=self.sim.ladder,
                                  block_mode=self.sim.block_mode,
                                  chrome_startup=self.chrome_startup_var.get(), chrome_cdp=self.cdp_var.get(),
                                  start_url=self.start_url_var.get(),
                                  auto_cycle=self.auto_cycle_var.get(),
                                  auto_hunt=self.auto_hunt_var.get(),
                                  tables_to_play=self._tables_target(),
                                  favorites_only=self.favorites_only.get(),
                                   human_mouse=self.human_mouse_var.get(),
                                   auto_place=self.auto_place_var.get(),
                                   auto_recover=self.auto_recover_var.get(),
                                   ladder_mode=self.ladder_mode_var.get(),
                                   ladder_base=self.ladder_base_var.get(),
                                   hands_min=self.sim.min_hands,
                                   hands_max=self.sim.max_hands,
                                   max_streak=self.sim.max_streak,
                                   min_lead=self.sim.min_lead,
                                   live_exit_streak=self.sim.live_exit_streak,
                                  chrome_window=dict(width=window["width"], height=window["height"],
                                                     percent=window["percent"],
                                                     fullscreen=window["fullscreen"]),
                                   chrome_url=BR.START_URL, ui_scale=self.ui_scale,
                                   game_mode=self.game_mode,
                                   longlong_base=self.longlong_base_var.get(),
                                   longlong_ladder=[var.get()
                                                    for var in self.longlong_ladder_vars]),
                    money={key: getattr(book, key) for key in MONEY_FIELDS})
        try:
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.state_file)
            return True
        except OSError:
            self.sim.pause("Không lưu được phiên xuống ổ đĩa. Đã tạm dừng; dữ liệu vẫn còn trong ứng dụng.")
            return False

    def _archive_state(self):
        if self.state_file.exists():
            archive = self.state_file.with_name("bcr-session-%s-%d.json" % (time.strftime("%Y%m%d-%H%M%S"), time.time_ns()))
            archive.write_bytes(self.state_file.read_bytes())

    def _migrate_scale(self, settings):
        """Chuyển state cũ (thang VNĐ, nhân 1000) về thang số của sàn (VN2), đúng một lần.

        File state cũ lưu vốn 20.000.000, bậc 100.000, đơn vị chip 1.000; sàn hiển thị
        3.014,74 và mức 100. Nếu không đổi, UI sẽ tiếp tục sai thang so với web.
        """
        if settings.get("scale") == "vn2":
            return
        divisor = 1000.0
        capital = settings.get("capital")
        if isinstance(capital, (int, float)) and not isinstance(capital, bool) and capital >= divisor * 10:
            settings["capital"] = capital / divisor
            settings["unit"] = (settings.get("unit") or 0) / divisor
            settings["take_profit_pct"] = settings.get("take_profit_pct", 25.0)
            ladder = settings.get("ladder")
            if isinstance(ladder, list):
                settings["ladder"] = [s / divisor for s in ladder
                                      if isinstance(s, (int, float)) and not isinstance(s, bool)]
            base = settings.get("ladder_base")
            if isinstance(base, str):
                digits = base.replace(".", "").replace(",", "").strip()
                if digits.isdigit() and int(digits) >= 1000:
                    settings["ladder_base"] = M.fmt_input_money(int(digits) / divisor)
            # Bộ lọc tay cũ mặc định 15-20; nay là 10-50.
            if settings.get("hands_min") == 15 and settings.get("hands_max") == 20:
                settings["hands_min"], settings["hands_max"] = ME.MIN_HANDS, ME.MAX_HANDS
        settings["scale"] = "vn2"
