# -*- coding: utf-8 -*-
"""Điểm vào tương thích: ``import app`` trỏ thẳng về ``bcr.controllers.app``.

Toàn bộ ứng dụng đã chia theo MVC trong package ``bcr``:

    bcr/models       -> LiveSimulation, method, engine, money, chips, longlong
    bcr/views        -> main_window, render, longlong_panel (tkinter, không quyết định cược)
    bcr/controllers  -> app, hunt, browsers, betting, session, settings, state_store
    bcr/adapters     -> browser, lobby_stream, table_bets, chuột, reader.js

File này chỉ alias cùng một đối tượng module, nhờ vậy ``app.BCRApp``,
``app.LiveSimulation``, mọi lệnh ``patch(...)`` và import cũ vẫn trỏ đúng module thật.

Chạy bot: ``python app.py`` (hoặc ``Chay_BCR.bat``) -> ``bcr.app_main.main()``.
"""

import sys

from bcr.controllers import app as _module

sys.modules[__name__] = _module

if __name__ == "__main__":
    from bcr.app_main import main

    sys.exit(main())
