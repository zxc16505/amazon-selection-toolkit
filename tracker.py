# -*- coding: utf-8 -*-
"""向后兼容入口。python tracker.py 等价于 python -m amazon_tracker.cli。"""
from amazon_tracker.cli import main

raise SystemExit(main())
