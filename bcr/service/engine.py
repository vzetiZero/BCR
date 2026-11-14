# -*- coding: utf-8 -*-
"""Tầng Service (transport): JSON-RPC qua stdin/stdout giữa Electron và Controller.

Mỗi trò là một tiến trình riêng do Electron spawn, có profile Chrome, cổng CDP và
file cấu hình/phiên riêng, nên thao tác ở tab này không ảnh hưởng tab kia.

Giao thức (mỗi dòng một JSON):

    yêu cầu : {"id": 1, "method": "tool.start", "params": {}}
    trả lời : {"id": 1, "ok": true, "result": {...}}
              {"id": 1, "ok": false, "error": "..."}
    sự kiện : {"event": "state", "data": {...}}
              {"event": "log", "data": {"level": "info", "text": "..."}}

Module này chỉ lo đọc/ghi JSON và chọn controller; mọi nghiệp vụ nằm ở
``bcr.controllers``. Chạy tay để kiểm tra:

    python -m bcr.service.engine --game baccarat
"""

import argparse
import json
import pathlib
import sys
import threading

from bcr.adapters import browser as BR
from bcr.controllers.baccarat import BaccaratController
from bcr.controllers.longlong import LongLongController

ROOT = pathlib.Path(__file__).resolve().parents[2]

GAMES = {
    # key: (cổng CDP, thư mục profile Chrome, file cấu hình/phiên)
    "baccarat": (9222, "chrome_profile", "bcr_state_baccarat.json"),
    "longlong": (9223, "chrome_profile_longlong", "bcr_state_longlong.json"),
}



# Tên cũ của lớp controller: giữ alias để script/kiểm thử cũ vẫn chạy.
BaccaratService = BaccaratController
LongLongService = LongLongController


class Emitter:
    """Ghi JSON ra stdout theo dòng, có khoá để nhiều thread không xen kẽ."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.lock = threading.Lock()

    def send(self, payload):
        line = json.dumps(payload, ensure_ascii=False)
        with self.lock:
            self.stream.write(line + "\n")
            self.stream.flush()

    def event(self, name, data):
        self.send({"event": name, "data": data})

    def reply(self, request_id, result=None, error=None):
        if error is None:
            self.send({"id": request_id, "ok": True, "result": result})
        else:
            self.send({"id": request_id, "ok": False, "error": str(error)})



def build_service(game, emit, state_path):
    """Tạo controller đúng trò và bật vòng nền (nếu controller có)."""
    if game == "longlong":
        controller = LongLongController(state_path, emit)
    elif game == "baccarat":
        controller = BaccaratController(state_path, emit)
    else:
        raise SystemExit("Trò không hỗ trợ: %s" % game)
    if hasattr(controller, "start"):
        controller.start()      # nhịp nền: đọc bàn/scan và báo Chrome đã đóng
    return controller


def dispatch(service, method, params):
    """Chuyển lệnh xuống controller (mọi controller đều có ``dispatch``)."""
    return service.dispatch(method, params or {})


def main(argv=None):
    # Ép UTF-8 cho stdin/stdout: Electron đọc JSON theo dòng, không phụ thuộc locale Windows.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="BCR engine (JSON qua stdin/stdout)")
    parser.add_argument("--game", required=True, choices=sorted(GAMES))
    parser.add_argument("--cdp-port", type=int, default=None)
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--state", default=None)
    args = parser.parse_args(argv)

    port, profile, state_name = GAMES[args.game]
    # Cách ly từng trò: đặt lại hằng số của adapter TRƯỚC khi tạo service.
    BR.CDP_PORT = args.cdp_port or port
    BR.PROFILE_DIR = str(ROOT / (args.profile_dir or profile))
    state_path = pathlib.Path(args.state) if args.state else ROOT / state_name

    emit = Emitter()
    service = build_service(args.game, emit, state_path)
    emit.event("ready", {"game": args.game, "cdp_port": BR.CDP_PORT,
                         "profile": BR.PROFILE_DIR})
    emit.event("state", service.snapshot())

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            emit.reply(None, error="JSON không hợp lệ")
            continue
        request_id = request.get("id")
        method = request.get("method") or ""
        if method in ("shutdown", "quit"):
            emit.reply(request_id, {"ok": True})
            break
        result, error = dispatch(service, method, request.get("params") or {})
        emit.reply(request_id, result, error)
    service.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
