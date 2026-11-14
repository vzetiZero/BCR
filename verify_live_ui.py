import hashlib
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

from PIL import ImageGrab

import method as ME
from app import BCRApp, STATE_FILE, row_problem


root = tk.Tk()
temporary = tempfile.TemporaryDirectory(prefix="bcr-ui-check-")
state = Path(temporary.name) / "state.json"
before = hashlib.sha256(STATE_FILE.read_bytes()).hexdigest()
app = BCRApp(root, state_file=state, auto_connect=False)
app.auto_place_var.set(False)          # lượt kiểm tra UI không bao giờ bấm chip thật
errors = []
root.report_callback_exception = lambda *args: errors.append(str(args[1]))
started = time.time()
report = {"screenshots": [], "checks": []}


def screenshot(name):
    root.attributes("-topmost", True)
    root.lift()
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    path = Path(__file__).parent / name
    ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(path)
    root.attributes("-topmost", False)
    report["screenshots"].append(str(path))


def begin():
    assert not hasattr(app, "start_button") and not hasattr(app, "toggle_simulation")
    assert not app.sim.running
    # Nút mở Chrome nằm ở thanh trên cùng nên thấy được ở cả hai tab.
    assert app.top_chrome_button.cget("text") == "Mở Chrome", app.top_chrome_button.cget("text")
    assert app.top_chrome_button.cget("state") == "normal"
    assert app.top_attach_button.cget("text") == "Gắn Chrome"
    # Tab LONG-LONG: đúng thang 8 bậc theo cấu hình, chỉ một ô đánh nên không có ô chọn cửa.
    app.select_game("longlong")
    assert [v.get() for v in app.longlong_ladder_vars] == [
        "40", "80", "160", "320", "640", "1280", "2560", "4920"], app.longlong_ladder_vars[0].get()
    assert not hasattr(app, "longlong_side_var")
    assert app.longlong_next.cget("text").startswith("Tay kế tiếp:")
    # Khối số dư thật + nút cập nhật realtime phải có sẵn trên tab LONG-LONG.
    assert app.longlong_balance.cget("text") == "—"
    assert app.longlong_live_button.cget("text") in ("Bật cập nhật", "Tắt cập nhật")
    assert app.top_balance.cget("text").startswith("Số dư:")
    # Mặc định: tự chốt kết quả BẬT, đặt chip thật TẮT (an toàn tiền thật).
    assert app.longlong_auto_var.get() is True
    assert app.longlong_place_var.get() is False
    assert app.longlong_auto_log.cget("text") == "Chưa có ván nào."
    app.select_game("baccarat")
    app.search.set("khong-co-ban")
    app.only_eligible.set(True)
    app._render_tables()
    assert not app.live_tree.get_children()
    app.search.set("")
    app.only_eligible.set(False)
    app.nav_buttons["settings"].invoke()
    with patch("app.messagebox.showerror") as error:
        app.entries["capital"].set("invalid")
        app.apply_settings()
        assert error.called
    app.entries["capital"].set("20000000")
    app.entries["take_profit_pct"].set("10")
    with patch("app.messagebox.askyesno", return_value=True):
        app.apply_settings()
    assert len(list(state.parent.glob("bcr-session-*.json"))) == 1
    assert app.entries["capital"].get() == "20,000,000", app.entries["capital"].get()
    assert [v.get() for v in app.ladder_vars] == ["100", "300", "700", "1,500",
                                                  "3,100", "6,400"], app.ladder_vars[0].get()
    assert app.sim.money.capital == 20_000_000
    assert app.sim.money.take_profit_pct == 10
    road_filter()
    screenshot("ui-settings.png")
    app.nav_buttons["history"].invoke()
    with patch("app.messagebox.showinfo") as info:
        app.export_csv()
        assert info.called
    screenshot("ui-history.png")
    app.nav_buttons["overview"].invoke()
    root.geometry("560x620+20+20")
    root.after(400, minimum)


