# -*- coding: utf-8 -*-
"""BCR - bot bàn Baccarat / LONG-LONG theo kiến trúc MVC, giao diện Electron.

Tầng (trên xuống):

    ui/                 View       - Electron (HTML/JS/CSS): chỉ vẽ và gửi lệnh
    bcr.service         Transport  - JSON-RPC stdin/stdout, mỗi trò một tiến trình
    bcr.controllers     Controller - điều phối Model + Adapter (composition root)
    bcr.adapters        Adapter    - Chrome/Selenium/CDP: đọc DOM, bấm chuột, đặt chip
    bcr.models          Model      - luật cầu, thang cược, sổ vốn (thuần, không I/O)

Luồng một lệnh: Electron -> preload/IPC -> bcr.service.engine -> controller
-> adapter/model, rồi sự kiện ``state``/``log`` trả ngược lên giao diện.
"""
