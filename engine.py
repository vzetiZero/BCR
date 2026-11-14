# -*- coding: utf-8 -*-
"""Shim tương thích: ``import engine`` trỏ thẳng về ``bcr.models.engine``.

Module thật đã chuyển vào package ``bcr`` theo kiến trúc MVC. File này chỉ alias
cùng một đối tượng module, nhờ vậy mọi lệnh ``patch(...)`` và import cũ vẫn trỏ
đúng module thật (không phải bản sao thuộc tính).
"""

import sys

from bcr.models import engine as _module

sys.modules[__name__] = _module
