# -*- coding: utf-8 -*-
"""Tầng Model: vòng đời một phiên đánh + các hàm thuần dùng chung.

Chuyển nguyên văn từ ``app.py`` cũ (``LiveSimulation`` + helper) để View/Controller chỉ
còn gọi lại. Không import tkinter, không import adapter: model chạy độc lập và kiểm
thử được bằng dữ liệu thuần.
"""

import re
import time

from bcr.config import *  # noqa: F401,F403  (BET_GATE_*, PLACE_*, LIVE_STALE, FAST_TABLE_MARKERS)
from bcr.models import engine as E
from bcr.models import method as ME
from bcr.models import money as M


def is_fast_table(label):
    """True với bàn có nhịp cược quá nhanh để bấm chip kịp (Siêu Tốc Độ/Super Speed)."""
    text = str(label or "").casefold()
    return any(marker in text for marker in FAST_TABLE_MARKERS)

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



class LiveSimulation:
    def __init__(self, book=None, profile=ME.DEFAULT_PROFILE, ladder=None,
                 min_hands=ME.MIN_HANDS, max_hands=ME.MAX_HANDS,
                 max_streak=ME.MAX_STREAK_ALLOWED,
                 live_exit_streak=ME.DEFAULT_LIVE_EXIT_STREAK,
                 min_lead=ME.DEFAULT_MIN_LEAD,
                 block_mode=ME.BLOCK_MODE_RECALCULATE):
        self.money = book or M.MoneyBook()
        self.profile = profile
        self.ladder = ME.ladder_stakes(profile, ladder)
        self.min_hands, self.max_hands = ME.normalize_hand_range(min_hands, max_hands)
        self.max_streak = ME.normalize_streak(max_streak)
        self.live_exit_streak = ME.normalize_streak(live_exit_streak, ME.DEFAULT_LIVE_EXIT_STREAK)
        self.min_lead = ME.normalize_min_lead(min_lead)
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
                       self.min_hands, self.max_hands, self.max_streak, self.block_mode,
                       self.min_lead)

    def set_road_filter(self, min_hands, max_hands, max_streak, min_lead=ME.DEFAULT_MIN_LEAD):
        """Đổi bộ lọc cầu (khoảng tay đã đi + số tay bệt tối đa); kẹp trong biên của method."""
        self.min_hands, self.max_hands = ME.normalize_hand_range(min_hands, max_hands)
        self.max_streak = ME.normalize_streak(max_streak)
        self.min_lead = ME.normalize_min_lead(min_lead)
        return self.min_hands, self.max_hands, self.max_streak, self.min_lead

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
        if hasattr(self, "_trace"):
            self._trace("hand_not_placed", dict(table=self.table_key, hand=len(letters),
                                                side=plan.get("side"),
                                                stake=plan.get("stake"), reason=str(reason)[:120]))
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
        """Rời bàn đang chơi CHỈ khi bệt hiện tại chạm ngưỡng thoát trong bàn.

        Khoảng 10-50 tay và luật "chưa từng có 3-3" là bộ lọc CHỌN bàn ở sảnh, không
        phải điều kiện sống trong bàn. Áp lại chúng lên cầu live làm app bỏ một bàn
        đang chơi bình thường ngay khi vượt quá ``max_hands`` hoặc vừa xuất hiện cầu
        3-3 — đúng hiện tượng "vào bàn vài giây rồi bị đá" trong bcr_trace.jsonl.
        Trong bàn chỉ còn ngưỡng thoát riêng (mặc định 7 tay bệt), khớp dòng cài đặt
        "Trong bàn: thoát khi bệt đạt N tay".
        """
        runs = E.run_lengths(letters)
        # Chỉ xét VỆT ĐANG CHẠY ở tay mới nhất (runs[-1]); dùng vệt dài nhất trong cả
        # lịch sử khiến bàn bị đá ngay khi vào nếu shoe từng có bệt ≥ ngưỡng từ lâu,
        # dù cầu hiện tại bình thường — đúng lỗi "vào bàn vài giây rồi bị đá" lặp lại
        # cùng một bàn trong bcr_trace.jsonl và không bao giờ kịp đặt chip.
        streak_side, streak_len = runs[-1] if runs else (None, 0)
        if streak_len < self.live_exit_streak:
            return False
        self.filter_exit_done = True
        self.cycle_done = True
        self.pause("Bàn không còn đạt điều kiện lọc (bệt %s %d tay, ngưỡng thoát %d tay) · "
                   "rời bàn và quét bàn khác." % (
                       E.SIDE_NAME[streak_side], streak_len, self.live_exit_streak))
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
