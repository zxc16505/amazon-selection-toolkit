# -*- coding: utf-8 -*-
"""纯函数与小工具。不依赖外部状态，便于单元测试。"""
from __future__ import annotations

import json
import re
from pathlib import Path


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def dump_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_price(raw: str | None) -> float | None:
    """把 '7,32 €' / '$11.99' / '1.234,56' 解析成 float。"""
    if not raw:
        return None
    s = re.sub(r"[^\d.,]", "", raw)
    if not s:
        return None
    if "," in s and "." in s:
        # 千分位和小数位都有，靠最后一个分隔符判断小数位
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # 只有逗号：按最后一段两位小数判断
        tail = s.split(",")[-1]
        s = s.replace(",", ".") if len(tail) == 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def retry_after(status_code: int, resp_headers: dict | None, idx: int,
                backoff: tuple[int, ...]) -> float:
    """429/503 退避时间。优先读 Retry-After，没有则按 backoff 递增。"""
    v = (resp_headers or {}).get("Retry-After")
    if v and str(v).strip().isdigit():
        return min(int(v), 300)
    return backoff[min(idx, len(backoff) - 1)]


def is_block(status_code: int, body: bytes, block_markers: tuple[str, ...],
             block_max_len: int) -> bool:
    """命中「页面小 + 拦截特征」两个条件才判拦截，避免误伤正常页。"""
    if len(body) > block_max_len:
        return False
    return any(m in body.decode("utf-8", "replace") for m in block_markers)
