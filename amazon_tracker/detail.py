# -*- coding: utf-8 -*-
"""商品详情页解析。只读，不涉及任何写操作。"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from .utils import parse_price

if TYPE_CHECKING:
    from .config import Market


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def _brand(soup) -> str:
    raw = _text(soup.select_one("#bylineInfo"))
    for prefix in ("Visit the ", "Brand: ", "Store: "):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    return raw.replace(" Store", "").strip()


def _price(soup) -> tuple[float | None, str]:
    """详情页价格。先尝试 whole+fraction 拼接，再退 a-offscreen 文本。"""
    box = soup.select_one("#corePrice_feature_div .a-price, #corePriceDisplay_desktop_feature_div .a-price")
    if box is not None:
        whole = box.select_one("span.a-price-whole")
        if whole is not None:
            w = re.sub(r"[^\d,.]", "", whole.get_text()).rstrip(".,")
            frac = box.select_one("span.a-price-fraction")
            raw = w + ("." + re.sub(r"\D", "", frac.get_text()) if frac else "")
            return parse_price(raw), (box.select_one("span.a-price-symbol").get_text(strip=True)
                                      if box.select_one("span.a-price-symbol") else "")
    off = soup.select_one("#corePrice_feature_div .a-offscreen")
    return parse_price(_text(off)), ""


def _rating(soup) -> float | None:
    for sel in ("#acrPopover", "span[data-hook=rating-out-of-text]"):
        node = soup.select_one(sel)
        if node is None:
            continue
        txt = node.get("title") or _text(node)
        m = re.search(r"(\d+(?:\.\d+)?)\s*out of 5", txt)
        if m:
            return float(m.group(1))
    return None


def _reviews(soup) -> int:
    txt = _text(soup.select_one("#acrCustomerReviewText"))
    m = re.search(r"([\d,]+)", txt)
    return int(m.group(1).replace(",", "")) if m else 0


def _category(soup) -> str:
    parts = [_text(a) for a in soup.select("#wayfinding-breadcrumbs_feature_div li a")]
    return " › ".join(p for p in parts if p)


def _bullets(soup) -> list[str]:
    out = []
    for li in soup.select("#feature-bullets li span.a-list-item"):
        t = _text(li)
        if t and t not in out:
            out.append(t)
    return out


def parse_detail(html: str, market: Market) -> dict:
    """解析商品详情页。返回标题 / 品牌 / 价格 / 五点 / 类目路径等。"""
    soup = BeautifulSoup(html, "html.parser")
    price, symbol = _price(soup)
    img = soup.select_one("#landingImage")
    return {
        "asin": (soup.select_one("input#ASIN") or {}).get("value") if soup.select_one("input#ASIN") else None,
        "title": _text(soup.select_one("#productTitle")),
        "brand": _brand(soup),
        "price": price,
        "currency": market.currency,
        "price_symbol": symbol,
        "rating": _rating(soup),
        "reviews": _reviews(soup),
        "category_path": _category(soup),
        "bullets": _bullets(soup),
        "availability": _text(soup.select_one("#availability span")),
        "image": (img.get("src") if img else "")[:300],
    }
