# -*- coding: utf-8 -*-
"""Cho phép chạy engine bằng ``python -m bcr --game baccarat|longlong``."""

import sys

from bcr.service.engine import main

if __name__ == "__main__":
    sys.exit(main())
