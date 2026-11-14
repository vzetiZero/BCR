# -*- coding: utf-8 -*-
"""Tầng Controller: vòng đời Chrome/Selenium và nhịp quét bàn (scan).

Bọc Adapter ``browser``/``lobby_stream`` thành các lệnh one-shot cho UI gọi.
"""
import threading
import time


from bcr.adapters import browser as BR
from bcr.adapters import lobby_stream as LS
from bcr.config import *


class BrowserMixin:
    """Mở/đóng Chrome, vào sảnh, quét bàn và đọc cầu định kỳ."""

    def read_longlong_from_page(self, done):
        """Đọc lịch sử LONG-LONG từ tab Chrome rồi trả ``(letters, error)`` cho View.

        Chạy trong thread riêng để nút bấm không treo giao diện; ``done`` luôn được gọi
        trên UI thread qua ``root.after``. View không phải biết gì về CDP/Adapter.
        """
        def work():
            try:
                stream = LS.LobbyStream(self.browser, human_mouse=False, game="longlong")
                letters, error = stream.read_longlong_results("")
            except Exception as exc:          # adapter lỗi thì trả lỗi, không ném vào UI
                letters, error = "", "Không đọc được kết quả (%s)." % type(exc).__name__
            self.root.after(0, lambda: done(letters, error))

        threading.Thread(target=work, daemon=True).start()

    def _start_url(self):
        """URL mở đầu do người dùng nhập; sai/không hợp lệ thì về mặc định."""
        try:
            url = self.start_url_var.get().strip()
        except AttributeError:
            return BR.START_URL
        if url and BR.valid_site_url(url):
            return url
        return BR.START_URL

    def start_browser(self):
        if self.chrome_busy or self.exit_requested:
            return
        self.chrome_busy = True
        self.connection_text = "Đang mở Chrome và ghép driver tương thích…"
        cdp = bool(self.cdp_var.get())
        window = self._window_config()
        pos = self._window_pos(window)
        def work():
            try:
                # Khi CDP bật, ưu tiên ghép tab Chrome/profile đang sống. Không mở WebDriver
                # thứ hai lên cùng profile vì launch token của EVO có thể chỉ dùng một lần.
                attached = cdp and self.browser.attach()
                if (cdp and not attached
                        and self.browser.last_diag.get("reason") == "evo_entry_auth_error"):
                    self.events.put(("open_error", self.browser.note))
                    return
                if not attached and not self.browser.alive() and not self.browser.start(
                        self._start_url(), pos=pos, cdp=cdp, window=window):
                    self.events.put(("open_error", self.browser.last_error))
                    return
                size_note = self.browser.resize_window(window, pos=pos)[1]
                # Chrome do driver mở tự tạo cổng 9222; cổng chưa lên thì mọi lượt quét
                # sảnh sẽ thất bại im lặng, nên phải nói rõ ngay từ dòng kết nối.
                port_note = ""
                if cdp and not attached and not self.browser.wait_for_cdp():
                    port_note = (" · Chrome đã mở nhưng cổng 9222 chưa lên nên chưa quét được cả sảnh, "
                                 "app chỉ đọc cầu trên màn hình. Bấm “Mở Chrome” lại sau khi tắt hết "
                                 "cửa sổ Chrome khác.")
                self.events.put(("opened", "%s · %s%s · Đăng nhập và tự vào sảnh Baccarat, "
                                            "rồi bấm “Nhận tab sảnh”." % (
                                                self.browser.driver_info, size_note, port_note)))
            except Exception as exc:
                self.events.put(("open_error", str(exc).splitlines()[0][:240]))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def attach_browser(self):
        if self.chrome_busy or self.exit_requested:
            return
        self.chrome_busy = True
        self.connection_text = "Đang kết nối Chrome hiện có qua CDP 9222; không mở lại trang…"
        window = self._window_config()
        pos = self._window_pos(window)
        def work():
            try:
                ok = self.browser.attach()
                if ok:
                    self.browser.resize_window(window, pos=pos)
                self.events.put(("opened" if ok else "open_error", self.browser.note))
            except Exception as exc:
                self.events.put(("open_error", "Không kết nối được Chrome (%s)." % type(exc).__name__))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def _cdp_available(self):
        if self.browser.cdp_attached:
            return True
        if self.browser.driver is None or not self.cdp_var.get():
            return False
        try:
            return self.browser._cdp_page() is not None
        except Exception:
            return False

    def enter_lobby(self):
        if self.chrome_busy or self.sweep_busy or self.exit_requested:
            return
        if not self._cdp_available():
            self.sweep_note_text = ("Chưa bật CDP: vào trang Baccarat trong cửa sổ Chrome rồi gắn Chrome "
                                    "đang mở để app nhận tab."
                                    if self.browser.driver is not None else "Gắn Chrome đang mở trước.")
            self.refresh()
            return
        self.sweep_busy = True
        self.session_lost = False  # người dùng chủ động nhận lại tab sau khi đăng nhập
        self.recover_attempts = 0
        self.sweep_note_text = "Đang tìm tab sảnh Baccarat bạn đã tự mở…"
        def work():
            try:
                ok, note = self.stream.find_lobby_tab()
                self.events.put(("lobby_entered", (ok, note)))
            except Exception as exc:
                self.events.put(("lobby_entered", (False, "Không nhận được tab sảnh (%s)." % type(exc).__name__)))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def sweep_lobby(self):
        if self.chrome_busy or self.sweep_busy or self.exit_requested:
            return
        if not self._cdp_available():
            self.sweep_note_text = "Gắn Chrome đang mở trước."
            self.refresh()
            return
        self.sweep_busy = True
        favorites = self.favorites_only.get()
        self._last_sweep_favorites = favorites
        self.sweep_note_text = ("Đang quét sảnh · chỉ bàn Ưa thích…" if favorites
                                else "Đang quét sảnh…")
        def work():
            try:
                rows = self.stream.sweep(favorites_only=favorites)
                self.events.put(("sweep", (rows, self.stream.last_error, self.stream.diag, time.time())))
            except Exception as exc:
                self.events.put(("sweep", ([], type(exc).__name__, {}, time.time())))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def stop_browser(self):
        if self.chrome_busy:
            return
        self.sim.pause("Đã ngắt kết nối.")
        self.chrome_busy = True
        self.connection_text = "Đang ngắt kết nối…"
        def work():
            self.events.put(("stopped", self.browser.stop()))
        threading.Thread(target=work, daemon=True).start()

    def _tab_snapshot(self):
        """Liệt kê an toàn các tab đang mở để vết chẩn đoán giải thích được lần mất phiên."""
        try:
            pages = self.browser._cdp_pages() or []
        except Exception as exc:
            return [{"kind": "error", "url": type(exc).__name__}]
        rows = []
        for page in pages:
            url = page.get("url", "") if isinstance(page, dict) else ""
            rows.append(dict(
                kind=("lobby" if BR.is_lobby_url(url) else "portal" if BR.is_portal_url(url)
                      else "entry" if BR.is_evo_entry_url(url) else "other"),
                title=str((page or {}).get("title") or "")[:40], url=BR._safe_url(url)))
        return rows

    def request_scan(self):
        self.next_scan = 0
        if not self.browser.connected:
            self.connection_text = "Hãy mở Chrome trước khi đọc dữ liệu."
        self.refresh()

    def _scan(self):
        self.scan_busy = True
        def work():
            try:
                reports = self.browser.scan()
                self.events.put(("scan", (reports, self.browser.last_error, time.time())))
            except Exception as exc:
                self.events.put(("scan", ([], str(exc).splitlines()[0][:180], time.time())))
        threading.Thread(target=work, daemon=True).start()
