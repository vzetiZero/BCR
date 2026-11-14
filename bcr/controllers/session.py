# -*- coding: utf-8 -*-
"""Tầng Controller: sức khoẻ phiên (mất đăng nhập, tự hồi phục) và sổ ví.

Đọc báo cáo quét, phát hiện phiên chết, đồng bộ số dư và ghi trace JSONL.
"""
import json
import re
import threading
import time


from bcr.config import *
from bcr.models import money as M


class SessionMixin:
    """Giữ phiên sống: phát hiện mất đăng nhập, hồi phục, đồng bộ ví, ghi trace."""

    def _trace(self, kind, detail=None):
        """Một dòng nhật ký thô (không token, không cookie) làm bằng chứng cho lần chạy thật."""
        try:
            row = dict(ts=round(time.time(), 3), at=time.strftime("%H:%M:%S"), kind=kind,
                       note=str(self.sweep_note_text or "")[:180], tabs=self._tab_snapshot())
            if detail:
                row["detail"] = detail
            with open(self.trace_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False)[:2400] + "\n")
        except Exception:
            pass

    def _auth_blame(self):
        """Sảnh đang báo mất phiên thật, chứ không chỉ chưa tải xong."""
        if getattr(self.stream, "session_lost", False):
            return True
        diag = getattr(self.browser, "last_diag", None)
        return isinstance(diag, dict) and diag.get("reason") == "authentication_required"

    def _session_lost_step(self, note):
        """Mất phiên: dừng vòng tự quét rồi (nếu bật) tự mở lại EVO để phục hồi.

        Không bấm tiếp lên trang lỗi EV.7; thay vào đó mở lại sảnh từ trang gốc NEO79.
        Nếu không bật tự phục hồi thì chỉ dừng và báo hướng dẫn như cũ.
        """
        self.session_lost = True
        self.cycle_waiting = False
        self.cycle_attempts = 0
        self.cycle_retry_at = 0.0
        self.cycle_table_key = None
        self.auto_start_table_key = None
        self.sim.pause("Sảnh báo phiên Evolution không còn hợp lệ. Đăng nhập lại trang gốc, "
                       "bấm thẻ Evolution một lần, rồi bấm “Nhận tab sảnh”.")
        self.sweep_note_text = note
        self._trace("session_lost")
        self.refresh()
        recover = getattr(self, "_try_recover_session", None)
        if recover is not None:
            recover()

    def _try_recover_session(self):
        """Tự phục hồi phiên bị văng: mở lại sảnh EVO từ trang gốc rồi quét tiếp.

        Có giới hạn số lần thử liên tiếp để không lặp vô hạn khi site vẫn đá phiên;
        chỉ chạy khi bật “Tự phục hồi phiên” và chưa có lần phục hồi đang chạy.
        """
        recover_var = getattr(self, "auto_recover_var", None)
        enabled = bool(recover_var.get()) if recover_var is not None else False
        if not enabled or self.recover_busy or self.exit_requested:
            return
        if self.recover_attempts >= 3:
            self.sweep_note_text = ("Đã tự mở lại EVO 3 lần nhưng phiên vẫn bị văng. Hãy đăng nhập "
                                    "lại trang gốc rồi bấm “Nhận tab sảnh”.")
            self.refresh()
            return
        self.recover_busy = True
        self.recover_attempts += 1
        self.sweep_note_text = ("Phát hiện văng phiên · đang tự mở lại EVO (lần %d/3)…"
                                % self.recover_attempts)
        self._trace("recover_attempt", dict(attempt=self.recover_attempts))

        def work():
            try:
                ok, note = self.stream.open_lobby_from_portal()
                if not ok:
                    # Nút “Chơi game” không mở được thường vì trang vào cũng đã mất phiên;
                    # tải lại trang vào (an toàn) để lấy launch token mới rồi bấm lại một lần.
                    reloaded, reload_note = self.stream.reload_portal()
                    if reloaded:
                        ok, note = self.stream.open_lobby_from_portal()
                    else:
                        note = reload_note or note
            except Exception as exc:
                ok, note = False, "Không mở lại được EVO (%s)." % type(exc).__name__
            self.events.put(("recovered", (ok, note)))

        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def _on_recovered(self, ok, note):
        """Xử lý kết quả một lần tự phục hồi phiên."""
        self.recover_busy = False
        if ok:
            self.session_lost = False
            self.recover_attempts = 0
            self.sweep_note_text = "Đã mở lại EVO · quét sảnh để tìm bàn…"
            self._trace("recovered")
            self.sweep_lobby()
        else:
            self.sweep_note_text = "Tự phục hồi chưa xong: " + note
            self._trace("recover_failed", dict(note=note))
            # Chờ đúng nhịp nghỉ như vòng quét thường rồi mới thử lại; gọi đệ quy ngay
            # sẽ đốt hết 3 lượt trong vài giây khi site còn đang đá phiên.
            self.cycle_waiting = True
            self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
        self.refresh()

    def _sync_wallet_from_reports(self, reports):
        """Show the live lobby wallet and use it as starting capital before a run."""
        text = next((str((report.get("page") or {}).get("wallet_balance") or "").strip()
                     for report in reports
                     if (report.get("page") or {}).get("wallet_balance")), "")
        if not text:
            if getattr(self, "_wallet_manual_sync_pending", False):
                self.wallet_balance_text = "Số dư ví trên web: chưa đọc được giá trị để cập nhật."
                if hasattr(self, "wallet_balance_label"):
                    self.wallet_balance_label.config(text=self.wallet_balance_text, fg=RED)
            return
        try:
            value = M.parse_money(text)
        except (TypeError, ValueError):
            self.wallet_balance_text = "Số dư ví trên web: không đọc được giá trị"
            if hasattr(self, "wallet_balance_label"):
                self.wallet_balance_label.config(text=self.wallet_balance_text, fg=RED)
            return

        self.wallet_balance_text = "Số dư ví trên web: %s VN2" % M.fmt_amount(value)
        book = self.sim.money
        startup_reset = (getattr(self, "_wallet_startup_sync_pending", False)
                         and not getattr(self, "state_read_error", False))
        if getattr(self, "_wallet_manual_sync_pending", False):
            # Người dùng bấm "Cập nhật số dư": dời vốn phiên theo ví thật ngay cả giữa
            # phiên. Giữ thống kê tay; chỉ đặt lại mốc vốn/số dư và mở lại ngưỡng dừng.
            self._wallet_manual_sync_pending = False
            self._wallet_startup_sync_pending = False
            ok, note = book.rebase_to_wallet(value)
            if ok:
                self.wallet_balance_text += " · đã cập nhật vốn phiên theo số dư ví"
                if hasattr(self, "entries") and "capital" in self.entries:
                    self.entries["capital"].set(M.fmt_input_money(value))
                if hasattr(self, "_save_state"):
                    self._save_state()
            else:
                self.wallet_balance_text += " · " + note
        elif startup_reset:
            # A reopened app starts a fresh ledger from the live wallet. Archive
            # the previous session first, then clear its P/L and hand counters.
            try:
                self._archive_state()
            except OSError:
                pass
            self.sim.money = M.MoneyBook(
                capital=value, unit=book.unit, mode=book.mode, percent=book.percent,
                take_profit_pct=book.take_profit_pct, mtg_cap=book.mtg_cap)
            self.sim.log = []
            self.history_size = -1
            self.tables_done = 0
            self._wallet_startup_sync_pending = False
            self.wallet_balance_text += " · vốn phiên mới lấy từ số dư ví; lãi/lỗ đã đặt lại"
            if hasattr(self, "entries") and "capital" in self.entries:
                self.entries["capital"].set(M.fmt_input_money(value))
            if hasattr(self, "_save_state"):
                self._save_state()
        elif not self.sim.running and not book.hands_played and not getattr(self, "state_read_error", False):
            changed = abs(book.start_capital - value) >= 0.005
            ok, _note = book.sync_capital(value)
            if ok:
                self.wallet_balance_text += " · vốn phiên đã đồng bộ"
                if hasattr(self, "entries") and "capital" in self.entries:
                    self.entries["capital"].set(M.fmt_input_money(value))
                if changed and hasattr(self, "_save_state"):
                    self._save_state()
        else:
            self.wallet_balance_text += " · vốn phiên đang giữ nguyên"
        if hasattr(self, "wallet_balance_label"):
            self.wallet_balance_label.config(text=self.wallet_balance_text, fg=ACCENT)

    def _ingest_reports(self, reports, now=None):
        now = time.time() if now is None else now
        incoming = {}
        for report in reports:
            for raw in report.get("tables", []):
                letters, label = raw.get("letters", ""), " ".join(raw.get("label", "").split())[:80]
                if not label or not re.fullmatch(r"[BPT]{0,400}", letters):
                    continue
                table_id = raw.get("table_id", "")
                if not isinstance(table_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", table_id):
                    table_id = ""
                key = "id:" + table_id if table_id else re.sub(r"\s+", "", label).casefold()
                previous = incoming.get(key)
                old = self.tables.get(key)
                # Màn hình bàn không có tên trong DOM: giữ tên đẹp đã đọc từ sảnh cho cùng mã bàn.
                if old and (not label or label == table_id) and old["label"] != table_id:
                    label = old["label"]
                conflict = bool(previous and (previous["letters"] != letters or previous["conflict"]))
                incoming[key] = dict(key=key, label=label, letters=letters, ts=now, table_id=table_id,
                                     game_type=raw.get("game_type", ""), read_error=raw.get("read_error", ""),
                                     source=raw.get("source", ""), conflict=conflict)
                if raw.get("road_match") is True:
                    incoming[key]["ws_confirmed_letters"] = letters
                elif old:
                    confirmed = old.get("ws_confirmed_letters", "")
                    # Đổi shoe hoặc cầu bị viết lại: mất bằng chứng hai nguồn cùng khớp.
                    incoming[key]["ws_confirmed_letters"] = confirmed if (
                        letters.startswith(confirmed) or confirmed.startswith(letters)) else ""
        for key, row in incoming.items():
            old = self.tables.get(key)
            row["stable"] = old["stable"] + 1 if old and old["letters"] == row["letters"] and not (
                old["conflict"] or row["conflict"] or now - old["ts"] > LIVE_STALE) else 1
            row["screen_stable"] = (old.get("screen_stable", 0) + 1
                                    if row["source"] == "evo-bead" and old
                                    and old.get("source") == "evo-bead"
                                    and old["letters"] == row["letters"] else
                                    1 if row["source"] == "evo-bead" else 0)
            self.tables[key] = row
        self.tables = {k: r for k, r in self.tables.items() if now - r["ts"] < 300 or k == self.sim.table_key}

    def request_wallet_sync(self):
        """Nút "Cập nhật số dư": đọc lại ví web rồi dời vốn phiên theo số dư thật mới nhất.

        Nhãn số dư vẫn tự cập nhật mỗi lần đọc, nhưng vốn phiên (số dư dùng để đặt cược
        và tính % chốt lời) chỉ đổi khi người dùng bấm nút này — tránh tự reset lãi/lỗ
        giữa phiên. Đọc tươi qua một lượt scan rồi áp dụng trong ``_sync_wallet_from_reports``.
        """
        if not self.browser.connected:
            self.wallet_balance_text = "Số dư ví trên web: chưa đọc được · hãy gắn Chrome trước."
            self.wallet_balance_label.config(text=self.wallet_balance_text, fg=RED)
            return
        self._wallet_manual_sync_pending = True
        self.wallet_balance_text = "Số dư ví trên web: đang đọc lại để cập nhật vốn…"
        self.wallet_balance_label.config(text=self.wallet_balance_text, fg=ACCENT)
        self.request_scan()
