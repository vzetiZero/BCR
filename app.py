# -*- coding: utf-8 -*-
import csv
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import browser as BR
import engine as E
import lobby_stream as LS
import method as ME
import money as M
import table_bets as TB

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "bcr_state.json"
SCAN_SECONDS = 4
FAST_SCAN = 1.5
LIVE_STALE = 30
ENTRY_WAIT = 75
# Vòng tự tìm bàn không có trần số lần: nghỉ CYCLE_RETRY_SECONDS giữa hai lượt quét.
CYCLE_RETRY_SECONDS = 15
CHIP_REFRESH = 3
# Gate "chờ ván mới": sau khi chốt kế hoạch, chờ tối đa BET_GATE_TIMEOUT giây để bàn
# dọn hết chip (cả 3 cửa về 0) rồi mới tự bấm — tránh bấm chồng lên ván cũ còn
# đang hiển thị/gom chip. BET_GATE_POLL là nhịp đọc lại trong lúc chờ.
BET_GATE_TIMEOUT = 20.0
BET_GATE_POLL = 0.3
# Vào bàn từ sảnh mất vài giây tải trang, khay chip/ô cược chưa kịp hiện nên lượt bấm
# đầu thường hỏng. Tay chưa đặt được chip thật thì không được tính (xem
# LiveSimulation.mark_placed), nhưng bỏ quá nhiều tay liên tiếp nghĩa là bàn đang đặt
# không được chút nào — dừng để người dùng nhìn thấy thay vì bỏ tay mãi.
PLACE_SKIP_LIMIT = 3
# Trần thời gian một lượt bấm chip còn được coi là "đang chạy" (gate chờ ván mới 20s +
# vài giây bấm/đọc chip). Quá hạn mà chưa có xác nhận thì coi như không đặt được — nhờ
# vậy thread chết giữa chừng không khiến việc tính tay bị treo vĩnh viễn.
PLACE_CONFIRM_TIMEOUT = 60.0
BG, PANEL, NAV = "#f3f6fa", "#ffffff", "#111e32"
TEXT, MUTED, LINE = "#172b45", "#718198", "#e5ebf2"
ACCENT, PALE = "#087f78", "#e8f5f2"
RED, BLUE, GREEN = "#d94d62", "#3f7edf", "#16866b"
COLORS = {"B": RED, "P": BLUE, "T": GREEN}
MONEY_FIELDS = ("balance", "hands_played", "wins", "losses", "pushes", "ties_bet",
                "total_staked", "total_returned", "peak", "max_drawdown", "loss_streak", "blocked")
DEFAULT_UI_SCALE = 80


def fresh_method():
    return {"started": False, "ladder_step": 0, "hands_in_block": 0, "strong": None}


def signed(value):
    return ("+" if value > 0 else "") + M.fmt_money(value)


def group_money_input(raw, caret):
    """Nhóm lại ô nhập tiền theo dấu phẩy hàng nghìn và giữ vị trí con trỏ tương ứng."""
    raw = str(raw)
    separators = [char for char in raw if char in ",."]
    decimal = ""
    if "," in raw and "." in raw:
        decimal = "." if raw.rfind(".") > raw.rfind(",") else ","
    elif separators:
        sep = separators[0]
        pieces = raw.split(sep)
        if len(pieces) == 2 and (1 <= len(pieces[1]) <= 2):
            decimal = sep
        elif len(pieces) > 2 and len(pieces[-1]) <= 2 and all(len(p) == 3 for p in pieces[1:-1]):
            decimal = sep
    if decimal:
        whole, fraction = raw.rsplit(decimal, 1)
        whole_digits = re.sub(r"\D", "", whole)
        fraction = re.sub(r"\D", "", fraction)
        grouped_whole = M.fmt_input_money(int(whole_digits)) if whole_digits else "0"
        grouped = grouped_whole + "." + fraction
    else:
        whole_digits = re.sub(r"\D", "", raw)
        fraction = ""
        grouped = M.fmt_input_money(int(whole_digits)) if whole_digits else ""
    if grouped == raw:
        return raw, caret
    kept = len(re.sub(r"\D", "", raw[:caret]))
    if decimal and raw.rfind(decimal) < caret and kept >= len(whole_digits):
        return grouped, min(len(grouped), len(M.fmt_input_money(int(whole_digits)) if whole_digits else "0")
                            + 1 + max(0, kept - len(whole_digits)))
    if kept >= len(whole_digits) + len(fraction):
        return grouped, len(grouped)
    seen = 0
    for index, char in enumerate(grouped):
        if char.isdigit():
            seen += 1
            if seen == kept + 1:
                return grouped, index
    return grouped, len(grouped)


def road_is_current(row):
    """Cầu đủ tin để chọn bàn: sảnh - hai nguồn (cầu đang vẽ + lịch sử WebSocket) cùng cho một chuỗi."""
    if not row or row.get("conflict") or row.get("read_error") or not row.get("letters"):
        return False
    if row.get("source") == "evo-bead":
        return row.get("screen_stable", 0) >= 2
    return row.get("ws_confirmed_letters") == row["letters"] and row.get("stable", 0) >= 2


def same_shoe(entry_letters, screen_letters):
    """Cầu đã lọc ở sảnh và cầu trong bàn là một shoe: phần trùng nhau phải khớp đầu hoặc khớp cuối."""
    if not entry_letters or not screen_letters:
        return False
    shared = min(len(entry_letters), len(screen_letters))
    return (entry_letters[:shared] == screen_letters[:shared]
            or entry_letters[-shared:] == screen_letters[-shared:])


def row_problem(row, now=None, check_fresh=True):
    now = time.time() if now is None else now
    if not row:
        return "Chưa có dữ liệu bàn. Mở Chrome, đăng nhập và chọn một bàn."
    if check_fresh and now - row["ts"] > LIVE_STALE:
        return "Dữ liệu quá %d giây chưa cập nhật. Đã dừng để tránh tính sai." % LIVE_STALE
    if row.get("conflict"):
        return "Có nhiều cầu khác nhau cùng tên bàn. Cần đối chiếu lại."
    if row.get("read_error"):
        return row["read_error"]
    if row.get("game_type") and row["game_type"] != "baccarat":
        return "Bàn trả thưởng đặc biệt: chưa hỗ trợ tính lãi/lỗ theo bàn chuẩn."
    if row.get("stable", 0) < 2:
        return "Chờ hai lần đọc giống nhau để xác nhận dữ liệu."
    if not row.get("table_id") and (row["label"].lower().startswith("ban ") or not re.search(r"\d", row["label"])):
        return "Chưa nhận diện rõ mã bàn. Không thể bắt đầu theo dõi."
    return ""


class LiveSimulation:
    def __init__(self, book=None, profile=ME.DEFAULT_PROFILE, ladder=None,
                 min_hands=ME.MIN_HANDS, max_hands=ME.MAX_HANDS,
                 max_streak=ME.MAX_STREAK_ALLOWED,
                 live_exit_streak=ME.DEFAULT_LIVE_EXIT_STREAK,
                 block_mode=ME.BLOCK_MODE_RECALCULATE):
        self.money = book or M.MoneyBook()
        self.profile = profile
        self.ladder = ME.ladder_stakes(profile, ladder)
        self.min_hands, self.max_hands = ME.normalize_hand_range(min_hands, max_hands)
        self.max_streak = ME.normalize_streak(max_streak)
        self.live_exit_streak = ME.normalize_streak(live_exit_streak, ME.DEFAULT_LIVE_EXIT_STREAK)
        self.block_mode = (ME.BLOCK_MODE_REPEAT if block_mode == "alternate" else block_mode)
        self.block_mode = (self.block_mode if self.block_mode in
                           (ME.BLOCK_MODE_RECALCULATE, ME.BLOCK_MODE_REPEAT)
                           else ME.BLOCK_MODE_RECALCULATE)
        self.results = []
        self.log = []
        self.method_state = fresh_method()
        self.table_key = None
        self.table_name = "Chưa chọn bàn"
        self.letters = ""
        self.running = False
        self.entry_waiting = False
        self.pending = None
        self.note = "Chọn bàn để theo dõi."
        self.block_hands = 0
        self.place_skips = 0
        self.cycle_done = False
        self.filter_exit_done = False
        # Chỉ rời bàn khi đã chạm mục tiêu lãi của phiên; hết một khối 3-3/3-2
        # không còn là lý do rời bàn nữa.
        self.take_profit_done = False
        self.on_arm = None

    @property
    def cycle_hands(self):
        """Số tay của một khối theo phương pháp đang chọn: 6 tay cho 3-3, 5 tay cho 3-2."""
        return ME.block_hands(self.profile)

    def set_method(self, profile, ladder):
        self.profile = profile
        self.ladder = ME.ladder_stakes(profile, ladder)

    def plan(self):
        s = self.method_state
        return ME.plan(self.results, self.ladder, s["ladder_step"], s["hands_in_block"],
                       s["started"], s["strong"], self.profile,
                       self.min_hands, self.max_hands, self.max_streak, self.block_mode)

    def set_road_filter(self, min_hands, max_hands, max_streak):
        """Đổi bộ lọc cầu (khoảng tay đã đi + số tay bệt tối đa); kẹp trong biên của method."""
        self.min_hands, self.max_hands = ME.normalize_hand_range(min_hands, max_hands)
        self.max_streak = ME.normalize_streak(max_streak)
        return self.min_hands, self.max_hands, self.max_streak

    def set_live_exit_streak(self, live_exit_streak):
        self.live_exit_streak = ME.normalize_streak(live_exit_streak, ME.DEFAULT_LIVE_EXIT_STREAK)
        return self.live_exit_streak

    def bind(self, row):
        self.pause()
        self.table_key, self.table_name = row["key"], row["label"]
        self.letters = row["letters"]
        self.results = list(self.letters)
        self.method_state = fresh_method()
        self.block_hands = 0
        self.place_skips = 0
        self.cycle_done = False
        self.filter_exit_done = False
        self.take_profit_done = False
        self.entry_waiting = False
        self.note = "Đã chọn bàn."

    def start(self, row, verified=False, now=None):
        problem = row_problem(row, now)
        if problem:
            self.note = problem
            return False
        if row["key"] != self.table_key:
            self.note = "Hãy chọn bàn trước."
            return False
        if row["letters"] != self.letters:
            self.bind(row)
            self.note = "Cầu đã thay đổi trong lúc dừng. Đối chiếu và xác nhận lại."
            return False
        if not verified:
            self.note = "Cần xác nhận cầu và loại bàn Baccarat chuẩn trước khi theo dõi."
            return False
        if self.money.blocked:
            self.note = self.money.blocked[1]
            return False
        if self.on_arm is not None:
            initial_plan = self.plan()
            if initial_plan["action"] != "start":
                self.note = initial_plan["reason"]
                return False
        self.running = True
        if self.on_arm is not None:
            self.entry_waiting = True
            self.pending = None
            self.note = "Vừa vào bàn · đang chờ kết quả tay hiện tại rồi mới đặt cược."
        else:
            self._arm()
        return self.running

    def pause(self, reason="Đã tạm dừng. Không ghi thêm kết quả vào vốn."):
        self.running = False
        self.entry_waiting = False
        self.pending = None
        self.note = reason

    def arm_at_entry(self, row):
        """Begin an entry wait; arm only after the first new stable table result."""
        problem = row_problem(row)
        if problem:
            self.note = problem
            return False
        if self.money.blocked:
            self.note = self.money.blocked[1]
            return False
        self.bind(row)
        self.running = True
        self.entry_waiting = True
        self.pending = None
        self.note = "Vừa vào bàn · đang chờ kết quả tay hiện tại rồi mới đặt cược."
        return self.running

    def _arm(self):
        plan = self.plan()
        if plan["action"] not in ("start", "bet"):
            self.pause(plan["reason"])
            return
        if plan["stake"] > self.money.balance:
            self.pause("Vốn không đủ cho bậc tiếp theo. Không tự giảm tiền cược.")
            return
        self.pending = dict(plan, armed_at=time.time(), baseline=self.letters)
        self.note = "Chờ %s · %s VN2 · ván kế tiếp." % (
            E.SIDE_NAME[plan["side"]], M.fmt_money(plan["stake"]))
        if self.on_arm is not None:
            # Truyền đúng object đang nằm ở self.pending để lượt bấm chip sau đó ghi kết
            # quả vào chính kế hoạch này; kết quả về muộn không gán nhầm cho tay mới.
            self.on_arm(self.pending)

    def mark_placing(self, plan=None):
        """App báo: kế hoạch đang được bấm chip THẬT trên bàn. Từ lúc này, tay mới chỉ
        được tính khi có xác nhận đặt thành công qua ``mark_placed``."""
        plan = self.pending if plan is None else plan
        if plan is not None:
            plan["placed"] = "pending"
            plan["placing_at"] = time.time()

    def mark_placed(self, ok, note="", plan=None):
        """Ghi kết quả một lượt bấm chip vào kế hoạch đã sinh ra lượt đó.

        ``plan`` là chính dict mà ``_arm`` đưa cho hook; chỉ nhận khi nó vẫn là kế hoạch
        đang chờ (``self.pending``) và còn ở "pending". Thread đặt chip chạy song song
        với vòng đọc cầu nên kết quả của lượt cũ có thể về sau khi đã chốt kế hoạch mới;
        gán nhầm là một tay chưa bấm sẽ mang tiếng đã bấm.
        """
        target = self.pending if plan is None else plan
        if target is None or target is not self.pending or target.get("placed") != "pending":
            return False
        target["placed"] = "ok" if ok else "failed"
        target["place_note"] = note
        # Ưu tiên giờ bấm thật trong thread nền (placed_at); nếu không có (nhánh báo
        # sớm trên luồng UI) mới dùng giờ hiện tại.
        target["confirmed_at"] = target.get("placed_at") or time.time()
        return True

    def _skip_unplaced_hand(self, letters, plan):
        """Tay có chữ mới nhưng chưa đặt được chip: chỉ ăn chữ vào cầu đang theo dõi.

        Không đụng vốn, không đổi bậc cược, không tăng ``block_hands`` — phương pháp chỉ
        tiến khi có cược thật. Kế hoạch cho tay kế tiếp được chốt lại trên cùng bậc.
        """
        reason = plan.get("place_note") or "không xác nhận được lượt đặt chip."
        self.letters = letters
        self.results = list(letters)
        self.place_skips += 1
        self.log.append(dict(time=time.time(), armed_at=plan.get("armed_at"), table=self.table_name,
                             table_key=self.table_key,
                             hand=len(letters), result=letters[-1], bet=plan["side"], stake=0,
                             delta=0.0, outcome="not_placed", balance_after=self.money.balance,
                             block_pos=plan.get("block_pos"), ladder=plan.get("ladder_step"),
                             source="chip-not-placed"))
        if self._exit_if_table_filter_failed(letters):
            return False
        if self.place_skips >= PLACE_SKIP_LIMIT:
            self.pause("Đã bỏ %d tay liên tiếp vì không đặt được chip thật (%s). Dừng lại để "
                       "kiểm tra khay chip và ô cược trên bàn." % (self.place_skips, reason))
            return False
        self._arm()
        # _arm() đã ghi note của kế hoạch mới; ghi tiếp lý do bỏ tay để người dùng nhìn
        # thấy ngay tay này đã bị trượt và bàn đang chờ lượt bấm chip kế tiếp.
        self.note = "Tay %d không được tính vì chưa đặt được chip (%s). %s" % (
            len(letters), reason, self.note)
        return False

    def _exit_if_table_filter_failed(self, letters):
        """Stop following a live table as soon as its entry filters no longer hold."""
        info = ME.check_table(list(letters), self.min_hands, self.max_hands, ME.STREAK_UPPER)
        runs = E.run_lengths(letters)
        streak_side, streak_len = max(runs, key=lambda run: run[1], default=(None, 0))
        streak_check = info["checks"][1]
        streak_check["ok"] = streak_len < self.live_exit_streak
        if not streak_check["ok"]:
            streak_check["detail"] = "bệt %s %d tay (ngưỡng thoát %d tay)" % (
                E.SIDE_NAME[streak_side], streak_len, self.live_exit_streak)
        else:
            streak_check["detail"] = "bệt hiện tại %d tay; thoát khi đạt %d tay" % (
                streak_len, self.live_exit_streak)
        info["eligible"] = all(check["ok"] for check in info["checks"])
        failure = next((check["detail"] for check in info["checks"] if not check["ok"]), "")
        if not failure:
            return False
        self.filter_exit_done = True
        self.cycle_done = True
        self.pause("Bàn không còn đạt điều kiện lọc (%s) · rời bàn và quét bàn khác." % failure)
        return True

    def process(self, row, now=None):
        if not self.running:
            return False
        if not row or row["key"] != self.table_key:
            self.pause("Không còn đọc được bàn đang theo dõi. Hãy chọn lại bàn.")
            return False
        letters = row["letters"]
        problem = row_problem(row, now, check_fresh=False)
        if row.get("conflict") or row.get("read_error") or row.get("game_type", "baccarat") not in ("", "baccarat"):
            self.pause(problem)
            return False
        current_time = time.time() if now is None else now
        if current_time - row["ts"] > LIVE_STALE:
            if letters == self.letters:
                return False
            self.pause(row_problem(row, now))
            return False
        # Tay mới phải được đọc giống nhau hai lần mới tính; trong lúc chờ vẫn giữ cược đã chốt.
        if row.get("stable", 0) < 2:
            return False
        if problem:
            self.pause(problem)
            return False
        if letters == self.letters:
            return False
        # Không tính bù các tay đã biết kết quả bằng kế hoạch lập sau đó.
        if not letters.startswith(self.letters) or len(letters) != len(self.letters) + 1:
            self.pause("Cầu nhảy tay, đổi shoe hoặc không khớp. Đã dừng, không tính bù lãi/lỗ.")
            return False
        if self.entry_waiting:
            # The table may still be showing a closed betting window from before
            # the join. Consume this first stable result without settling a wager,
            # then arm against the newest road for the following hand.
            self.letters = letters
            self.results = list(letters)
            self.entry_waiting = False
            if self._exit_if_table_filter_failed(letters):
                return True
            self.note = "Vừa nhận kết quả mới sau khi vào bàn; chuẩn bị cược tay kế tiếp."
            self._arm()
            return True
        plan = self.pending
        if not plan or plan["baseline"] != self.letters:
            self.pause("Không có kế hoạch được chốt trước kết quả. Hãy xác nhận lại bàn.")
            return False
        # Vào bàn từ sảnh phải chờ trang tải, khay chip chưa kịp hiện nên lượt bấm đầu
        # hay trượt. Chưa có xác nhận đặt chip thật thì chưa được tính tay.
        if plan.get("placed") == "pending":
            if current_time - (plan.get("placing_at") or current_time) > PLACE_CONFIRM_TIMEOUT:
                plan["placed"] = "failed"
                plan["place_note"] = ("không có xác nhận đặt chip sau %d giây (lượt bấm có thể "
                                      "đã chết giữa chừng)." % int(PLACE_CONFIRM_TIMEOUT))
            else:
                # Chữ mới đã về mà chip chưa được xác nhận: giữ tay này lại, chờ nhịp poll
                # kế tiếp (xác nhận về thì tính, báo thất bại thì bỏ tay không đụng vốn).
                # Mốc thấy kết quả để đối chiếu giờ bấm thật: xác nhận muộn hơn nghĩa là
                # chip rơi sang cửa sổ tay sau, không được tính cho tay này.
                plan.setdefault("result_seen_at", current_time)
                self.note = "Chờ xác nhận đã đặt chip thật xong rồi mới tính tay này."
                return False
        if plan.get("placed") == "failed":
            return self._skip_unplaced_hand(letters, plan)
        if self.on_arm is not None and plan.get("placed") != "ok":
            plan["placed"] = "failed"
            plan["place_note"] = "không có xác nhận web đã đặt chip cho tay này."
            return self._skip_unplaced_hand(letters, plan)
        if (plan.get("result_seen_at")
                and (plan.get("confirmed_at") or 0) > plan["result_seen_at"]):
            # Xác nhận ok nhưng giờ bấm thật muộn hơn lúc kết quả đã về: chip thuộc cửa
            # sổ đặt cược của tay sau (lượt bấm trượt cửa sổ tay này). Settle ở đây là
            # tính lỗ oan, đẩy bậc cược và lấy kết quả tay chưa đặt làm tham chiếu.
            plan["placed"] = "failed"
            plan["place_note"] = ("chip chỉ được xác nhận sau khi kết quả tay này đã về "
                                  "(bấm sang cửa sổ tay sau) nên tay này không được tính.")
            return self._skip_unplaced_hand(letters, plan)
        self.place_skips = 0
        result = letters[-1]
        settled = self.money.settle(plan["side"], result, plan["stake"], E.PAYOUT)
        self.method_state, _ = ME.advance(self.method_state, result, plan["side"], plan["stake"],
                                          self.profile)
        self.method_state["strong"] = plan["strong"]
        self.letters = letters
        self.results = list(letters)
        self.log.append(dict(time=time.time(), armed_at=plan["armed_at"], table=self.table_name,
                             table_key=self.table_key,
                             hand=len(letters), result=result, bet=plan["side"], stake=plan["stake"],
                             delta=settled["delta"], outcome=settled["outcome"],
                             balance_after=self.money.balance, block_pos=plan["block_pos"],
                             ladder=plan["ladder_step"], source="live-driver"))
        if result != E.TIE:
            self.block_hands += 1
        if self.money.blocked:
            self.pause(self.money.blocked[1])
            if self.money.blocked[0] == "TAKE_PROFIT":
                # Đủ lãi mới rời bàn. App dùng cycle_done làm tín hiệu đưa tab về sảnh;
                # take_profit_done để app biết đây là bàn chốt lời (rời xong mới tính bàn).
                self.take_profit_done = True
                self.cycle_done = True
                self.note = ("Đã đạt mục tiêu lãi %.0f%% vốn (%s) · rời bàn." % (
                    self.money.take_profit_pct, M.fmt_money(self.money.profit)))
        else:
            if self._exit_if_table_filter_failed(letters):
                return True
            # Hết một khối 3-3/3-2 thì lặp khối tiếp theo ngay tại bàn này.
            # method.plan() tự tính lại bên mạnh và reset vị trí trong khối.
            self._arm()
        return True


