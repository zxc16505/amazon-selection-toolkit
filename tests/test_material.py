# -*- coding: utf-8 -*-
"""上架素材生成 + 详情页解析的离线测试。全部不发网络请求。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amazon_tracker.config import Market
from amazon_tracker.detail import parse_detail
from amazon_tracker.material import (build_material, check_listing, extract_keywords,
                                     price_band, render_markdown)


DETAIL_HTML = """
<html><body>
<input id="ASIN" value="B0TARGET01"/>
<span id="productTitle"> Wireless Bluetooth Headphones with Noise Cancelling, 40H Playtime </span>
<div id="bylineInfo">Visit the SoundCore Store</div>
<div id="corePrice_feature_div">
  <span class="a-price">
    <span class="a-price-symbol">$</span>
    <span class="a-price-whole">59.</span>
    <span class="a-price-fraction">99</span>
  </span>
</div>
<span id="acrPopover" title="4.5 out of 5 stars"></span>
<span id="acrCustomerReviewText">1,234 ratings</span>
<div id="wayfinding-breadcrumbs_feature_div">
  <ul><li><a>Electronics</a></li><li><a>Headphones</a></li></ul>
</div>
<div id="feature-bullets">
  <ul>
    <li><span class="a-list-item">Active noise cancelling blocks out background noise</span></li>
    <li><span class="a-list-item">40 hours playtime with fast charging support</span></li>
    <li><span class="a-list-item">Bluetooth 5.3 stable connection for daily use</span></li>
  </ul>
</div>
<div id="availability"><span>In Stock</span></div>
<img id="landingImage" src="https://example.com/img.jpg"/>
</body></html>
"""

RULES = {
    "title_max_chars": 200,
    "bullet_max_chars": 500,
    "banned_phrases": ["best seller", "free shipping", "guarantee"],
    "category_rules": {
        "electronics": [
            {"pattern": r"\b(cure|treat|FDA[-\s]?approved)\b",
             "message": "电子类目不得宣称医疗功效或未获批资质"}
        ]
    },
}


class TestParseDetail(unittest.TestCase):
    def setUp(self):
        self.m = Market("com", "www.amazon.com", "USD", "en-US,en;q=0.9")
        self.d = parse_detail(DETAIL_HTML, self.m)

    def test_fields(self):
        self.assertEqual(self.d["asin"], "B0TARGET01")
        self.assertIn("Headphones", self.d["title"])
        self.assertEqual(self.d["brand"], "SoundCore")
        self.assertEqual(self.d["price"], 59.99)
        self.assertEqual(self.d["rating"], 4.5)
        self.assertEqual(self.d["reviews"], 1234)
        self.assertEqual(self.d["category_path"], "Electronics › Headphones")
        self.assertEqual(len(self.d["bullets"]), 3)
        self.assertEqual(self.d["availability"], "In Stock")


class TestPriceBand(unittest.TestCase):
    def test_band(self):
        b = price_band([10.0, 30.0, 20.0])
        self.assertEqual(b["min"], 10.0)
        self.assertEqual(b["median"], 20.0)
        self.assertEqual(b["max"], 30.0)

    def test_too_few(self):
        self.assertIsNone(price_band([10.0]))
        self.assertIsNone(price_band([]))


class TestKeywords(unittest.TestCase):
    def test_frequency_and_stopwords(self):
        texts = ["Wireless Bluetooth Headphones with Noise Cancelling",
                 "Wireless Headphones for Running, Bluetooth 5.3"]
        kws = extract_keywords(texts, top_n=10)
        phrases = [k["phrase"] for k in kws]
        self.assertIn("wireless", phrases)
        self.assertNotIn("with", phrases)
        self.assertNotIn("for", phrases)

    def test_bigram(self):
        texts = ["noise cancelling headphones", "noise cancelling earbuds"]
        kws = extract_keywords(texts, top_n=10)
        self.assertIn("noise cancelling", [k["phrase"] for k in kws])

    def test_empty(self):
        self.assertEqual(extract_keywords([]), [])


class TestCheckListing(unittest.TestCase):
    def test_clean(self):
        issues = check_listing("Wireless Headphones", ["a" * 10] * 3, 59.9,
                               "electronics", RULES)
        self.assertEqual([i for i in issues if i["level"] == "error"], [])

    def test_banned_phrase(self):
        issues = check_listing("Best Seller Wireless Headphones", [], 10.0,
                               "electronics", RULES)
        codes = [i["code"] for i in issues]
        self.assertIn("BANNED_PHRASE", codes)

    def test_title_too_long(self):
        issues = check_listing("x" * 201, [], 10.0, "electronics", RULES)
        self.assertIn("TITLE_TOO_LONG", [i["code"] for i in issues])

    def test_category_claim(self):
        issues = check_listing("Headphones", ["this device can cure headache"], 10.0,
                               "electronics", RULES)
        self.assertIn("CATEGORY_CLAIM", [i["code"] for i in issues])

    def test_bullets_few(self):
        issues = check_listing("Headphones", ["only one"], 10.0, "electronics", RULES)
        self.assertIn("BULLETS_FEW", [i["code"] for i in issues])

    def test_no_price(self):
        issues = check_listing("Headphones", ["a"] * 3, None, "electronics", RULES)
        self.assertIn("NO_PRICE", [i["code"] for i in issues])


class TestBuildAndRender(unittest.TestCase):
    def setUp(self):
        self.m = Market("com", "www.amazon.com", "USD", "en-US,en;q=0.9")
        self.target = parse_detail(DETAIL_HTML, self.m)
        self.comps = [
            {"asin": "C1", "title": "Wireless Bluetooth Headphones Noise Cancelling",
             "price": 49.99, "bullets": ["Bluetooth 5.3 stable connection"], "currency": "USD"},
            {"asin": "C2", "title": "Bluetooth Headphones with Noise Cancelling",
             "price": 79.99, "bullets": ["40 hours playtime fast charging"], "currency": "USD"},
        ]
        self.mat = build_material(self.target, self.comps, "electronics", RULES)

    def test_structure(self):
        self.assertEqual(self.mat["competitor_count"], 2)
        self.assertEqual(self.mat["price_band"]["min"], 49.99)
        self.assertTrue(self.mat["keywords"])
        self.assertTrue(self.mat["suggestions"])

    def test_price_inside_band(self):
        self.assertTrue(any("落在竞品价格带" in s for s in self.mat["suggestions"]))

    def test_render_mentions_no_upload(self):
        md = render_markdown(self.mat, "com", "2026-11-10")
        self.assertIn("不含任何上架/提交动作", md)
        self.assertIn("B0TARGET01", md)

    def test_render_with_violation(self):
        bad = dict(self.target, title="Best Seller Headphones")
        mat = build_material(bad, self.comps, "electronics", RULES)
        md = render_markdown(mat, "com", "2026-11-10")
        self.assertIn("[阻断]", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
