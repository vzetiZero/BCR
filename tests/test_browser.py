# -*- coding: utf-8 -*-
"""Kiểm thử phát hiện Chrome đã bị đóng (liveness) — không cần Chrome thật.

Lỗi từng gặp: mở Chrome rồi tự tay đóng cửa sổ, nhưng app vẫn coi là "đang kết nối"
nên nút mở Chrome bị vô hiệu và không có cách mở lại.
"""

import unittest
from unittest.mock import patch

from bcr.adapters import browser as BR


class ChromeLivenessTests(unittest.TestCase):
    def session(self):
        session = BR.BrowserSession()
        session.cdp_attached = True
        session.driver = object()          # phiên cũ còn sót sau khi Chrome bị đóng
        session.mode = "cdp"
        session._alive_checked_at = 0.0
        return session

    def test_fresh_session_is_disconnected(self):
        self.assertFalse(BR.BrowserSession().connected)

    def test_dead_chrome_is_reported_and_state_is_cleared(self):
        session = self.session()
        with patch.object(BR.BrowserSession, "_probe_alive", return_value=False):
            self.assertFalse(session.connected)
        self.assertFalse(session.cdp_attached)
        self.assertIsNone(session.driver)
        self.assertEqual(session.mode, "")
        self.assertEqual(session.connection_state, "closed")
        self.assertIn("Mở Chrome", session.note)

    def test_alive_chrome_keeps_the_session(self):
        session = self.session()
        with patch.object(BR.BrowserSession, "_probe_alive", return_value=True):
            self.assertTrue(session.connected)
        self.assertTrue(session.cdp_attached)
        self.assertEqual(session.mode, "cdp")

    def test_liveness_result_is_cached(self):
        session = self.session()
        with patch.object(BR.BrowserSession, "_probe_alive", return_value=True) as probe:
            for _ in range(5):
                self.assertTrue(session.connected)
        self.assertEqual(probe.call_count, 1, "không dò HTTP mỗi lần UI lấy state")

    def test_cache_expires_and_detects_the_close(self):
        session = self.session()
        with patch.object(BR.BrowserSession, "_probe_alive", return_value=True):
            self.assertTrue(session.connected)
        session._alive_checked_at = 0.0     # giả lập đã quá hạn cache
        with patch.object(BR.BrowserSession, "_probe_alive", return_value=False):
            self.assertFalse(session.connected)

    def test_mark_closed_resets_everything(self):
        session = self.session()
        session.mark_closed()
        self.assertFalse(session.connected)
        self.assertEqual(session.connection_state, "closed")
        self.assertIn("Mở Chrome", session.note)

    def test_after_close_it_can_attach_again(self):
        """Sau khi bị đóng, phiên phải về trạng thái sạch để attach/start lại được."""
        session = self.session()
        with patch.object(BR.BrowserSession, "_probe_alive", return_value=False):
            self.assertFalse(session.connected)
        self.assertIsNone(session.driver)
        self.assertFalse(session.cdp_attached)
        self.assertEqual(session._alive_ok, False)


class ProbeTests(unittest.TestCase):
    def test_probe_true_when_cdp_port_is_open(self):
        session = BR.BrowserSession()
        session.cdp_attached = True
        with patch.object(BR.BrowserSession, "_port_open", return_value=True):
            self.assertTrue(session._probe_alive())

    def test_probe_false_when_port_is_closed(self):
        session = BR.BrowserSession()
        session.cdp_attached = True
        with patch.object(BR.BrowserSession, "_port_open", return_value=False):
            self.assertFalse(session._probe_alive())

    def test_probe_true_when_driver_still_answers(self):
        session = BR.BrowserSession()
        session.driver = type("Driver", (), {"window_handles": ["h1"]})()
        with patch.object(BR.BrowserSession, "_port_open", return_value=False):
            self.assertTrue(session._probe_alive(), "WebDriver còn sống thì vẫn coi là kết nối")

    def test_dead_driver_is_dropped(self):
        session = BR.BrowserSession()

        class DeadDriver:
            @property
            def window_handles(self):
                raise RuntimeError("chrome has gone away")

        session.driver = DeadDriver()
        with patch.object(BR.BrowserSession, "_port_open", return_value=False):
            self.assertFalse(session._probe_alive())
        self.assertIsNone(session.driver)

    def test_tcp_probe_is_fast_and_reports_closed_port(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]
        self.assertFalse(BR.BrowserSession._port_open(free_port, timeout=0.4))


if __name__ == "__main__":
    unittest.main(verbosity=2)
