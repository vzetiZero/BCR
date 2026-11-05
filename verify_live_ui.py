import hashlib
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

from PIL import ImageGrab

from app import BCRApp, STATE_FILE


root = tk.Tk()
temporary = tempfile.TemporaryDirectory(prefix="bcr-ui-check-")
state = Path(temporary.name) / "state.json"
before = hashlib.sha256(STATE_FILE.read_bytes()).hexdigest()
app = BCRApp(root, state_file=state, auto_connect=False)
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
    app.start_button.invoke()
    assert not app.sim.running
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
    with patch("app.messagebox.askyesno", return_value=True):
        app.apply_settings()
    assert len(list(state.parent.glob("bcr-session-*.json"))) == 1
    assert app.entries["capital"].get() == "20,000,000", app.entries["capital"].get()
    assert [v.get() for v in app.ladder_vars] == ["100,000", "300,000", "700,000", "1,500,000",
                                                  "3,100,000", "6,400,000"], app.ladder_vars[0].get()
    assert app.sim.money.capital == 20_000_000
    screenshot("ui-settings.png")
    app.nav_buttons["history"].invoke()
    with patch("app.messagebox.showinfo") as info:
        app.export_csv()
        assert info.called
    screenshot("ui-history.png")
    app.nav_buttons["overview"].invoke()
    root.geometry("1080x740+20+20")
    root.after(400, minimum)


def minimum():
    screenshot("ui-minimum.png")
    canvas = app.page_scrolls["overview"]
    canvas.yview_moveto(1)
    root.update()
    assert app.live_tree.winfo_rooty() + app.live_tree.winfo_height() <= root.winfo_rooty() + root.winfo_height()
    screenshot("ui-minimum-scrolled.png")
    report["minimum_layout"] = {"table_height": app.live_tree.winfo_height(),
                                "table_accessible_after_scroll": True,
                                "window_height": root.winfo_height()}
    canvas.yview_moveto(0)
    root.geometry("1380x920+20+20")
    report["checks"].extend(["empty_start_guard", "search_filter", "invalid_settings", "archive_new_session", "empty_export"])
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
    app.start_button.invoke()
    assert not app.sim.running
    app.only_eligible.set(True)
    app._render_tables()
    report['eligible_count'] = len(app.live_tree.get_children())
    app.only_eligible.set(False)
    app.refresh()
    assert app.disconnect_button.cget('text') == 'Ngắt kết nối'
    report['checks'].extend(['read_only_attach', 'live_search', 'live_selection', 'confirmation_guard', 'eligible_filter'])
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
