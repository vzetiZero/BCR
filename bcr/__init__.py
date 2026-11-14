# -*- coding: utf-8 -*-
"""BCR — bot bàn Baccarat/LONG-LONG theo kiến trúc MVC.

Tầng:
    bcr.models       -> domain thuần (không tkinter, không selenium)
    bcr.views        -> dựng giao diện tkinter, không quyết định cược
    bcr.controllers  -> điều khiển vòng đời, nối View với Model
    bcr.adapters     -> trình duyệt/CDP/chuột: lớp IO ra thế giới bên ngoài
"""

__version__ = "2.0.0"
