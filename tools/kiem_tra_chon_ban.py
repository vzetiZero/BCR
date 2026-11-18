# -*- coding: utf-8 -*-
"""Kiểm tra THỰC TẾ ở mức chọn bàn (chạy với Chrome/sảnh thật của bạn).

Cách dùng (Chrome đang mở sảnh Baccarat với CDP 9222):

    python tools/kiem_tra_chon_ban.py              # chỉ đọc sảnh, in bảng đạt/loại
    python tools/kiem_tra_chon_ban.py --vao-ban    # đọc sảnh rồi bấm ô bàn ĐẦU TIÊN đạt
                                                   # và xác nhận cầu (KHÔNG đặt cược)

Mỗi bàn được chấm bằng đúng bộ lọc của app (5 điều kiện) + 4 cổng kỹ thuật
(dữ liệu ≤ 30 giây, cầu đủ tin, không lỗi/không xung đột, bỏ bàn Siêu Tốc Độ).
--vao-ban dùng đúng quy trình của nút “VÀO BÀN HỢP LỆ” nhưng dừng ở bước xác nhận:
không arm kế hoạch, không bấm chip.
"""

import argparse
import pathlib
import socket
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bcr.adapters import browser as BR
from bcr.models import method as ME
from bcr.models.live_sim import is_fast_table, road_is_current, row_problem

ENTRY_WAIT = 75


def cdp_open(port=BR.CDP_PORT, timeout=1.0):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class _Emitter:
    def event(self, name, data):
        pass

    def reply(self, request_id, result=None, error=None):
        pass

    def send(self, payload):
        pass


def verdicts(rows, cfg):
    out = []
    for row in rows:
        letters = row.get("letters") or ""
        hands = len(letters)
        info = ME.check_table(list(letters), cfg["min_hands"], cfg["max_hands"],
                              cfg["max_streak"], cfg["min_lead"],
                              max_lead=cfg["max_lead"], tie_side=cfg["tie_side"])
        reasons = []
        problem = row_problem(row)
        if problem:
            reasons.append(problem)
        if not road_is_current(row):
            reasons.append("cầu chưa đủ tin (chưa ổn định/chưa khớp 2 nguồn)")
        if is_fast_table(row.get("label")):
            reasons.append("bàn Siêu Tốc Độ")
        if not info["eligible"]:
            reasons.append("không đạt 5 điều kiện (%s)" % info.get("reason", ""))
        out.append({"row": row, "info": info, "reasons": reasons, "ok": not reasons})
    return out


def print_board(items):
    width = max([len(str(item["row"].get("label"))) for item in items] + [8])
    print("%-*s %5s %5s %-7s %s" % (width, "BÀN", "TAY", "LỆCH", "MẠNH", "KẾT LUẬN"))
    for item in items:
        row, info = item["row"], item["info"]
        print("%-*s %5d %5s %-7s %s" % (
            width, str(row.get("label"))[:width], len(row.get("letters") or ""),
            info.get("lead"), ME.SIDE_NAME.get(info.get("strong"), "—"),
            "ĐẠT" if item["ok"] else "loại · " + "; ".join(item["reasons"])))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vao-ban", action="store_true",
                    help="bấm ô bàn đầu tiên ĐẠT rồi xác nhận cầu (không đặt cược)")
    ap.add_argument("--favorites", action="store_true", help="chỉ quét bàn Ưa thích")
    ap.add_argument("--timeout", type=float, default=ENTRY_WAIT,
                    help="giây chờ xác nhận cầu sau khi mở bàn (mặc định 75)")
    args = ap.parse_args()

    if not cdp_open():
        print("Không thấy Chrome CDP ở cổng %d." % BR.CDP_PORT)
        print("Mở Chrome kèm --remote-debugging-port=%d, đăng nhập và vào sảnh Baccarat."
              % BR.CDP_PORT)
        return 2

    # Dùng đúng controller/service thật; chỉ tắt đặt chip để không đụng tiền.
    from bcr.service import engine

    service = engine.BaccaratService(pathlib.Path(tempfile.mkdtemp()) / "probe.json", _Emitter())
    service.config_set({"auto_place": False})
    service.tool_running = False
    if not service.browser.connected:
        service.browser.attach()
    if not service.browser.connected:
        print("Chưa gắn được Chrome CDP:", getattr(service.browser, "note", "")
              or getattr(service.browser, "last_error", "") or "không rõ lý do")
        return 2

    ok, note = service.stream.find_lobby_tab()
    if not ok:
        print("Không nhận được tab sảnh:", note)
        return 2

    print("Đang quét sảnh (cuộn hết danh sách, có thể mất tới 75 giây)…")
    rows = service.stream.sweep(favorites_only=args.favorites)
    if not rows:
        print("Không đọc được bàn:", service.stream.last_error or "sảnh chưa có dữ liệu")
        return 2

    cfg = {
        "min_hands": service.sim.min_hands, "max_hands": service.sim.max_hands,
        "max_streak": service.sim.max_streak, "min_lead": service.sim.min_lead,
        "max_lead": service.sim.max_lead, "tie_side": service.sim.tie_side,
    }
    items = verdicts(rows, cfg)
    print("\nĐã đọc %d bàn · %d bàn ĐẠT\n" % (
        len(items), sum(1 for item in items if item["ok"])))
    print_board(items)

    passed = [item for item in items if item["ok"]]
    if not passed:
        print("\nKhông có bàn đạt chuẩn ngay lúc này.")
        return 1
    if not args.vao-ban:
        print("\nBàn sẽ được chọn: %s" % passed[0]["row"].get("label"))
        return 0

    table_id = passed[0]["row"].get("table_id")
    print("\nBấm ô bàn %s…" % passed[0]["row"].get("label"))
    ok, note = service.stream.open_table(table_id)
    print(note)
    if not ok:
        return 1

    letters = service.stream and passed[0]["row"].get("letters")
    print("Chờ xác nhận cầu trong bàn (2 lần đọc giống nhau + cùng shoe), tối đa %ds…"
          % args.timeout)
    deadline = time.time() + args.timeout
    last = None
    while time.time() < deadline:
        reports = service.browser.scan()
        tables = [table for report in reports for table in report.get("tables", [])]
        row = next((table for table in tables if table.get("id") == table_id), None)
        if row:
            service.tables[table_id] = row
            problem = row_problem(row)
            same = (letters or "")[:min(len(letters or ""), len(row.get("letters") or ""))] == \
                   (row.get("letters") or "")[:min(len(letters or ""), len(row.get("letters") or ""))]
            state = (row.get("source"), row.get("screen_stable"), problem, same,
                     len(row.get("letters") or ""))
            if state != last:
                print("  đọc được %d tay · nguồn %s · ổn định %s%s%s" % (
                    state[4], row.get("source"), row.get("screen_stable"),
                    " · lỗi: " + problem if problem else "",
                    "" if same else " · KHÁC SHOE"))
                last = state
            if row.get("source") == "evo-bead" and row.get("screen_stable", 0) >= 2 \
                    and not problem and same:
                print("\nĐẠT: đã vào đúng bàn, cầu khớp shoe. Chưa đặt cược (đúng thiết kế).")
                return 0
        time.sleep(1.5)
    print("\nHết %ds mà chưa xác nhận được cầu trong bàn." % args.timeout)
    return 1


if __name__ == "__main__":
    sys.exit(main())
