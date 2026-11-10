# -*- coding: utf-8 -*-
"""榜单页解析。依赖 BeautifulSoup，纯函数。"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from .config import Market


def price_from(node) -> float | None:
    """取现价。

    a-offscreen 里的文本是 "$1787"（部分币种下小数点被吞），不能直接用；
    真实价格由 a-price-whole + a-price-fraction 两段拼成。
    a-text-price 是划线原价，必须排除。
    """
    from .utils import parse_price
    box = node.select_one("span.a-price:not(.a-text-price)")
    if box is None:
        el = node.select_one("span[class*=p13n-sc-price]")
        return parse_price(el.get_text() if el else "")
    whole = box.select_one("span.a-price-whole")
    if whole is not None:
        w = re.sub(r"[^\d,.]", "", whole.get_text()).rstrip(".,")
        frac = box.select_one("span.a-price-fraction")
        raw = w + ("." + re.sub(r"\D", "", frac.get_text()) if frac else "")
        return parse_price(raw)
    return parse_price(box.get_text(" ", strip=True))


def parse_listing(html: str, market: Market, top: int) -> list[dict]:
    """从榜单页取前 top 条。每条含 rank / asin / title / price / currency / rating / reviews。"""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for node in soup.select("div#gridItemRoot")[:top]:
        rank_txt = node.select_one(".zg-bdg-text")
        holder = node.select_one("div[data-asin]")
        link = node.select_one("a[href*='/dp/']")
        asin = (holder.get("data-asin") if holder else None)
        if not asin and link:
            m = re.search(r"/dp/([A-Z0-9]{10})", link.get("href", ""))
            asin = m.group(1) if m else None
        if not rank_txt or not asin:
            continue
        img = node.select_one("img[alt]")
        star_el = node.select_one("span.a-icon-alt")
        rev_el = (node.select_one("a[href*=customerReviews] span")
                  or node.select_one("span.a-size-small"))
        m = re.search(r"(\d[\d.]*)\s*out of 5", star_el.get_text()) if star_el else None
        rows.append({
            "rank": int(re.sub(r"\D", "", rank_txt.get_text())),
            "asin": asin,
            "title": (img.get("alt") if img else "").strip(),
            "price": price_from(node),
            "currency": market.currency,
            "rating": float(m.group(1)) if m else None,
            "reviews": int(re.sub(r"\D", "", rev_el.get_text()) or 0) if rev_el else 0,
        })
    return rows
