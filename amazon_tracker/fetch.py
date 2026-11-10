# -*- coding: utf-8 -*-
"""HTTP 采集层。唯一发网络请求的模块。"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import requests

from .utils import is_block, retry_after

if TYPE_CHECKING:
    from .config import Config, Market


class RiskControl(Exception):
    """页面出现拦截特征，必须停止全部后续请求。"""


def fetch(session: requests.Session, market: Market, list_type_path: str,
          category: str, cache: dict, cfg: "Config") -> tuple[str | None, str]:
    """请求榜单页。返回 (html 或 None 表示 304, 说明)。

    调用方负责：已决定今天还没采过、愿意发这个请求。
    这里只关心单次请求，重试 2 次（含首轮）。命中拦截抛 RiskControl。
    """
    url = "https://" + market.host + list_type_path.format(category)
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept-Language": market.accept_language,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://" + market.host + "/",
    }
    cached = cache.get(url) or {}
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]

    last = "unknown"
    for attempt in range(cfg.max_attempts):
        if attempt:
            time.sleep(cfg.backoff_seconds[min(attempt - 1, len(cfg.backoff_seconds) - 1)])
        t0 = time.time()
        try:
            r = session.get(url, timeout=30, headers=headers,
                            cookies={"i18n-prefs": market.currency,
                                     "lc-main": market.accept_language.split(",")[0].replace("-", "_")})
        except requests.RequestException as e:
            last = type(e).__name__
            continue

        ms = round((time.time() - t0) * 1000)
        if r.status_code == 304:
            return None, f"304 Not Modified（{ms}ms）"
        if is_block(r.status_code, r.content, cfg.block_markers, cfg.block_max_len):
            raise RiskControl(f"{market.code} 返回拦截页（HTTP {r.status_code}，{len(r.content)} 字节）")
        if r.status_code in (429, 503):
            wait = retry_after(r.status_code, r.headers, attempt, cfg.backoff_seconds)
            last = f"HTTP {r.status_code} → 退避 {wait}s"
            if attempt == cfg.max_attempts - 1:
                break
            time.sleep(wait)
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"
            break

        cache[url] = {"etag": r.headers.get("ETag"),
                      "last_modified": r.headers.get("Last-Modified")}
        return r.text, f"200 OK（{len(r.content)}B, {ms}ms）"
    raise RuntimeError(last)
