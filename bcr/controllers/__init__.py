# -*- coding: utf-8 -*-
"""Tầng Controller: điều phối Model + Adapter; không dùng tkinter, không biết Electron.

    baccarat.py       BaccaratController - cầu 3-3/3-2, lọc bàn, tự đặt chip, săn bàn
    longlong.py       LongLongController - hai bát vàng/đỏ, thang 8 bậc
    longlong_auto.py  HandTracker        - nhận diện ván mới, chống chốt trùng

Mỗi controller tự mô tả lệnh của mình trong ``dispatch(method, params)``.
"""
