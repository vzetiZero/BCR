# -*- coding: utf-8 -*-
"""Shim tương thích: ``import table_bets`` trỏ thẳng về ``bcr.adapters.table_bets``.

Module thật đã chuyển vào package ``bcr`` theo kiến trúc MVC. File này chỉ alias
cùng một đối tượng module, nhờ vậy mọi lệnh ``patch(...)`` và import cũ vẫn trỏ
đúng module thật (không phải bản sao thuộc tính).
"""

import sys

from bcr.adapters import table_bets as _module

sys.modules[__name__] = _module
