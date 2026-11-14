# -*- coding: utf-8 -*-
"""Điểm khởi động: tạo cửa sổ Tk, ghép View + Controller rồi chạy vòng poll."""

import tkinter as tk

from bcr.controllers.app import BCRApp
from bcr.config import STATE_FILE


def main():
    root = tk.Tk()
    BCRApp(root)
    root.mainloop()