def table_is_clear(live):
    """True nếu đọc chip cho biết cả 3 cửa (B/P/T) đang trống — suy luận "ván mới".

    Không có timer đặt cược thật để đọc (DOM Evolution chưa khảo sát phần này), nên
    dùng tín hiệu gián tiếp: ván trước kết thúc thì bàn gom hết chip về 0 trước khi
    mở cửa đặt cược ván kế tiếp. ``live`` chưa đọc được (``ok`` False/thiếu) thì
    không coi là "trống" — phải chờ đọc được rồi mới tự bấm.
    """
    if not isinstance(live, dict) or not live.get("ok"):
        return False
    spots = live.get("spots") or {}
    for side in ("B", "P", "T"):
        entry = spots.get(side) or {}
        if float(entry.get("amount") or entry.get("amount_vnd") or 0) > 0:
            return False
    return True


class BCRApp:
    def __init__(self, root, state_file=STATE_FILE, auto_connect=True):
        self.root, self.state_file = root, Path(state_file)
        self.sim = LiveSimulation()
        self.sim.on_arm = self.place_armed_bet
        self.ladder_store = {}
        self.ladder_shown_profile = None
        self.browser = BR.BrowserSession()
        self.tables = {}
        self.events = queue.Queue()
        self.chrome_busy = self.scan_busy = self.closing = self.exit_requested = False
        self.sweep_busy = self.open_table_busy = False
        self.auto_start_table_key = None
        self.auto_start_letters = ""
        self.auto_start_deadline = 0.0
        self.cycle_waiting = False
        self.cycle_attempts = 0
        self.cycle_retry_at = 0.0
        self.cycle_table_key = None
        # Số bàn đã chốt lời xong trong phiên này; đủ ``tables_var`` bàn thì dừng hẳn.
        self.tables_done = 0
        self.session_lost = False
        self.recover_busy = False
        self.recover_attempts = 0
        self.trace_file = self.state_file.with_name("bcr_trace.jsonl")
        self.chip_live = None
        self.chip_check = None
        self.chip_read_at = 0.0
        self.chip_busy = False
        self.place_busy = False
        self.stream = LS.LobbyStream(self.browser)
        self.sweep_note_text = "Quét sảnh để tìm bàn đạt 3 điều kiện."
        self.next_scan = 0
        self.poll_id = None
        self.state_read_error = False
        self.ui_scale = DEFAULT_UI_SCALE
        self.chrome_startup_var = tk.BooleanVar(value=False)
        # Mặc định BẬT: app tự mở Chrome bằng chromedriver kèm cổng 9222, nên mọi
        # đường đọc (WebSocket sảnh, bấm ô bàn) chạy được mà người dùng không phải
        # giữ hay gắn cửa sổ Chrome nào khác.
        self.cdp_var = tk.BooleanVar(value=True)
        self.start_url_var = tk.StringVar(value=BR.START_URL)
        self.auto_cycle_var = tk.BooleanVar(value=True)
        self.auto_hunt_var = tk.BooleanVar(value=True)
        self.tables_var = tk.StringVar(value="1")
        self.favorites_only = tk.BooleanVar(value=True)
        self.human_mouse_var = tk.BooleanVar(value=True)
        self.auto_place_var = tk.BooleanVar(value=True)
        self.auto_recover_var = tk.BooleanVar(value=True)
        self.ladder_mode_var = tk.StringVar(value="manual")
        self.ladder_base_var = tk.StringVar(value=M.fmt_input_money(ME.DEFAULT_LADDER[ME.DEFAULT_PROFILE][0]))
        self.hands_min_var = tk.StringVar(value=str(ME.MIN_HANDS))
        self.hands_max_var = tk.StringVar(value=str(ME.MAX_HANDS))
        self.streak_var = tk.StringVar(value=str(ME.MAX_STREAK_ALLOWED))
        self.live_exit_streak_var = tk.StringVar(value=str(ME.DEFAULT_LIVE_EXIT_STREAK))
        self.block_mode_var = tk.StringVar(value=ME.BLOCK_MODE_RECALCULATE)
        self._last_sweep_favorites = False
        self.chrome_fullscreen_var = tk.BooleanVar(value=BR.WIN_FULLSCREEN)
        self.chrome_w_var = tk.StringVar(value=str(BR.WIN_W))
        self.chrome_h_var = tk.StringVar(value=str(BR.WIN_H))
        self.chrome_scale_var = tk.StringVar(value=str(int(BR.WIN_SCALE * 100)))
        self.only_eligible = tk.BooleanVar(value=False)
        self.search = tk.StringVar()
        self.connection_text = "Chưa kết nối Chrome."
        self.wallet_balance_text = "Số dư ví trên web: chưa đọc được"
        self._wallet_startup_sync_pending = True
        self._wallet_manual_sync_pending = False
        self._load_state()
        self.stream.human_mouse = self.human_mouse_var.get()
        self.base_tk_scaling = float(root.tk.call("tk", "scaling"))
        root.tk.call("tk", "scaling", self.base_tk_scaling * self.ui_scale / 100)
        root.title("BCR · Theo dõi 3-3 / 3-2")
        root.configure(bg=BG)
        root.geometry("600x900+20+20")
        root.minsize(560, 620)
        self._build_ui()
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(500, self.poll)
        if auto_connect and self.chrome_startup_var.get():
            root.after(700, self.start_browser)

    def _label(self, parent, text="", size=10, color=TEXT, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kw)

    def _button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, bg=ACCENT if primary else PANEL,
                         fg="white" if primary else TEXT, activebackground=PALE,
                         activeforeground=ACCENT, relief="flat", bd=0, cursor="hand2",
                         font=("Segoe UI", 10, "bold"), padx=14, pady=8)

    def _card(self, parent):
        return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)

    def _build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=32, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=BG, foreground=MUTED,
                        relief="flat", font=("Segoe UI", 9, "bold"), padding=(8, 7))
        style.map("Treeview", background=[("selected", PALE)], foreground=[("selected", ACCENT)])
        # Thanh điều hướng ngang thay cho cột trái 170px: cửa sổ app chỉ còn ~600px
        # bề ngang để đặt cạnh Chrome trên cùng màn hình.
        nav = tk.Frame(self.root, bg=NAV)
        nav.pack(side="top", fill="x")
        self._label(nav, "BCR", 15, "white", True).pack(side="left", padx=(12, 6), pady=6)
        self.brand_method = self._label(nav, "", 15, "#63d6bc", True)
        self.brand_method.pack(side="left", padx=(0, 10))
        self.nav_buttons = {}
        for key, title in (("overview", "Tổng quan"), ("history", "Lịch sử"), ("settings", "Cài đặt")):
            button = tk.Button(nav, text=title, relief="flat", bd=0,
                               bg=NAV, fg="#b8c6d8", activebackground="#25364e", activeforeground="white",
                               font=("Segoe UI", 10), padx=12, pady=8, cursor="hand2",
                               command=lambda k=key: self.show_page(k))
            button.pack(side="left", padx=2, pady=4)
            self.nav_buttons[key] = button
        main = tk.Frame(self.root, bg=BG)
        main.pack(side="top", fill="both", expand=True, padx=10, pady=10)
        header = tk.Frame(main, bg=BG)
        header.pack(fill="x", pady=(0, 8))
        self.page_title = self._label(header, "Tổng quan", 15, bold=True)
        self.page_title.pack(side="left")
        self.hunt_banner = tk.Frame(main, bg=PALE, highlightbackground=ACCENT, highlightthickness=1)
        self.hunt_spinner = self._label(self.hunt_banner, "●", 13, ACCENT, True)
        self.hunt_spinner.pack(side="left", padx=(14, 8), pady=8)
        self.hunt_text = self._label(self.hunt_banner, "", 10, ACCENT, True, anchor="w", justify="left")
        self.hunt_text.pack(side="left", fill="x", expand=True, pady=8)
        self.hunt_cancel = self._button(self.hunt_banner, "Dừng tự quét", self.cancel_auto_hunt)
        self.hunt_cancel.pack(side="right", padx=10, pady=6)
        self._hunt_frame = 0
        connect = self._card(main)
        connect.pack(fill="x", pady=(0, 12))
        self.hunt_banner.pack(fill="x", pady=(0, 12), before=connect)
        self.hunt_banner.pack_forget()
        self._hunt_anchor = connect
        url_row = tk.Frame(connect, bg=PANEL)
        url_row.pack(fill="x", padx=10, pady=(8, 0))
        self._label(url_row, "Link", 9, MUTED).pack(side="left", padx=(0, 6))
        url_entry = tk.Entry(url_row, textvariable=self.start_url_var, font=("Segoe UI", 10),
                             relief="solid", bd=1)
        url_entry.pack(side="left", fill="x", expand=True, ipady=3)
        url_entry.bind("<FocusOut>", lambda _e: self._save_state())
        self.connection_label = self._label(connect, self._window_text(self._window_config()),
                                            9, bold=True, anchor="w")
        self.connection_label.pack(fill="x", padx=10, pady=(6, 0))
        row = tk.Frame(connect, bg=PANEL)
        row.pack(fill="x", padx=10, pady=(6, 2))
        self.open_button = self._button(row, "Mở Chrome", self.start_browser, True)
        self.open_button.pack(side="left")
        self.attach_button = self._button(row, "Gắn Chrome", self.attach_browser)
        self.attach_button.pack(side="left", padx=4)
        self._button(row, "Đọc lại", self.request_scan).pack(side="left", padx=4)
        self.disconnect_button = self._button(row, "Đóng", self.stop_browser)
        self.disconnect_button.pack(side="left", padx=4)
        self.connection_note = self._label(connect, size=9, color=MUTED, anchor="w", justify="left", wraplength=500)
        self.connection_note.pack(fill="x", padx=10, pady=(0, 6))
        wallet_row = tk.Frame(connect, bg=PANEL)
        wallet_row.pack(fill="x", padx=10, pady=(0, 6))
        self.wallet_balance_label = self._label(wallet_row, self.wallet_balance_text, 10, bold=True, anchor="w")
        self.wallet_balance_label.pack(side="left", fill="x", expand=True)
        self.wallet_sync_button = self._button(wallet_row, "Cập nhật số dư", self.request_wallet_sync)
        self.wallet_sync_button.pack(side="right", padx=(8, 0))
        sweep_row = tk.Frame(connect, bg=PANEL)
        sweep_row.pack(fill="x", padx=10, pady=(0, 6))
        self.enter_lobby_button = self._button(sweep_row, "Nhận tab sảnh", self.enter_lobby)
        self.enter_lobby_button.pack(side="left")
        self.sweep_button = self._button(sweep_row, "Quét sảnh", self.sweep_lobby)
        self.sweep_button.pack(side="left", padx=4)
        self.auto_open_button = self._button(sweep_row, "Tự mở bàn", lambda: self.auto_open_table(True))
        self.auto_open_button.pack(side="left", padx=4)
        tk.Checkbutton(sweep_row, text="Ưa thích", variable=self.favorites_only,
                       command=self._save_state, bg=PANEL, fg=TEXT, selectcolor=PALE,
                       activebackground=PANEL, activeforeground=ACCENT,
                       font=("Segoe UI", 9)).pack(side="left", padx=(4, 0))
        self.sweep_note = self._label(connect, "Bạn tự mở tab sảnh Baccarat trong Chrome rồi bấm “Nhận tab sảnh”. "
                                              "Vào bàn là app tự đánh, lặp khối tới khi đủ lãi mới đóng bàn.",
                                      9, MUTED, anchor="w", justify="left", wraplength=500)
        self.sweep_note.pack(fill="x", padx=10, pady=(0, 8))
        self.step_label = self._label(main, "", 9, MUTED, anchor="w", wraplength=520)
        self.step_label.pack(fill="x", pady=(0, 8))

        self.stack = tk.Frame(main, bg=BG)
        self.stack.pack(fill="both", expand=True)
        self.stack.rowconfigure(0, weight=1)
        self.stack.columnconfigure(0, weight=1)
        self.pages, self.page_hosts, self.page_scrolls = {}, {}, {}
        for key in self.nav_buttons:
            host = tk.Frame(self.stack, bg=BG)
            host.grid(row=0, column=0, sticky="nsew")
            self.page_hosts[key] = host
            if key == "history":
                self.pages[key] = host
                continue
            canvas = tk.Canvas(host, bg=BG, highlightthickness=0)
            scrollbar = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            page = tk.Frame(canvas, bg=BG)
            item = canvas.create_window(0, 0, anchor="nw", window=page)
            canvas.bind("<Configure>", lambda e, c=canvas, i=item: c.itemconfigure(i, width=e.width))
            page.bind("<Configure>", lambda e, c=canvas: c.configure(scrollregion=c.bbox("all")))
            self.pages[key] = page
            self.page_scrolls[key] = canvas
        self._build_overview(self.pages["overview"])
        self._build_history(self.pages["history"])
        self._build_settings(self.pages["settings"])
        self.root.bind("<MouseWheel>", self._scroll_page, add="+")
        self.show_page("overview")

    def _build_overview(self, parent):
        kpis = tk.Frame(parent, bg=BG)
        kpis.pack(fill="x", pady=(0, 10))
        self.kpi_values, self.kpi_notes = [], []
        for i, title in enumerate(("VỐN", "LÃI / LỖ", "ROI", "THẮNG / THUA")):
            kpis.columnconfigure(i, weight=1, uniform="kpi")
            card = self._card(kpis)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            self._label(card, title, 8, MUTED, True).pack(anchor="w", padx=8, pady=(8, 2))
            value = self._label(card, "—", 14, bold=True)
            value.pack(anchor="w", padx=8)
            note = self._label(card, "", 8, MUTED)
            note.pack(anchor="w", padx=8, pady=(2, 8))
            self.kpi_values.append(value)
            self.kpi_notes.append(note)
        # Cầu (B/P/T, Big Road) không hiển thị ở app nữa — xem trực tiếp trong Chrome.
        # Khối này chỉ còn đường vốn để theo dõi tiền; tên bàn hiện nhỏ ở góc làm ngữ cảnh.
        equity = self._card(parent)
        equity.pack(fill="x", pady=(0, 10))
        equity_head = tk.Frame(equity, bg=PANEL)
        equity_head.pack(fill="x", padx=10, pady=(10, 2))
        self._label(equity_head, "ĐƯỜNG VỐN", 8, MUTED, True).pack(side="left")
        self.road_title = self._label(equity_head, "Chưa chọn bàn", 9, MUTED, anchor="e")
        self.road_title.pack(side="right", fill="x", expand=True)
        self.canvases = {}
        canvas = tk.Canvas(equity, bg=PANEL, height=150, width=200, highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self.canvases["equity"] = canvas
        canvas.bind("<Configure>", lambda _e: self._draw_canvas("equity"))
        plan = self._card(parent)
        plan.pack(fill="x", pady=(0, 10))
        self._label(plan, "KẾ HOẠCH TAY TIẾP THEO", 8, MUTED, True).pack(anchor="w", padx=10, pady=(10, 2))
        self.plan_side = self._label(plan, "CHỜ BÀN", 18, bold=True)
        self.plan_side.pack(anchor="w", padx=10)
        self.plan_stake = self._label(plan, "", 10, MUTED)
        self.plan_stake.pack(anchor="w", padx=10, pady=(0, 5))
        self.cycle_strip = tk.Frame(plan, bg=PANEL)
        self.cycle_strip.pack(fill="x", padx=10)
        self.cycle_labels = []
        self._sync_cycle_strip()
        self.checks_label = self._label(plan, "", 9, MUTED, anchor="w", justify="left")
        self.checks_label.pack(fill="x", padx=10, pady=6)
        self.chip_title = self._label(plan, "CHIP TRÊN BÀN", 8, MUTED, True, anchor="w")
        self.chip_title.pack(fill="x", padx=10, pady=(2, 0))
        self.chip_status = self._label(plan, "Chưa đọc chip.", 9, MUTED, True, anchor="w", justify="left",
                                       wraplength=500)
        self.chip_status.pack(fill="x", padx=10)
        self.chip_detail = self._label(plan, "", 9, MUTED, anchor="w", justify="left", wraplength=500)
        self.chip_detail.pack(fill="x", padx=10, pady=(0, 4))
        self.place_status = self._label(plan, "", 9, MUTED, anchor="w", justify="left", wraplength=500)
        self.place_status.pack(fill="x", padx=10, pady=(0, 4))
        self.sim_note = self._label(plan, "", 9, MUTED, anchor="w", justify="left", wraplength=500)
        self.sim_note.pack(fill="x", padx=10, pady=(0, 10))
        table_card = self._card(parent)
        table_card.pack(fill="both", expand=True)
        toolbar = tk.Frame(table_card, bg=PANEL)
        toolbar.pack(fill="x", padx=10, pady=8)
        self.table_title = self._label(toolbar, "Bàn từ site · 0", 11, bold=True)
        self.table_title.pack(side="left")
        self._button(toolbar, "Chọn bàn", self.load_live_selected).pack(side="right")
        tk.Checkbutton(toolbar, text="Chỉ bàn đạt", variable=self.only_eligible, bg=PANEL,
                       activebackground=PANEL, font=("Segoe UI", 9), command=self._render_tables).pack(side="right", padx=8)
        entry = tk.Entry(toolbar, textvariable=self.search, relief="solid", bd=1,
                         font=("Segoe UI", 10), width=10)
        entry.pack(side="right", ipady=4)
        self.search.trace_add("write", lambda *_: self._render_tables())
        self.live_tree = self._tree(table_card, (("table", "TÊN BÀN", 140), ("hands", "TAY", 40),
                                    ("strong", "BÊN MẠNH", 70), ("rules", "ĐIỀU KIỆN", 160),
                                    ("data", "DỮ LIỆU", 90)), height=4)
        self.live_tree.tag_configure("ready", foreground=ACCENT)
        self.live_tree.tag_configure("wait", foreground=MUTED)
        self.live_tree.bind("<Double-1>", lambda _e: self.load_live_selected())
        self.empty_label = self._label(table_card, "Chưa đọc được bàn. Đăng nhập trong Chrome và mở sảnh Baccarat.",
                                       9, MUTED, anchor="w", wraplength=500)
        self.empty_label.pack(fill="x", padx=10, pady=8)

    def _tree(self, parent, columns, height=10):
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height,
                            selectmode="browse")
        for key, title, width in columns:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=width, minwidth=40, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return tree

    def _build_history(self, parent):
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        bar = tk.Frame(card, bg=PANEL)
        bar.pack(fill="x", padx=10, pady=10)
        self._label(bar, "Kết quả bàn", 12, bold=True).pack(side="left")
        self._button(bar, "Xuất CSV", self.export_csv).pack(side="right")
        self.history_note = self._label(card, "", 9, MUTED, anchor="w", justify="left", wraplength=500)
        self.history_note.pack(fill="x", padx=10, pady=(0, 10))
        self.history_tree = self._tree(card, (("time", "GIỜ", 55), ("table", "BÀN", 90),
                        ("hand", "TAY", 40), ("bet", "CƯỢC", 45), ("result", "KQ", 40),
                        ("stake", "TIỀN", 70), ("delta", "LÃI/LỖ", 70), ("balance", "VỐN", 85)))
        self.history_tree.configure(show="tree headings")
        self.history_tree.heading("#0", text="")
        self.history_tree.column("#0", width=20, minwidth=20, stretch=False)
        self.history_tree.tag_configure("table_group", font=("Segoe UI", 9, "bold"), background="#edf2f8")
        self.history_tree.tag_configure("win", foreground=GREEN)
        self.history_tree.tag_configure("loss", foreground=RED)
        self.history_size = -1

    def _build_settings(self, parent):
        card = self._card(parent)
        card.pack(fill="both", expand=True)
        self._label(card, "Vốn & chốt lời", 13, bold=True).pack(anchor="w", padx=12, pady=(12, 4))
        self._label(card, "Đổi vốn sẽ lưu phiên hiện tại.", 9, MUTED).pack(anchor="w", padx=12)
        grid = tk.Frame(card, bg=PANEL)
        grid.pack(fill="x", padx=12, pady=10)
        self.entries = {}
        self.money_inputs = []
        fields = (("capital", "Vốn ban đầu (VN2)"),
                  ("take_profit_pct", "Chốt lời (% vốn)"))
        for i, (key, title) in enumerate(fields):
            r, c = divmod(i, 2)
            grid.columnconfigure(c, weight=1)
            field = tk.Frame(grid, bg=PANEL)
            field.grid(row=r, column=c, sticky="ew", padx=(0, 12), pady=(0, 10))
            self._label(field, title, 9, MUTED).pack(anchor="w", pady=(0, 4))
            value = self.sim.money.start_capital if key == "capital" else getattr(self.sim.money, key)
            is_money = key == "capital"
            display_value = M.fmt_input_money(value) if is_money else str(value)
            var = tk.StringVar(value=display_value)
            entry = tk.Entry(field, textvariable=var, font=("Segoe UI", 11), relief="solid", bd=1)
            entry.pack(fill="x", ipady=4)
            if is_money:
                self._bind_money_input(entry, var)
            self.entries[key] = var
        method = tk.Frame(card, bg=PANEL)
        method.pack(fill="x", padx=12, pady=(4, 0))
        self._label(method, "Phương pháp vào cầu", 12, bold=True).pack(anchor="w")
        self.profile_var = tk.StringVar(value=self.sim.profile)
        for name in ME.PROFILES:
            tk.Radiobutton(method, text=ME.PROFILE_LABEL[name], variable=self.profile_var, value=name,
                           command=self._rebuild_ladder_inputs, bg=PANEL, fg=TEXT, selectcolor=PALE,
                           activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w")
        self._label(method, "Hướng khối 3–3 tại cùng bàn", 10, MUTED, True).pack(anchor="w", pady=(8, 0))
        for value, title in (
                (ME.BLOCK_MODE_RECALCULATE, "Tính lại bên mạnh sau mỗi khối 6 tay (hiện tại)"),
                (ME.BLOCK_MODE_REPEAT, "Lặp lại hướng của khối đầu tiên (3P–3B rồi tiếp tục 3P–3B)")):
            tk.Radiobutton(method, text=title, variable=self.block_mode_var, value=value,
                           command=self.apply_block_mode, bg=PANEL, fg=TEXT, selectcolor=PALE,
                           activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w")
        self.ladder_mode_frame = tk.Frame(method, bg=PANEL)
        self.ladder_mode_frame.pack(fill="x", pady=(10, 0))
        self._label(self.ladder_mode_frame, "Cơ chế điền mức cược", 10, MUTED, True).pack(anchor="w")
        self.ladder_mode_buttons = {}
        for key, title in (("manual", "Tự config — điền từng bậc"),
                           ("multiply", "Tự nhân — theo cấu trúc 1-3-7-15-32-64")):
            tk.Radiobutton(self.ladder_mode_frame, text=title, variable=self.ladder_mode_var,
                           value=key, command=self._on_ladder_mode_change, bg=PANEL, fg=TEXT,
                           selectcolor=PALE, activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w")
        self.ladder_base_row = tk.Frame(method, bg=PANEL)
        self.ladder_base_row.pack(fill="x", pady=(6, 0))
        self._label(self.ladder_base_row, "Bậc 1 (VN2)", 10, MUTED).pack(side="left", padx=(0, 8))
        self.ladder_base_entry = tk.Entry(self.ladder_base_row, textvariable=self.ladder_base_var,
                                          width=14, font=("Segoe UI", 11), relief="solid", bd=1)
        self.ladder_base_entry.pack(side="left", ipady=4)
        self._bind_money_input(self.ladder_base_entry, self.ladder_base_var)
        self._label(self.ladder_base_row, "×3 ×7 ×15 ×32 ×64", 9, MUTED).pack(side="left", padx=8)
        self.ladder_frame = tk.Frame(method, bg=PANEL)
        self.ladder_frame.pack(fill="x", pady=(10, 0))
        self.ladder_vars = []
        self._rebuild_ladder_inputs()
        self._label(method, "Thắng về bậc 1 · hoà giữ bậc, hoàn tiền và không tính tay · mỗi ô là một tay trong khối.",
                    9, MUTED, justify="left", wraplength=500).pack(anchor="w", pady=(6, 0))
        hands_row = tk.Frame(method, bg=PANEL)
        hands_row.pack(fill="x", pady=(12, 0))
        self._label(hands_row, "Bộ lọc cầu", 12, bold=True).pack(anchor="w")
        hand_inputs = tk.Frame(hands_row, bg=PANEL)
        hand_inputs.pack(anchor="w", pady=(6, 0))
        self._label(hand_inputs, "Tay đã đi", 9, MUTED).pack(side="left", padx=(0, 6))
        tk.Entry(hand_inputs, textvariable=self.hands_min_var, width=4, font=("Segoe UI", 11),
                 relief="solid", bd=1).pack(side="left", ipady=3)
        self._label(hand_inputs, "–", 9, MUTED).pack(side="left", padx=4)
        tk.Entry(hand_inputs, textvariable=self.hands_max_var, width=4, font=("Segoe UI", 11),
                 relief="solid", bd=1).pack(side="left", ipady=3)
        self._label(hand_inputs, "\u00b7 b\u1ec7t t\u1ed1i \u0111a \u1edf s\u1ea3nh", 9, MUTED).pack(side="left", padx=(10, 6))
        tk.Entry(hand_inputs, textvariable=self.streak_var, width=3, font=("Segoe UI", 11),
                 relief="solid", bd=1).pack(side="left", ipady=3)
        live_exit_row = tk.Frame(hands_row, bg=PANEL)
        live_exit_row.pack(anchor="w", pady=(6, 0))
        self._label(live_exit_row, "Thoát bàn khi vệt bệt đạt", 9, MUTED).pack(side="left", padx=(0, 6))
        tk.Entry(live_exit_row, textvariable=self.live_exit_streak_var, width=4,
                 font=("Segoe UI", 11), relief="solid", bd=1).pack(side="left", ipady=3)
        self._label(live_exit_row, "tay (ví dụ 7: thoát ở tay bệt thứ 7)", 9, MUTED).pack(side="left", padx=(4, 0))
        self._label(hand_inputs, "tay", 9, MUTED).pack(side="left", padx=(4, 0))
        self._button(hand_inputs, "Lưu bộ lọc", self.apply_road_filter).pack(side="left", padx=(10, 0))
        self.hands_note = self._label(hands_row, "", 9, MUTED, justify="left", wraplength=500)
        self.hands_note.pack(anchor="w", pady=(6, 0))
        self._button(card, "Lưu & áp dụng phương pháp", self.apply_settings, True).pack(anchor="w", padx=12, pady=(14, 0))
        cycle = tk.Frame(card, bg=PANEL)
        cycle.pack(fill="x", padx=12, pady=(16, 0))
        self._label(cycle, "Chu trình bàn", 12, bold=True).pack(anchor="w")
        self.cycle_check = tk.Checkbutton(cycle, text="", variable=self.auto_cycle_var,
                                          command=self._save_state, bg=PANEL, fg=TEXT, selectcolor=PALE,
                                          activebackground=PANEL, font=("Segoe UI", 10))
        self.cycle_check.pack(anchor="w")
        self.hunt_check = tk.Checkbutton(cycle, text="Tự quét tìm bàn tới khi vào được (nghỉ %d giây)" % CYCLE_RETRY_SECONDS,
                                         variable=self.auto_hunt_var, command=self._save_state,
                                         bg=PANEL, fg=TEXT, selectcolor=PALE, activebackground=PANEL,
                                         font=("Segoe UI", 10))
        self.hunt_check.pack(anchor="w")
        tables_row = tk.Frame(cycle, bg=PANEL)
        tables_row.pack(anchor="w", pady=(6, 0))
        self._label(tables_row, "Số bàn muốn chơi", 9, MUTED).pack(side="left", padx=(0, 6))
        tk.Entry(tables_row, textvariable=self.tables_var, width=4, font=("Segoe UI", 11),
                 relief="solid", bd=1).pack(side="left", ipady=3)
        self._button(tables_row, "Lưu số bàn", self.apply_tables_target).pack(side="left", padx=(8, 0))
        self.tables_note = self._label(cycle, "", 9, MUTED, justify="left", wraplength=500)
        self.tables_note.pack(anchor="w", pady=(2, 0))
        self.cycle_rule = self._label(cycle, "", 9, MUTED, justify="left", wraplength=500)
        self.cycle_rule.pack(anchor="w", pady=(4, 0))
        self._sync_method_texts()
        display = tk.Frame(card, bg=PANEL)
        display.pack(fill="x", padx=12, pady=(16, 0))
        self._label(display, "Hiển thị", 12, bold=True).pack(anchor="w", pady=(0, 6))
        scale_row = tk.Frame(display, bg=PANEL)
        scale_row.pack(anchor="w")
        self._label(scale_row, "Tỉ lệ giao diện (%)", 9, MUTED).pack(side="left", padx=(0, 8))
        self.ui_scale_var = tk.StringVar(value=str(int(self.ui_scale)))
        tk.Entry(scale_row, textvariable=self.ui_scale_var, width=6,
                 font=("Segoe UI", 11), relief="solid", bd=1).pack(side="left", ipady=3)
        self._button(scale_row, "Lưu tỉ lệ", self.apply_ui_scale).pack(side="left", padx=8)
        self._label(display, "Cửa sổ app 600 × 900 · tỉ lệ áp dụng khi mở lại app.",
                    9, MUTED).pack(anchor="w", pady=(6, 0))
        self._label(card, "Kết nối Chrome", 12, bold=True).pack(anchor="w", padx=12, pady=(18, 6))
        for var, text in ((self.chrome_startup_var, "Tự mở Chrome khi khởi động app"),
                          (self.cdp_var, "Chrome app mở có cổng 9222 (để quét sảnh)")):
            tk.Checkbutton(card, text=text, variable=var, command=self._save_state, bg=PANEL,
                           activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w", padx=10)
        tk.Checkbutton(card, text="Tự phục hồi phiên khi bị văng “Bạn đã đăng xuất” (EV.7)",
                       variable=self.auto_recover_var, command=self._save_state, bg=PANEL,
                       fg=TEXT, selectcolor=PALE, activebackground=PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=10)
        tk.Checkbutton(card, text="Chuột người thật khi bấm sảnh/ô bàn",
                       variable=self.human_mouse_var, command=self.apply_mouse_setting, bg=PANEL,
                       fg=TEXT, selectcolor=PALE, activebackground=PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=10)
        tk.Checkbutton(card, text="Tự đặt chip vào ô cược khi chốt kế hoạch",
                       variable=self.auto_place_var, command=self._save_state, bg=PANEL,
                       fg=TEXT, selectcolor=PALE, activebackground=PANEL,
                       font=("Segoe UI", 10)).pack(anchor="w", padx=10)
        window = tk.Frame(card, bg=PANEL)
        window.pack(fill="x", padx=10, pady=(10, 0))
        tk.Checkbutton(window, text="Chrome toàn màn hình",
                       variable=self.chrome_fullscreen_var,
                       command=self.apply_window_settings, bg=PANEL, fg=TEXT, selectcolor=PALE,
                       activebackground=PANEL, font=("Segoe UI", 10)).pack(anchor="w")
        size_row = tk.Frame(window, bg=PANEL)
        size_row.pack(anchor="w", pady=(8, 0))
        inputs = (("Ngang", self.chrome_w_var, 6), ("Dọc", self.chrome_h_var, 6),
                  ("Tỉ lệ %", self.chrome_scale_var, 5))
        for title, var, width in inputs:
            self._label(size_row, title, 9, MUTED).pack(side="left", padx=(0, 4))
            tk.Entry(size_row, textvariable=var, width=width, font=("Segoe UI", 11),
                     relief="solid", bd=1).pack(side="left", padx=(0, 10), ipady=3)
        self._button(size_row, "Lưu cửa sổ", self.apply_window_settings).pack(side="left")
        self._label(window, "Cỡ 800×600 – 7680×4320, tỉ lệ 50–200%. Đổi tỉ lệ render cần mở lại Chrome.",
                    9, MUTED, justify="left", wraplength=500).pack(anchor="w", pady=(6, 0))
        chips = tk.Frame(card, bg=PANEL)
        chips.pack(fill="x", padx=12, pady=(14, 12))
        self._label(chips, "Chip trên bàn", 12, bold=True).pack(anchor="w")
        self._label(chips, "Số trên chip là tiền VN2 của sàn (chip 20 = 20). App đọc khay thật để gợi ý chip.",
                    9, MUTED, justify="left", wraplength=500).pack(anchor="w")

    def _bind_money_input(self, entry, var):
        """Ô nhập tiền chỉ nhận chữ số và tự chèn dấu phân cách hàng nghìn khi gõ."""
        entry.config(validate="key", validatecommand=(
            entry.register(lambda proposed: re.fullmatch(r"[\d,.]*", proposed) is not None), "%P"))

        def regroup(_event=None):
            try:
                caret = entry.index("insert")
            except tk.TclError:
                caret = len(var.get())
            grouped, target = group_money_input(var.get(), caret)
            if grouped == var.get():
                return
            var.set(grouped)
            entry.icursor(target)

        self.money_inputs.append((entry, regroup))
        for event in ("<KeyRelease>", "<FocusOut>"):
            entry.bind(event, regroup)

    def format_money_inputs(self):
        """Định dạng lại mọi ô nhập tiền khi giá trị được set bằng code, không có phím thật."""
        alive = []
        for entry, regroup in self.money_inputs:
            if not entry.winfo_exists():
                continue
            regroup()
            alive.append((entry, regroup))
        self.money_inputs = alive

    def _sync_cycle_strip(self):
        """Dải ô tay của khối: 6 ô cho 3-3, 5 ô cho 3-2, dựng lại khi đổi phương pháp."""
        for label in self.cycle_labels:
            label.destroy()
        self.cycle_labels = []
        for i in range(self.sim.cycle_hands):
            label = self._label(self.cycle_strip, str(i + 1), 10, MUTED, True, width=3, pady=3)
            label.pack(side="left", padx=(0, 5))
            self.cycle_labels.append(label)

    def _rebuild_ladder_inputs(self):
        """Số ô nhập bậc cược đi theo khối của phương pháp; giữ mức đã gõ riêng cho từng phương pháp."""
        if getattr(self, "ladder_vars", None):
            self.ladder_store[self.ladder_shown_profile] = [var.get() for var in self.ladder_vars]
        profile = self.profile_var.get()
        raw = self.ladder_store.get(profile) or [M.fmt_input_money(s) for s in ME.DEFAULT_LADDER[profile]]
        for child in self.ladder_frame.winfo_children():
            child.destroy()
        self.ladder_vars = []
        multiply = self.ladder_mode_var.get() == "multiply"
        for i, text in enumerate(raw):
            field = tk.Frame(self.ladder_frame, bg=PANEL)
            field.grid(row=i // 3, column=i % 3, padx=(0, 12), pady=(0, 8))
            self._label(field, "Tay %d · bậc %d (VN2)" % (i + 1, i + 1), 9, MUTED).pack(anchor="w")
            var = tk.StringVar(value=text)
            entry = tk.Entry(field, textvariable=var, width=13, font=("Segoe UI", 11),
                             relief="solid", bd=1)
            entry.pack(ipady=4)
            self._bind_money_input(entry, var)
            if multiply:
                entry.config(state="readonly", readonlybackground=PALE)
            self.ladder_vars.append(var)
        self.ladder_shown_profile = profile
        self._sync_method_texts(profile)
        self._sync_ladder_mode_state()

    def _sync_ladder_mode_state(self):
        """Đồng bộ trạng thái ô bậc 1 và các ô bậc theo cơ chế đang chọn."""
        if not hasattr(self, "ladder_base_entry"):
            return
        multiply = self.ladder_mode_var.get() == "multiply"
        self.ladder_base_entry.config(state="normal" if multiply else "readonly",
                                      readonlybackground=PALE if not multiply else "white")
        if multiply:
            try:
                base = M.parse_money(self.ladder_base_var.get())
            except ValueError:
                base = 0
            if base > 0:
                stakes = ME.multiply_ladder(base, self.profile_var.get())
                for var, value in zip(self.ladder_vars, stakes):
                    var.set(M.fmt_input_money(value))
                self.ladder_store[self.ladder_shown_profile] = [M.fmt_input_money(s) for s in stakes]

    def _on_ladder_mode_change(self):
        """Đổi cơ chế: bảo lưu bậc 1 và tự điền/nhả các ô bậc cược cho khớp."""
        multiply = self.ladder_mode_var.get() == "multiply"
        if multiply:
            # Lấy bậc 1 hiện đang hiển thị làm gốc cho cơ chế tự nhân.
            try:
                base = M.parse_money(self.ladder_vars[0].get())
            except (ValueError, IndexError):
                base = ME.DEFAULT_LADDER[self.profile_var.get()][0]
            self.ladder_base_var.set(M.fmt_input_money(base))
        self._rebuild_ladder_inputs()

    def _ladder_from_inputs(self):
        """Đọc các ô bậc cược thành số tiền; báo rõ ô nào nhập sai."""
        if self.ladder_mode_var.get() == "multiply":
            try:
                base = M.parse_money(self.ladder_base_var.get())
            except ValueError:
                raise ValueError("Bậc 1 (%r) chưa hợp lệ — nhập số tiền, ví dụ 20,000." % self.ladder_base_var.get())
            if base <= 0:
                raise ValueError("Bậc 1 phải lớn hơn 0.")
            stakes = ME.multiply_ladder(base, self.profile_var.get())
            self.ladder_store[self.profile_var.get()] = [M.fmt_input_money(s) for s in stakes]
            return stakes
        stakes = []
        for i, var in enumerate(self.ladder_vars):
            try:
                stakes.append(M.parse_money(var.get()))
            except ValueError:
                raise ValueError("Bậc %d (%r) chưa hợp lệ — nhập số tiền, ví dụ 160,000." % (
                    i + 1, var.get()))
        return stakes

    def _sync_method_texts(self, profile=None):
        """Câu chữ trong app luôn nói đúng số tay của khối đang chọn."""
        profile = profile or self.sim.profile
        hands = ME.block_hands(profile)
        strong = ME.strong_hands(profile)
        if hasattr(self, "brand_method"):
            applied = ME.strong_hands(self.sim.profile)
            self.brand_method.config(text="%d — %d" % (applied, self.sim.cycle_hands - applied))
        target = self.sim.money.take_profit_pct
        if hasattr(self, "cycle_check"):
            self.cycle_check.config(text="Lặp khối %s tại bàn tới khi lãi đủ %.0f%% vốn" % (
                profile, target))
            direction_rule = ("t\u00ednh l\u1ea1i b\u00ean m\u1ea1nh m\u1ed7i kh\u1ed1i" if self.sim.block_mode == ME.BLOCK_MODE_RECALCULATE
                              else "l\u1eb7p l\u1ea1i h\u01b0\u1edbng c\u1ee7a kh\u1ed1i \u0111\u1ea7u ti\u00ean")
            self.cycle_rule.config(
                text="Kh\u1ed1i %s = %d tay b\u00ean m\u1ea1nh + %d tay b\u00ean \u0111\u1ed5i; %s. "
                     "\u0110\u1ee7 %.0f%% v\u1ed1n th\u00ec r\u1eddi b\u00e0n, t\u1ef1 qu\u00e9t v\u00e0o b\u00e0n k\u1ebf ti\u1ebfp t\u1edbi khi xong %d b\u00e0n; "
                     "v\u1ed1n ban \u0111\u1ea7u t\u1ef1 \u0111\u1ed3ng b\u1ed9 theo s\u1ed1 d\u01b0 sau m\u1ed7i b\u00e0n." % (
                         profile, strong, hands - strong, direction_rule, target, self._tables_target()))
        if hasattr(self, "step_label"):
            self.step_label.config(text="Vào bàn là tự đánh · khối %s lặp tới khi lãi đủ %.0f%% vốn · tự đóng bàn"
                                   % (profile, target))

    def _scroll_page(self, event):
        canvas = self.page_scrolls.get(self.current_page)
        if canvas and not isinstance(event.widget, ttk.Treeview):
            canvas.yview_scroll(-int(event.delta / 120), "units")

    def show_page(self, key):
        self.current_page = key
        self.page_hosts[key].tkraise()
        self.page_title.config(text={"overview": "Tổng quan", "history": "Lịch sử",
                                     "settings": "Cài đặt"}[key])
        for name, button in self.nav_buttons.items():
            button.config(bg="#25364e" if name == key else NAV, fg="white" if name == key else "#b8c6d8")

    def _window_config(self):
        return BR.normalize_window((self.chrome_w_var.get(), self.chrome_h_var.get()),
                                   self.chrome_scale_var.get(), self.chrome_fullscreen_var.get())

    def _window_pos(self, window):
        if window["fullscreen"]:
            return None
        return (max(20, self.root.winfo_screenwidth() - window["width"] - 20), 30)

    def _window_text(self, window):
        return ("CHROME · toàn màn hình · tỉ lệ %d%%" % window["percent"] if window["fullscreen"]
                else "CHROME · %d × %d · tỉ lệ %d%%" % (window["width"], window["height"],
                                                         window["percent"]))

    def apply_window_settings(self):
        window = self._window_config()
        # Ô nhập được viết lại đúng giá trị đã kẹp để người dùng thấy con số app thật dùng.
        self.chrome_w_var.set(str(window["width"]))
        self.chrome_h_var.set(str(window["height"]))
        self.chrome_scale_var.set(str(window["percent"]))
        self._save_state()
        pos = self._window_pos(window)
        self.connection_text = self._window_text(window)
        if not self.browser.connected:
            self.sweep_note_text = ("Đã lưu cấu hình cửa sổ Chrome (%s). Mở Chrome là app áp dụng "
                                    "luôn; đổi tỉ lệ thì cần mở lại Chrome." % self._window_text(
                                        window).replace("CHROME · ", ""))
            self.refresh()
            return
        self.chrome_busy = True
        def work():
            try:
                sized, note = self.browser.resize_window(window, pos=pos)
                self.events.put(("window_applied", (sized, note)))
            except Exception as exc:
                self.events.put(("window_applied", (False, "Không đổi được cửa sổ (%s)." % type(exc).__name__)))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def _tables_target(self):
        """Số bàn muốn chơi trong phiên (≥ 1); ô nhập sai thì giữ mức tối thiểu 1."""
        try:
            return max(1, int(str(self.tables_var.get()).strip()))
        except (ValueError, TypeError, AttributeError):
            return 1

    def _session_complete(self):
        return self.tables_done >= self._tables_target()

    def apply_tables_target(self):
        """Lưu số bàn muốn chơi: mỗi bàn đủ chốt lời thì rời bàn, tự quét vào bàn kế tiếp."""
        try:
            count = int(self.tables_var.get().strip())
            if count < 1:
                raise ValueError()
        except (ValueError, TypeError):
            messagebox.showerror("Số bàn chưa hợp lệ",
                                 "Nhập số bàn muốn chơi ≥ 1 (ví dụ 3).", parent=self.root)
            return
        self.tables_var.set(str(count))
        self._save_state()
        self._sync_method_texts()
        self.refresh()

    def _start_next_table(self):
        """Một bàn vừa chốt lời và đã rời khỏi bàn thành công, nhưng chưa đủ số bàn cấu hình.

        Đồng bộ vốn ban đầu = số dư hiện tại để bàn kế tiếp chốt lời theo % trên số tiền
        mới nhất (người dùng không phải sửa tay), rồi mở lại vòng quét tìm bàn.
        """
        book = self.sim.money
        balance = book.rebase_to_balance()
        if "capital" in self.entries:
            self.entries["capital"].set(M.fmt_input_money(balance))
        self.sim.take_profit_done = False
        self.sim.note = "Bàn trước đã chốt lời · vốn mới %s · đang tìm bàn kế tiếp." % M.fmt_money(balance)
        self.cycle_waiting = True
        self.cycle_attempts = 0
        self.cycle_retry_at = 0.0
        self.cycle_table_key = self.sim.table_key
        self.sweep_note_text = ("Bàn %d/%d đã chốt lời · vốn mới %s · đang quét tìm bàn kế tiếp…" % (
            self.tables_done, self._tables_target(), M.fmt_money(balance)))
        self._save_state()
        self.sweep_lobby()

    def apply_road_filter(self):
        """Lưu bộ lọc cầu: khoảng tay đã đi (10–50) và số tay bệt tối đa còn nhận."""
        try:
            low = int(self.hands_min_var.get().strip())
            high = int(self.hands_max_var.get().strip())
            streak = int(self.streak_var.get().strip())
            live_exit_streak = int(self.live_exit_streak_var.get().strip())
        except (ValueError, TypeError):
            messagebox.showerror("Bộ lọc cầu chưa hợp lệ",
                                 "Nhập số tay đã đi tối thiểu/tối đa (ví dụ 10 và 50) "
                                 "và số tay bệt tối đa (ví dụ 3).",
                                 parent=self.root)
            return
        low, high, streak = self.sim.set_road_filter(low, high, streak)
        live_exit_streak = self.sim.set_live_exit_streak(live_exit_streak)
        self.hands_min_var.set(str(low))
        self.hands_max_var.set(str(high))
        self.streak_var.set(str(streak))
        self.live_exit_streak_var.set(str(live_exit_streak))
        self._save_state()
        if hasattr(self, "hands_note"):
            self.hands_note.config(
                text="\u0110ang l\u1ecdc s\u1ea3nh %d?%d tay; b\u1ec7t t\u1ed1i \u0111a %d tay. Trong b\u00e0n: tho\u00e1t khi b\u1ec7t \u0111\u1ea1t %d tay." % (low, high, streak, live_exit_streak), fg=GREEN)
        self.refresh()

    def apply_mouse_setting(self):
        """Bật/tắt chuột người thật ngay cho lượt bấm sảnh kế tiếp."""
        enabled = bool(self.human_mouse_var.get())
        self.stream.human_mouse = enabled
        self.sweep_note_text = ("Đã bật chuột người thật (WindMouse + ghost-cursor) cho các cú bấm sảnh."
                                if enabled else "Đã tắt chuột người thật; dùng cú bấm 3 sự kiện như cũ.")
        self._save_state()
        self.refresh()

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

    def auto_open_table(self, allow_sweep=False, exclude_key=None):
        if self.chrome_busy or self.sweep_busy or self.open_table_busy or self.exit_requested:
            return
        if getattr(self.sim, "take_profit_done", False):
            # Đã chốt lời: chỉ đóng bàn rồi dừng, không mở bàn mới.
            return
        if not self._cdp_available():
            self.sweep_note_text = "Gắn Chrome đang mở trước."
            self.refresh()
            return
        if self.sim.running:
            self.sweep_note_text = "Tạm dừng trước khi đổi bàn."
            self.refresh()
            return
        candidates = []
        for row in self.tables.values():
            if not row.get("table_id") or row["key"] == exclude_key or row_problem(row):
                continue
            if not road_is_current(row):
                continue
            info = ME.check_table(list(row["letters"]), self.sim.min_hands, self.sim.max_hands,
                                  self.sim.max_streak)
            if info["eligible"]:
                candidates.append((info["hands"], row, info))
        if not candidates:
            if not self.cycle_waiting and self.auto_hunt_var.get():
                # A first lobby scan can return tables that simply do not meet
                # the filters. Start the same visible retry loop used later in
                # the hunt instead of leaving auto-hunt idle after that scan.
                self.cycle_waiting = True
                self.cycle_attempts = 0
                self.cycle_table_key = exclude_key
            if self._schedule_cycle_retry("Chưa có bàn đạt 3 điều kiện"):
                return
            if self.cycle_waiting or not allow_sweep:
                self.sweep_note_text = ("Chưa có bàn đạt 3 điều kiện · bấm “Quét toàn sảnh” để thử lại, "
                                        "hoặc bật “Tự quét tìm bàn” để app tự lặp cho tới khi vào được.")
                self.refresh()
                return
            self.cycle_attempts += 1
            self.sweep_note_text = "Chưa có bàn đạt 3 điều kiện (lượt %d) · đang quét lại sảnh…" % (
                self.cycle_attempts)
            self.sweep_lobby()
            return
        ideal = (self.sim.min_hands + self.sim.max_hands) / 2.0
        candidates.sort(key=lambda item: (abs(item[0] - ideal), item[1]["label"].casefold()))
        hands, row, info = candidates[0]
        self.open_table_busy = True
        self.auto_start_letters = row["letters"]
        self.sweep_note_text = "Đang mở %s (%d tay, bên mạnh %s)…" % (
            row["label"], hands, E.SIDE_NAME.get(info["strong"], "—"))
        table_id = row["table_id"]
        def work():
            try:
                ok, note = self.stream.open_table(table_id)
                self.events.put(("table_opened", (ok, note, row["key"])))
            except Exception as exc:
                self.events.put(("table_opened", (False, "Không mở được bàn (%s)." % type(exc).__name__, "")))
        threading.Thread(target=work, daemon=True).start()
        self.refresh()

    def cancel_auto_hunt(self):
        """Người dùng chủ động dừng vòng tự quét tìm bàn."""
        self.cycle_waiting = False
        self.cycle_retry_at = 0.0
        self.cycle_table_key = None
        self.sweep_note_text = "Đã dừng tự quét tìm bàn."
        self._trace("hunt_cancelled")
        self.refresh()

    def _schedule_cycle_retry(self, reason, exclude_key=None):
        """Vòng tìm bàn không có trần số lần: nghỉ CYCLE_RETRY_SECONDS rồi quét lại, tới khi vào được bàn."""
        if self.session_lost:
            return False
        if getattr(self.sim, "take_profit_done", False):
            # Sau chốt lời không còn vòng săn bàn nào để hẹn giờ.
            return False
        if not (self.cycle_waiting and self.auto_hunt_var.get()) or self.exit_requested:
            return False
        if exclude_key:
            self.cycle_table_key = exclude_key
        self.cycle_attempts += 1
        self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
        self.sweep_note_text = ("%s · tự quét lại sau %d giây (đã thử %d lượt)." % (
            reason, CYCLE_RETRY_SECONDS, self.cycle_attempts))
        self.refresh()
        return True

    def leave_table_for_lobby(self, take_profit=False):
        """Đưa tab về danh sách bàn.

        Bình thường: hết khối thì quét - chọn - mở bàn mới.
        ``take_profit=True``: chỉ rời bàn và dừng phiên, không quét/mở bàn mới.
        """
        take_profit = bool(take_profit or getattr(self.sim, "take_profit_done", False))
        filter_exit = bool(getattr(self.sim, "filter_exit_done", False))
        if self.exit_requested:
            return
        if not take_profit and not filter_exit and not self.auto_cycle_var.get():
            return
        self.cycle_waiting = True
        self.cycle_attempts = 0
        self.cycle_table_key = self.sim.table_key
        self.cycle_retry_at = 0.0
        self._trace("take_profit_exit" if take_profit else "filter_exit" if filter_exit else "block_done",
                    dict(table=self.sim.table_key, hands=self.sim.block_hands,
                         target_pct=self.sim.money.take_profit_pct))
        if self.chrome_busy or self.sweep_busy or self.open_table_busy:
            self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
            self.sweep_note_text = (
                "Đã chốt lời nhưng app đang bận · sẽ tự rời bàn sau %d giây." % CYCLE_RETRY_SECONDS
                if take_profit else
                "App đang bận nên việc đổi bàn hẹn quét lại sau %d giây." % CYCLE_RETRY_SECONDS)
            self.refresh()
            return
        if not self._cdp_available():
            self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
            self.sweep_note_text = (
                "Đã chốt lời nhưng chưa gắn Chrome · sẽ rời bàn khi kết nối lại."
                if take_profit else
                "Chưa gắn Chrome · sẽ tự quét tìm bàn ngay khi kết nối lại.")
            self.refresh()
            return
        self.sweep_busy = True
        self.sweep_note_text = (
            "Đã đạt mục tiêu lãi %.0f%% vốn · rời bàn, dừng phiên…"
            % self.sim.money.take_profit_pct if take_profit else
            "Bàn không còn đạt điều kiện lọc · đang rời bàn để quét lại…" if filter_exit else
            "Đã đi đủ %d tay · rời bàn, về danh sách bàn Baccarat…" % self.sim.cycle_hands)
        def work():
            try:
                self.events.put(("cycle_back", self.stream.back_to_lobby()))
            except Exception as exc:
                self.events.put(("cycle_back", (False, "Không về được sảnh (%s)." % type(exc).__name__)))
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

    def request_chip_read(self, force=False):
        """Đọc chip thật đang đặt trên bàn ở thread nền; không chặn UI, không bấm gì."""
        now = time.time()
        if self.chip_busy or self.exit_requested:
            return
        if not force and now - self.chip_read_at < CHIP_REFRESH:
            return
        if not self.sim.table_key:
            return
        if not self.browser.connected:
            return
        self.chip_busy = True
        self.chip_read_at = now
        unit = TB.DEFAULT_CHIP_UNIT
        def work():
            live = self._read_chip_state(unit)
            self.events.put(("chips", live))
        threading.Thread(target=work, daemon=True).start()

    def _read_chip_state(self, unit):
        """Đọc chip thật đang đặt trên bàn (đồng bộ); gọi từ thread nền.

        Dùng chung cho cả việc hiển thị trạng thái chip và cho gate "chờ ván mới"
        trước khi tự đặt cược.
        """
        try:
            return TB.read(self.browser, unit=unit)
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def _update_chip_status(self):
        """So chip thật với kế hoạch đang chốt và vẽ trạng thái khớp/lệch."""
        if not hasattr(self, "chip_status"):
            return
        plan = self.sim.pending or self.sim.plan()
        unit = (self.chip_live or {}).get("unit") or TB.DEFAULT_CHIP_UNIT
        self.chip_check = ME.compare_chips(plan.get("side"), plan.get("stake") or 0,
                                           self.chip_live, unit=unit)
        check = self.chip_check
        status = check["status"]
        self.chip_status.config(text="%s · %s" % (ME.CHIP_STATUS_LABEL.get(status, status), check["note"]),
                                fg={"match": GREEN, "no_bet": MUTED, "unknown": MUTED}.get(status, RED))
        live = self.chip_live or {}
        lines = []
        chips = check.get("suggested_chips") or []
        if chips and check["side"]:
            lines.append("Cần bấm: %s (%d chip) = %s VN2" % (
                " + ".join(str(c) for c in chips), len(chips), M.fmt_money(check["expected"])))
        if live.get("ok"):
            parts = []
            for side in ("B", "P", "T"):
                entry = (live.get("spots") or {}).get(side) or {}
                if entry.get("amount_vnd"):
                    chips_txt = " + ".join(str(c) for c in entry.get("chips", []))
                    parts.append("%s %s%s" % (E.SIDE_NAME[side], M.fmt_money(entry["amount_vnd"]),
                                              (" [" + chips_txt + "]") if chips_txt else ""))
            lines.append("Trên bàn: %s" % (" · ".join(parts) if parts else "chưa có chip"))
            if live.get("total") is not None:
                lines.append("Tổng đặt: %s VN2" % M.fmt_money(live["total"]))
        elif live.get("error"):
            lines.append("Chưa đọc được chip (%s)." % live["error"])
        else:
            lines.append("Vào bàn để đọc chip đang đặt.")
        self.chip_detail.config(text="\n".join(lines))

    def place_armed_bet(self, plan):
        """Đặt số tiền của kế hoạch vào ô cược bằng chuột; mệnh giá chip do nơi bấm chốt.

        Gọi từ hook ``self.sim.on_arm`` mỗi khi kế hoạch được chốt. Chỉ chạy khi bật
        “Tự đặt chip” và kế hoạch có cửa + số tiền; chạy ở thread nền, không chặn UI.

        Khi tự đặt đang bật, MỌI nhánh thoát đều phải báo về ``sim.mark_placed``: có tay
        chỉ được tính khi chip thật đã đặt thành công. Nhánh nào im lặng thoát là nhánh
        đó để tay chưa đặt bị đếm như đã đặt (đúng lỗi vào bàn chậm ở tay đầu tiên).
        """
        if not self.auto_place_var.get():
            # The live cycle may only advance after the browser confirms a real bet.
            # Previously this returned with no placement state, so process() treated
            # the next result like a simulated successful wager.
            self.sim.mark_placing(plan)
            self.sim.mark_placed(False, "Chưa đặt: hãy bật ‘Tự đặt chip’ để chỉ tính tay đã được web xác nhận.", plan)
            self._set_place_status(False, plan.get("place_note", "Chưa đặt được chip."))
            return
        side, stake = plan.get("side"), plan.get("stake") or 0
        if not side or stake <= 0:
            self.sim.mark_placing(plan)
            self.sim.mark_placed(False, "Chưa đặt: kế hoạch cược không hợp lệ.", plan)
            return
        self.sim.mark_placing(plan)
        if self.exit_requested:
            self._finish_place(plan, False, "Đang đóng app, huỷ tự đặt chip.")
            return
        if self.place_busy:
            self._finish_place(plan, False, "Lượt bấm chip trước chưa xong nên tay này chưa được bấm.")
            return
        # Số in trên chip chính là số tiền VN2 của sàn (chip 20 = 20), nên không nhân
        # hệ số nào: khay 20/40/50 ghép thẳng mức cược 20. Nhân thêm là bỏ tay oan.
        unit = TB.DEFAULT_CHIP_UNIT
        # KHÔNG chốt mệnh giá ở đây. chip_live là bản đọc từ trước/trong lúc bàn render
        # khay, quyết định theo nó là bỏ tay oan — đúng lỗi "bàn đã mở cửa sổ chọn chip
        # lên nhưng lại không chọn được". place_bet đọc lại khay thật ngay chỗ bấm (chờ
        # tới 20s) và chốt mệnh giá theo đúng những gì khay đang có.
        self.place_busy = True
        self.place_status.config(text="Đang tự đặt %s VN2 vào %s…" % (
            M.fmt_money(stake), E.SIDE_NAME[side]), fg=ACCENT)

        def work():
            try:
                ok_gate, gate_note = self._wait_for_next_hand(unit)
                if not ok_gate:
                    self.events.put(("placed", (False, gate_note, plan)))
                    return
                # Kết quả tay này đã về trong lúc chờ gate thì cửa sổ đặt cược đã đóng;
                # bấm nữa là chip rơi sang tay sau (đúng lỗi "tay 1 chưa đặt, tay 2 mang
                # chip 40 trong khi kế hoạch báo 120"). Bỏ lượt, không bấm liều.
                row_letters = (self.tables.get(self.sim.table_key) or {}).get("letters")
                if row_letters and row_letters != plan.get("baseline"):
                    self.events.put(("placed", (False, "Kết quả tay này đã về trước khi bấm chip; "
                                                       "không bấm sang cửa sổ tay sau.", plan)))
                    return
                # Timestamp each actual bet-spot click in table_bets, not the start of DOM setup.
                # If the result arrives while setup is waiting, a later chip click is
                # attributed to the next window and cannot settle this hand.
                def record_bet_click():
                    plan["placed_at"] = time.time()

                ok, note, _placed = TB.place_bet(self.browser, side, [], unit=unit,
                                                 human_mouse=self.human_mouse_var.get(),
                                                 amount=stake, on_bet_click=record_bet_click)
                self.events.put(("placed", (ok, note, plan)))
            except Exception as exc:
                # Thread chết giữa chừng mà không báo thì kế hoạch kẹt ở "pending" và
                # việc tính tay dừng lại mãi — phải luôn trả về một kết quả.
                self.events.put(("placed", (False, "Lỗi khi tự đặt chip (%s)."
                                            % type(exc).__name__, plan)))

        threading.Thread(target=work, daemon=True).start()

    def _finish_place(self, plan, ok, note):
        """Báo kết quả lượt đặt chip ngay trên luồng UI (nhánh không cần thread nền)."""
        self.sim.mark_placed(ok, note, plan)
        self._set_place_status(ok, note)

    def _on_placed(self, ok, note, plan):
        """Nhánh "placed" của luồng poll: kết quả chỉ có ý nghĩa với kế hoạch đang chờ.

        Về muộn (plan đã bị thay/tạm dừng) thì ghi vào lịch sử cũng không sao nhưng sim
        sẽ bỏ qua — tay mới chưa bấm chip và vẫn phải bị tính là chưa đặt.
        """
        self.place_busy = False
        self.sim.mark_placed(ok, note, plan)
        self._set_place_status(ok, note)

    def _set_place_status(self, ok, note):
        if hasattr(self, "place_status"):
            self.place_status.config(text=note, fg=(GREEN if ok else RED))

    def _wait_for_next_hand(self, unit, timeout=BET_GATE_TIMEOUT, poll=BET_GATE_POLL):
        """Chờ bàn gom hết chip ván trước (cả 3 cửa về 0) rồi mới cho phép tự đặt.

        Evolution chưa lộ rõ đồng hồ đếm giờ đặt cược qua DOM nên dùng tín hiệu gián
        tiếp: chip trên bàn trống nghĩa là ván trước đã kết toán và bàn đang ở cửa
        sổ đặt cược mới, an toàn để bấm. Hết ``timeout`` giây mà bàn còn chip của ván
        trước thì bỏ, không bấm liều lên chip cũ. Chạy trong thread nền của
        ``place_armed_bet``, không chặn UI.
        """
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.exit_requested:
                return False, "Đang đóng app, huỷ tự đặt chip."
            live = self._read_chip_state(unit)
            if table_is_clear(live):
                return True, ""
            # Phân biệt "bàn còn chip ván trước" với "không đọc được bàn": báo nhầm hai
            # cái này khiến người dùng tưởng bàn đang bận trong khi app chưa thấy khay.
            last_error = "" if (live or {}).get("ok") else ((live or {}).get("error") or "no_read")
            time.sleep(poll)
        if last_error:
            return False, ("Không tự đặt: sau %ds vẫn không đọc được bàn (%s) nên chưa bấm chip."
                           % (int(timeout), last_error))
        return False, ("Không tự đặt: sau %ds bàn vẫn còn chip ván trước, chưa chắc "
                       "đang ở lượt đặt cược mới." % int(timeout))

    def _on_cycle_back(self, ok, note):
        """Kết quả đưa tab về danh sách bàn.

        Bàn chốt lời: rời bàn thành công thì tính là xong một bàn. Chưa đủ số bàn cấu
        hình thì đồng bộ vốn và quét bàn kế tiếp; đủ rồi thì dừng hẳn (không quét/mở).
        """
        self.sweep_note_text = note
        take_profit = bool(getattr(self.sim, "take_profit_done", False))
        self._trace("cycle_back", dict(ok=ok, take_profit=take_profit))
        if take_profit:
            if ok:
                self.cycle_waiting = False
                self.cycle_retry_at = 0.0
                self.cycle_table_key = None
                self.tables_done += 1
                self._trace("table_completed", dict(done=self.tables_done,
                                                     target=self._tables_target(),
                                                     balance=self.sim.money.balance))
                if self._session_complete():
                    self.sweep_note_text = (
                        "Đã đạt mục tiêu lãi %.0f%% vốn (%s) · đã đóng bàn và dừng phiên "
                        "(xong %d/%d bàn)." % (
                            self.sim.money.take_profit_pct, M.fmt_money(self.sim.money.profit),
                            self.tables_done, self._tables_target()))
                else:
                    self._start_next_table()
            elif self._auth_blame():
                self._session_lost_step(note)
            else:
                # Không về được sảnh: hẹn thử lại đúng việc rời bàn,
                # không chuyển sang săn bàn mới.
                self.cycle_waiting = True
                self.cycle_retry_at = time.time() + CYCLE_RETRY_SECONDS
                self.sweep_note_text = (
                    "%s · sẽ thử đóng bàn lại sau %d giây." % (note, CYCLE_RETRY_SECONDS))
            self.refresh()
            return
        if ok:
            self.sweep_lobby()
        elif self._auth_blame():
            self._session_lost_step(note)
        elif not self._schedule_cycle_retry(note):
            self.cycle_waiting = False
            self.refresh()

    def poll(self):
        if self.closing:
            return
        while not self.events.empty():
            kind, data = self.events.get_nowait()
            if kind in ("opened", "open_error", "stopped"):
                self.chrome_busy = False
                self.connection_text = data
                self.next_scan = 0
                if kind != "opened":
                    self.sim.pause("Chưa kết nối Chrome. Phiên theo dõi đã dừng.")
                    self.tables.clear()
            elif kind == "scan":
                self.scan_busy = False
                reports, error, observed_at = data
                if error:
                    self.connection_text = "Đọc cầu chưa thành công: " + error
                    self.tables.clear()
                    if self._auth_blame():
                        self._session_lost_step(error)
                        continue
                    self.sim.pause("Mất kết nối/đọc cầu lỗi. Hãy đối chiếu lại trước khi tiếp tục.")
                else:
                    self.session_lost = False
                    self._sync_wallet_from_reports(reports)
                    self._ingest_reports(reports, observed_at)
                    n = sum(len(rep.get("tables", [])) for rep in reports)
                    self.connection_text = ("Đang đọc %d bàn." % n if n else "Chưa thấy bàn Baccarat.")
            elif kind == "sweep":
                self.sweep_busy = False
                rows, error, diag, observed_at = data
                self._trace("sweep", dict(rows=len(rows or []), error=bool(error), diag=diag or {}))
                if error and self._auth_blame():
                    self._session_lost_step("Quét sảnh không thành công: " + error)
                elif error:
                    self.sweep_note_text = "Quét sảnh không thành công: " + error
                    self._schedule_cycle_retry(self.sweep_note_text)
                elif not rows:
                    # Lượt quét không ra bàn nào (kể cả lần quét đầu tiên, trước khi từng
                    # vào bàn): nếu "Tự quét tìm bàn" đang bật, tự bật vòng hunt ngay từ
                    # đây để có banner đếm ngược + tự quét lại, không chỉ dòng chữ tĩnh
                    # bắt người dùng tự bấm lại "Quét toàn sảnh".
                    was_hunting = self.cycle_waiting
                    if not was_hunting and self.auto_hunt_var.get() and not self.session_lost:
                        self.cycle_waiting = True
                        self.cycle_attempts = 0
                        self.cycle_table_key = None
                    self.sweep_note_text = (
                        "Chưa có bàn nào trong mục Ưa thích đọc được lịch sử. Hãy thêm bàn vào Ưa thích "
                        "trên sảnh rồi quét lại."
                        if self._last_sweep_favorites else
                        ("Chưa đọc được lịch sử bàn ở sảnh. Mở tab sảnh Baccarat rồi bấm "
                         "“Nhận tab sảnh” để app tự quét tiếp."
                         if was_hunting else
                         "Chưa nhận được lịch sử. Mở sảnh Baccarat rồi quét lại."))
                    self._schedule_cycle_retry(self.sweep_note_text)
                else:
                    self.session_lost = False
                    self._ingest_reports([{"tables": rows, "page": {}, "diag": {}}], observed_at)
                    for row in rows:
                        stored = self.tables.get("id:" + row["table_id"])
                        # WebSocket và cầu đang vẽ là hai nguồn đọc độc lập cùng cho một chuỗi.
                        if stored and row["road_match"] and stored["stable"] < 2:
                            stored["stable"] = 2
                    # Quét Ưa thích chỉ tin danh sách vừa lọc: bỏ bàn cũ ngoài danh sách
                    # để vòng tự mở bàn không chọn nhầm bàn không còn là ưa thích.
                    if self._last_sweep_favorites:
                        allowed = {"id:" + row["table_id"] for row in rows if row.get("table_id")}
                        self.tables = {key: value for key, value in self.tables.items()
                                       if key in allowed or key == self.sim.table_key}
                    self.sweep_note_text = "Đã quét %d bàn%s · %d bàn khớp lịch sử." % (
                        len(rows), " trong mục Ưa thích" if self._last_sweep_favorites else "",
                        sum(1 for r in rows if r["road_match"]))
                    # Bàn được mở bằng đúng ô bàn của sảnh nên phiên đăng nhập không bị động vào.
                    if not self.sim.running:
                        self.auto_open_table(allow_sweep=self.cycle_waiting,
                                             exclude_key=self.cycle_table_key if self.cycle_waiting else None)
            elif kind == "lobby_entered":
                self.sweep_busy = False
                ok, note = data
                self.sweep_note_text = note
                self._trace("lobby_tab", dict(ok=ok))
                if ok:
                    self.session_lost = False
                    self.next_scan = 0
            elif kind == "window_applied":
                self.chrome_busy = False
                ok, note = data
                self.sweep_note_text = note if ok else "Đã lưu cấu hình. " + note
            elif kind == "cycle_back":
                self.sweep_busy = False
                self._on_cycle_back(*data)
            elif kind == "table_opened":
                self.open_table_busy = False
                ok, note, key = data
                self.sweep_note_text = note
                if ok and key in self.tables:
                    self.session_lost = False
                    self.sim.bind(self.tables[key])
                    self.auto_start_table_key = key
                    self.auto_start_deadline = time.time() + ENTRY_WAIT
                    self.sweep_note_text = "Đã vào bàn · đang xác nhận dữ liệu…"
                    self.next_scan = 0
                    self.chip_live = None
                    self.chip_read_at = 0.0
                elif not ok:
                    self._trace("table_open_failed", dict(key=key))
                    if self._auth_blame():
                        self._session_lost_step(note)
                    else:
                        self._schedule_cycle_retry(note, exclude_key=key or self.cycle_table_key)
            elif kind == "chips":
                self.chip_busy = False
                self.chip_live = data
            elif kind == "placed":
                self._on_placed(*data)
            elif kind == "recovered":
                ok, note = data
                self._on_recovered(ok, note)
        self._auto_start_verified_table()
        if self.sim.process(self.tables.get(self.sim.table_key)):
            self._save_state()
        if self.sim.cycle_done:
            self.sim.cycle_done = False
            # Đủ lãi mới có cờ take_profit_done: rời bàn xong thì dừng hẳn,
            # không quét hay mở bàn mới.
            self.leave_table_for_lobby(take_profit=self.sim.take_profit_done)
        now = time.time()
        # Mất phiên thì dừng hẳn việc đọc/đặt chip: quét tiếp chỉ bấm lên trang đã bị site
        # đá (EV.7) và làm phiên bị đá lặp lại. Chờ người dùng/tự phục hồi mở lại sảnh.
        if not self.session_lost:
            self.request_chip_read()
        if (not self.exit_requested and not self.session_lost and now >= self.next_scan
                and not self.scan_busy and not self.chrome_busy and not self.sweep_busy
                and self.browser.connected):
            self.next_scan = now + (FAST_SCAN if (self.auto_start_table_key or self.sim.running) else SCAN_SECONDS)
            self._scan()
        self._resume_cycle_hunt(now)
        self.refresh()
        self.poll_id = self.root.after(500, self.poll)

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

    def _resume_cycle_hunt(self, now):
        """Hết thời gian nghỉ: phục hồi phiên nếu đang văng, ngược lại quét-chọn-vào bàn."""
        take_profit = bool(getattr(self.sim, "take_profit_done", False))
        filter_exit = bool(getattr(self.sim, "filter_exit_done", False))
        if not (self.cycle_waiting and (self.auto_hunt_var.get() or take_profit or filter_exit)
                and self.cycle_retry_at and now >= self.cycle_retry_at):
            return
        self.cycle_retry_at = 0.0
        if self.exit_requested or self.sim.running or self.auto_start_table_key:
            return
        if take_profit or filter_exit:
            # Đang chờ đóng bàn sau chốt lời: thử lại đúng việc rời bàn,
            # không quét và không mở bàn mới.
            self.leave_table_for_lobby(take_profit=take_profit)
            return
        # Vẫn đang mất phiên: thử mở lại EVO thay vì quét trên trang đã bị đá.
        if self.session_lost:
            self._try_recover_session()
            if self.session_lost:
                # Chưa phục hồi được (hoặc đang chạy nền): giữ nhịp chờ, không quét.
                self.cycle_retry_at = now + CYCLE_RETRY_SECONDS
            return
        if self.chrome_busy or self.sweep_busy or self.open_table_busy or not self._cdp_available():
            self.cycle_retry_at = now + CYCLE_RETRY_SECONDS
            return
        self.sweep_note_text = "Đang tự quét tìm bàn đạt chuẩn (lượt %d)…" % (self.cycle_attempts + 1)
        self._trace("hunt_retry", dict(attempt=self.cycle_attempts + 1))
        self.sweep_lobby()

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
        # Lobby có thể tạm không trả bàn đang mở khi chuyển màn hình: giữ cược đã chốt, chờ lượt đọc sau.

    def _cycle_search(self, key):
        """Chu trình đang chờ bàn mới: loại bàn vừa hỏng và tìm bàn khác trong sảnh."""
        if self.cycle_waiting:
            self.auto_open_table(allow_sweep=True, exclude_key=key)

    def _auto_start_verified_table(self):
        key = self.auto_start_table_key
        if not key:
            return
        if getattr(self, "_wallet_startup_sync_pending", False):
            self.sweep_note_text = "Đang chờ đồng bộ vốn theo số dư ví trước khi theo dõi bàn."
            return
        row = self.tables.get(key)
        if not row or row.get("source") != "evo-bead" or row.get("screen_stable", 0) < 2:
            if time.time() > self.auto_start_deadline:
                self.sweep_note_text = ("Vào bàn rồi nhưng %d giây chưa đọc được cầu bàn đã mở hai lần giống nhau. "
                                        "Đang tìm bàn khác…" % ENTRY_WAIT)
                self.auto_start_table_key = None
                self._cycle_search(key)
            return
        problem = row_problem(row)
        if problem or not same_shoe(self.auto_start_letters, row["letters"]):
            self.sweep_note_text = ("Chưa tự bắt đầu: " + problem) if problem else (
                "Cầu trong bàn (%d tay) không cùng shoe với cầu đã lọc ở sảnh (%d tay)." % (
                    len(row["letters"]), len(self.auto_start_letters)))
            self.auto_start_table_key = None
            self._cycle_search(key)
            return
        if self.sim.arm_at_entry(row):
            self.sweep_note_text = "Đã vào bàn và bắt đầu theo dõi · %s" % self.sim.note
            self.auto_start_table_key = None
            self.cycle_waiting = False
            self.cycle_attempts = 0
            self.cycle_retry_at = 0.0
            self.cycle_table_key = None
        else:
            self.sweep_note_text = "Chưa thể bắt đầu: " + self.sim.note
            self.auto_start_table_key = None
            self._cycle_search(key)

    def _render_tables(self):
        if not hasattr(self, "live_tree"):
            return
        rows = []
        for row in self.tables.values():
            if time.time() - row["ts"] > LIVE_STALE:
                continue
            info = ME.check_table(list(row["letters"]), self.sim.min_hands, self.sim.max_hands,
                                  self.sim.max_streak)
            ready = info["eligible"] and not row_problem(row) and road_is_current(row)
            if self.search.get().casefold() not in row["label"].casefold() or (self.only_eligible.get() and not ready):
                continue
            rows.append((row, info, ready))
        rows.sort(key=lambda item: (not item[2], item[0]["label"].casefold()))
        keep = {row["key"] for row, _, _ in rows}
        for key in self.live_tree.get_children():
            if key not in keep:
                self.live_tree.delete(key)
        for i, (row, info, ready) in enumerate(rows):
            passed = sum(c["ok"] for c in info["checks"])
            verdict = "Đạt 3/3 · cần đối chiếu" if info["eligible"] else "%d/3 · %s" % (
                passed, next(c["detail"] for c in info["checks"] if not c["ok"]))
            problem = row_problem(row)
            status = "Xung đột" if row["conflict"] else "Chờ xác nhận đọc" if row["stable"] < 2 else (
                "Dữ liệu cũ" if time.time() - row["ts"] > LIVE_STALE else "Mới · %ds" % (time.time() - row["ts"]))
            if row.get("read_error"):
                verdict, status = row["read_error"], "Cần đối chiếu"
            elif row.get("game_type") and row["game_type"] != "baccarat":
                verdict, status = "Bàn đặc biệt · không theo dõi", "Đọc được Canvas"
            elif not road_is_current(row):
                status = "Chưa đối chiếu 2 nguồn"
            elif problem and not row["conflict"] and row["stable"] >= 2 and "mã bàn" in problem:
                status = "Chưa rõ mã bàn"
            values = (row["label"], len(row["letters"]), E.SIDE_NAME.get(info["strong"], "—"), verdict, status)
            if self.live_tree.exists(row["key"]):
                self.live_tree.item(row["key"], values=values, tags=("ready" if ready else "wait",))
                self.live_tree.move(row["key"], "", i)
            else:
                self.live_tree.insert("", "end", iid=row["key"], values=values, tags=("ready" if ready else "wait",))
        self.table_title.config(text="Bàn khớp cầu · %d" % len(rows))
        self.empty_label.config(text=("%d bàn hiển thị · Bấm đúp để đối chiếu. Bàn đặc biệt không hỗ trợ." % len(rows))
                                if self.tables else "Chưa đọc được bàn. Đăng nhập trong Chrome và mở sảnh Baccarat.")

    def load_live_selected(self):
        """Chọn bàn là vào đánh luôn: không còn nút Bắt đầu/Tạm dừng hay ô xác nhận tay.

        Bàn không đạt bộ lọc thì ``sim.start`` tự trả lời lý do (qua ``plan``) và không
        chốt cược nào; đang theo dõi đúng bàn đó thì giữ nguyên khối đang đi.
        """
        if getattr(self, "_wallet_startup_sync_pending", False):
            self.sim.note = "Đang chờ đọc số dư ví để đặt lại vốn phiên trước khi theo dõi bàn."
            self.refresh()
            return
        selected = self.live_tree.selection()
        row = self.tables.get(selected[0]) if selected else None
        if not row:
            self.sim.note = "Chọn một dòng bàn trong danh sách trước."
        elif self.sim.running and row["key"] == self.sim.table_key:
            self.sim.note = "Đang theo dõi bàn này."
        else:
            self.sim.bind(row)
            self.sim.start(row, verified=True)
        self._save_state()
        self.refresh()

    def _refresh_hunt_banner(self):
        """Hiện/ẩn banner chờ khi vòng tự quét tìm bàn đang chạy, kèm đếm ngược và lượt thử."""
        take_profit = bool(getattr(self.sim, "take_profit_done", False))
        filter_exit = bool(getattr(self.sim, "filter_exit_done", False))
        waiting = bool(getattr(self, "cycle_waiting", False)) and (
            bool(self.auto_hunt_var.get()) or take_profit or filter_exit)
        if not waiting:
            self.hunt_banner.pack_forget()
            return
        self._hunt_frame = getattr(self, "_hunt_frame", 0) + 1
        spinner = "●○"[self._hunt_frame % 2]
        remaining = max(0.0, self.cycle_retry_at - time.time()) if self.cycle_retry_at else 0.0
        if filter_exit:
            if remaining > 0:
                text = "Bàn vi phạm điều kiện lọc · thử rời bàn lại sau %.0f giây." % remaining
            else:
                text = "Bàn vi phạm điều kiện lọc · đang rời bàn để quét bàn khác…"
        elif take_profit:
            # Đã chốt lời: banner chỉ nhắc việc đóng bàn, không phải săn bàn mới.
            if remaining > 0:
                text = "Đã chốt lời · thử đóng bàn lại sau %.0f giây (lượt %d)." % (
                    remaining, self.cycle_attempts)
            else:
                text = ("Đã chốt lời · đang đóng bàn để dừng phiên…" if self._session_complete()
                        else "Đã chốt lời · đang rời bàn để sang bàn kế tiếp…")
        elif remaining > 0:
            text = "Đang tự quét tìm bàn… nghỉ %.0f giây nữa rồi quét lại (đã thử %d lượt)." % (
                remaining, self.cycle_attempts)
        else:
            text = "Đang tự quét tìm bàn đạt chuẩn… (đã thử %d lượt)." % self.cycle_attempts
        self.hunt_spinner.config(text=spinner)
        self.hunt_text.config(text=text)
        anchor = getattr(self, "_hunt_anchor", None)
        if anchor is not None and anchor.winfo_exists():
            self.hunt_banner.pack(fill="x", pady=(0, 12), before=anchor)
        else:
            self.hunt_banner.pack(fill="x", pady=(0, 12))

    def refresh(self):
        book = self.sim.money
        decisive = book.wins + book.losses
        values = (M.fmt_money(book.balance), signed(book.profit), "%+.2f%%" % (book.profit / book.start_capital * 100)
                  if book.start_capital else "—", "%.0f%% / %.0f%%" % (book.wins / decisive * 100, book.losses / decisive * 100)
                  if decisive else "— / —")
        notes = ("Hiện tại", "%d tay · %d hòa" % (book.hands_played, book.pushes),
                 "÷ vốn đầu", "không tính hòa")
        for i, (value, note) in enumerate(zip(values, notes)):
            self.kpi_values[i].config(text=value, fg=(GREEN if book.profit >= 0 else RED) if i in (1, 2) else TEXT)
            self.kpi_notes[i].config(text=note)
        if self.browser.connected:
            self.connection_text = self.browser.note or "Đã nối Chrome (driver)."
        elif not self.chrome_busy:
            self.connection_text = "Chế độ driver: bấm “Mở Chrome” để app mở Chrome và tự vào sảnh."
        self.connection_note.config(text=self.connection_text)
        self.connection_label.config(text=self._window_text(self._window_config()))
        self.open_button.config(state="disabled" if self.chrome_busy else "normal")
        self.attach_button.config(state="disabled" if self.chrome_busy else "normal")
        self.disconnect_button.config(text="Ngắt kết nối" if self.browser.mode == "cdp" else "Đóng")
        self.enter_lobby_button.config(state="disabled" if self.chrome_busy or self.sweep_busy else "normal")
        self.sweep_button.config(state="disabled" if self.chrome_busy or self.sweep_busy else "normal")
        self.auto_open_button.config(state="disabled" if self.chrome_busy or self.sweep_busy
                                     or self.open_table_busy else "normal")
        self.sweep_note.config(text=self.sweep_note_text)
        self._refresh_hunt_banner()
        if hasattr(self, "tables_note"):
            self.tables_note.config(text="Số bàn muốn chơi: %d · đã xong %d bàn trong phiên." % (
                self._tables_target(), self.tables_done), fg=MUTED)
        self.road_title.config(text=self.sim.table_name)
        plan = self.sim.pending or self.sim.plan()
        side = plan["side"]
        self.plan_side.config(text=E.SIDE_NAME.get(side, "CHỜ BÀN").upper(), fg=COLORS.get(side, MUTED))
        self.plan_stake.config(text=("%s VN2 · bậc %d/%d · %s" % (
            M.fmt_money(plan["stake"]), min(plan["ladder_step"] + 1, len(self.sim.ladder)),
            len(self.sim.ladder), "đã chốt" if self.sim.running else "dự kiến")) if side else "Chưa có kế hoạch")
        strong = plan["strong"]
        for i, label in enumerate(self.cycle_labels):
            cell_side = strong if i < ME.strong_hands(self.sim.profile) else (
                ("P" if strong == "B" else "B") if strong else None)
            active = i == plan["block_pos"] and side
            label.config(text=cell_side or str(i + 1), bg=COLORS.get(cell_side, BG) if active else BG,
                         fg="white" if active else COLORS.get(cell_side, MUTED))
        self.checks_label.config(text="\n".join(("Đạt · " if c["ok"] else "Chờ · ") + c["name"] for c in plan["checks"]))
        self._update_chip_status()
        self.sim_note.config(text=self.sim.note, fg=ACCENT if self.sim.running else MUTED)
        for key in self.canvases:
            self._draw_canvas(key)
        self._render_tables()
        self.history_note.config(text="%d tay · %d thắng / %d thua / %d hòa · Lãi/lỗ %s VN2\nSụt vốn lớn nhất: %s đ" % (
            book.hands_played, book.wins, book.losses, book.pushes, signed(book.profit), M.fmt_money(book.max_drawdown)))
        if self.history_size != len(self.sim.log):
            for item in self.history_tree.get_children():
                self.history_tree.delete(item)
            groups = {}
            for row in self.sim.log:
                group_key = row.get("table_key") or row.get("table") or "(B\u00e0n kh\u00f4ng r\u00f5)"
                groups.setdefault(group_key, []).append(row)
            for group_key, rows in groups.items():
                name = rows[-1].get("table") or str(group_key)
                wins = sum(row.get("outcome") == "win" for row in rows)
                losses = sum(row.get("outcome") == "loss" for row in rows)
                pushes = sum(row.get("outcome") == "push" for row in rows)
                profit = sum(float(row.get("delta", 0) or 0) for row in rows)
                parent = self.history_tree.insert("", "end", tags=("table_group",), values=(
                    "", "%s \u00b7 %d l\u01b0\u1ee3t" % (name, len(rows)), "", "%dT/%dB/%dH" % (wins, losses, pushes),
                    "", "", signed(profit), M.fmt_money(rows[-1].get("balance_after", 0))))
                for row in rows:
                    self.history_tree.insert(parent, "end", tags=(row.get("outcome", ""),), values=(
                        time.strftime("%H:%M:%S", time.localtime(row["time"])), "", row.get("hand", ""),
                        row.get("bet", ""), row.get("result", ""), M.fmt_money(row.get("stake", 0)),
                        signed(row.get("delta", 0)), M.fmt_money(row.get("balance_after", 0))))
            self.history_size = len(self.sim.log)

    def _draw_canvas(self, key):
        canvas = self.canvases[key]
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 20 or h < 20:
            return
        canvas.delete("all")
        if not self.sim.log:
            canvas.create_text(w / 2, h / 2, text="Chưa có dữ liệu đối chiếu từ site", fill=MUTED, font=("Segoe UI", 11))
            return
        values = [self.sim.money.start_capital] + [r["balance_after"] for r in self.sim.log]
        low, high = min(values), max(values)
        span = max(high - low, self.sim.money.unit)
        coords = []
        for i, v in enumerate(values):
            coords.extend((18 + i / (len(values) - 1) * (w - 36), h - 30 - (v - low) / span * (h - 52)))
        canvas.create_line(*coords, fill=ACCENT, width=3)
        canvas.create_text(18, 12, anchor="w", text="Vốn · " + M.fmt_money(values[-1]) + " VN2", fill=MUTED)

    def export_csv(self):
        if not self.sim.log:
            messagebox.showinfo("Chưa có dữ liệu", "Chưa có dữ liệu đối chiếu để xuất.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                          initialfile="bcr-live-%s.csv" % time.strftime("%Y%m%d-%H%M%S"))
        if not path:
            return
        fields = ("time", "armed_at", "table", "hand", "bet", "result", "stake", "delta", "outcome", "balance_after", "source")
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields)
                writer.writeheader()
                for item in self.sim.log:
                    row = {k: item[k] for k in fields}
                    if row["table"].startswith(("=", "+", "-", "@", "\t", "\r")):
                        row["table"] = "'" + row["table"]
                    writer.writerow(row)
        except OSError as exc:
            messagebox.showerror("Không xuất được", str(exc), parent=self.root)

    def apply_ui_scale(self):
        try:
            scale = int(self.ui_scale_var.get().strip())
            if not 50 <= scale <= 150:
                raise ValueError()
        except ValueError:
            messagebox.showerror("Tỉ lệ chưa hợp lệ", "Nhập tỉ lệ từ 50 đến 150.", parent=self.root)
            return
        self.ui_scale = scale
        self._save_state()
        messagebox.showinfo("Đã lưu tỉ lệ", "Mở lại ứng dụng để áp dụng tỉ lệ %d%%." % scale,
                            parent=self.root)

    def apply_block_mode(self):
        mode = self.block_mode_var.get()
        if mode == "alternate":
            mode = ME.BLOCK_MODE_REPEAT
            self.block_mode_var.set(mode)
        if mode not in (ME.BLOCK_MODE_RECALCULATE, ME.BLOCK_MODE_REPEAT):
            mode = ME.BLOCK_MODE_RECALCULATE
            self.block_mode_var.set(mode)
        self.sim.block_mode = mode
        self._sync_method_texts()
        self._save_state()
        self.refresh()

    def apply_settings(self):
        try:
            values = {k: (M.parse_money(v.get()) if k == "capital" else float(v.get().strip()))
                      for k, v in self.entries.items()}
            if not all(math.isfinite(v) for v in values.values()):
                raise ValueError("Vốn và chốt lời phải là số hợp lệ.\n"
                                 "Vốn nhập kiểu 20,000,000; chốt lời ≥ 1%.")
            if values["take_profit_pct"] < 1:
                raise ValueError("Chốt lời phải ≥ 1%.")
            profile = self.profile_var.get()
            stakes = ME.ladder_stakes(profile, self._ladder_from_inputs())
            if stakes[0] > values["capital"] or values["capital"] <= 0:
                raise ValueError("Vốn (%s) phải lớn hơn 0 và không nhỏ hơn bậc 1 (%s)." % (
                    M.fmt_money(values["capital"]), M.fmt_money(stakes[0])))
        except ValueError as exc:
            messagebox.showerror("Thông số chưa hợp lệ", str(exc), parent=self.root)
            return
        book = M.MoneyBook(unit=stakes[0], **values)
        for var, stake in zip(self.ladder_vars, stakes):
            var.set(M.fmt_input_money(stake))
        self.ladder_store[profile] = [var.get() for var in self.ladder_vars]
        self.entries["capital"].set(M.fmt_input_money(values["capital"]))
        if not messagebox.askyesno("Tạo phiên mới", "Lưu bản sao phiên hiện tại, dừng theo dõi và bắt đầu với vốn mới?", parent=self.root):
            return
        if not self._save_state():
            return
        try:
            self._archive_state()
        except OSError as exc:
            messagebox.showerror("Không lưu được bản sao", str(exc), parent=self.root)
            return
        self.sim = LiveSimulation(book, profile=profile, ladder=stakes,
                                  min_hands=self.sim.min_hands, max_hands=self.sim.max_hands,
                                  max_streak=self.sim.max_streak,
                                  live_exit_streak=self.sim.live_exit_streak,
                                  block_mode=self.block_mode_var.get())
        self.sim.on_arm = self.place_armed_bet
        self.tables_done = 0
        self._sync_cycle_strip()
        self._sync_method_texts()
        self.history_size = -1
        self._save_state()
        self.refresh()

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
            self.auto_place_var.set(bool(settings.get("auto_place", True)))
            self.auto_recover_var.set(bool(settings.get("auto_recover", True)))
            saved_mode = settings.get("ladder_mode", "manual")
            self.ladder_mode_var.set(saved_mode if saved_mode in ("manual", "multiply") else "manual")
            saved_base = settings.get("ladder_base")
            if isinstance(saved_base, str) and saved_base.strip():
                self.ladder_base_var.set(saved_base)
            low, high, streak = self.sim.set_road_filter(settings.get("hands_min", ME.MIN_HANDS),
                                                         settings.get("hands_max", ME.MAX_HANDS),
                                                         settings.get("max_streak", ME.MAX_STREAK_ALLOWED))
            self.hands_min_var.set(str(low))
            self.hands_max_var.set(str(high))
            self.streak_var.set(str(streak))
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
                                   live_exit_streak=self.sim.live_exit_streak,
                                  chrome_window=dict(width=window["width"], height=window["height"],
                                                     percent=window["percent"],
                                                     fullscreen=window["fullscreen"]),
                                  chrome_url=BR.START_URL, ui_scale=self.ui_scale),
                    money={key: getattr(book, key) for key in MONEY_FIELDS})
        try:
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.state_file)
            return True
        except OSError:
            self.sim.pause("Không lưu được phiên xuống ổ đĩa. Đã tạm dừng; dữ liệu vẫn còn trong ứng dụng.")
            return False

    def close(self):
        if self.closing:
            return
        self.exit_requested = True
        self.sim.pause()
        if self.chrome_busy or self.scan_busy or self.sweep_busy or self.open_table_busy:
            self.connection_note.config(text="Đang chờ tác vụ Chrome/Sảnh kết thúc để đóng an toàn…")
            self.root.after(100, self.close)
            return
        self._save_state()
        self.closing = True
        if self.poll_id:
            self.root.after_cancel(self.poll_id)
        self.connection_note.config(text="Đang ngắt kết nối và lưu phiên…" if self.browser.cdp_attached
                                    else "Đang đóng Chrome và lưu phiên…")
        def work():
            try:
                self.browser.stop()
            finally:
                self.events.put(("closed", None))
        threading.Thread(target=work, daemon=True).start()
        def finish():
            while not self.events.empty():
                kind, _ = self.events.get_nowait()
                if kind == "closed":
                    self.root.destroy()
                    return
            self.root.after(100, finish)
        self.root.after(100, finish)


def main():
    root = tk.Tk()
    BCRApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