def road_filter():
    """Bộ lọc cầu nằm trong Cài đặt: đổi số tay bệt không được tạo phiên mới."""
    app.hands_min_var.set("12")
    app.hands_max_var.set("40")
    app.streak_var.set("5")
    app.apply_road_filter()
    assert (app.sim.min_hands, app.sim.max_hands, app.sim.max_streak) == (12, 40, 5)
    saved = json.loads(state.read_text(encoding="utf-8"))["settings"]
    assert (saved["hands_min"], saved["hands_max"], saved["max_streak"]) == (12, 40, 5), saved
    assert len(list(state.parent.glob("bcr-session-*.json"))) == 1
    with patch("app.messagebox.showerror") as error:
        app.streak_var.set("ba tay")
        app.apply_road_filter()
        assert error.called
    assert app.sim.max_streak == 5
    app.hands_min_var.set("10")
    app.hands_max_var.set("50")
    app.streak_var.set("3")
    app.apply_road_filter()
    assert (app.sim.min_hands, app.sim.max_hands, app.sim.max_streak) == (10, 50, 3)


def minimum():
    screenshot("ui-minimum.png")
    canvas = app.page_scrolls["overview"]
    canvas.yview_moveto(1)
    root.update()
    assert app.live_tree.winfo_rooty() + app.live_tree.winfo_height() <= root.winfo_rooty() + root.winfo_height()
    screenshot("ui-minimum-scrolled.png")
    report["minimum_layout"] = {"table_height": app.live_tree.winfo_height(),
                                "table_accessible_after_scroll": True,
                                "window_width": root.winfo_width(),
                                "window_height": root.winfo_height()}
    canvas.yview_moveto(0)
    root.geometry("600x900+20+20")
    root.update()
    screenshot("ui-overview.png")
    report["checks"].extend(["no_start_button", "search_filter", "invalid_settings", "archive_new_session",
                             "take_profit_config", "road_filter_config", "empty_export", "compact_layout"])
    app.attach_button.invoke()
    root.after(500, check_live)


def check_live():
    if app.chrome_busy or app.scan_busy or not any(row['stable'] >= 2 for row in app.tables.values()):
        if time.time() - started < 40:
            root.after(500, check_live)
            return
    report["connection"] = app.connection_text
    report["state"] = app.browser.connection_state
    report["driver"] = app.browser.driver_info
    report["frames"] = app.browser.last_frames
    report["table_count"] = len(app.tables)
    assert app.browser.cdp_attached and app.browser.driver is None
    assert app.tables, app.connection_text
    app.search.set('Lotus')
    app._render_tables()
    assert app.live_tree.get_children()
    assert all('Lotus' in app.tables[key]['label'] for key in app.live_tree.get_children())
    app.search.set('khong-co-ban')
    app._render_tables()
    assert not app.live_tree.get_children()
    app.search.set('')
    app._render_tables()
    first = app.live_tree.get_children()[0]
    app.live_tree.selection_set(first)
    app.load_live_selected()
    assert app.sim.table_key == first
    row = app.tables[first]
    info = ME.check_table(list(row["letters"]), app.sim.min_hands, app.sim.max_hands, app.sim.max_streak)
    if info["eligible"] and not row_problem(row):
        assert app.sim.running and app.sim.pending, app.sim.note
        report["auto_arm"] = {"side": app.sim.pending["side"], "stake": app.sim.pending["stake"],
                              "hands": len(row["letters"])}
    else:
        assert not app.sim.running
        report["auto_arm"] = {"refused": app.sim.note}
    app.sim.pause("Kết thúc lượt kiểm tra UI.")
    app.only_eligible.set(True)
    app._render_tables()
    report['eligible_count'] = len(app.live_tree.get_children())
    app.only_eligible.set(False)
    app.refresh()
    assert app.disconnect_button.cget('text') == 'Ngắt kết nối'
    report['checks'].extend(['read_only_attach', 'live_search', 'auto_arm_on_pick', 'eligible_filter'])
    screenshot("ui-live-overview.png")
    app.page_scrolls['overview'].yview_moveto(1)
    screenshot('ui-attached-tables.png')
    report["callback_errors"] = errors
    report["production_state_unchanged"] = before == hashlib.sha256(STATE_FILE.read_bytes()).hexdigest()
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    app.close()


root.after(600, begin)
root.mainloop()
temporary.cleanup()
assert not errors, errors
assert before == hashlib.sha256(STATE_FILE.read_bytes()).hexdigest()
assert not app.browser.connected
assert app.browser._cdp_page() is not None
print('Read-only disconnect kept the authenticated Chrome open.', flush=True)
