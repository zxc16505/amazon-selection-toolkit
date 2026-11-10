"""单元测试：所有纯函数。不发网络请求。"""
from __future__ import annotations

import re
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amazon_tracker.utils import is_block, parse_price, retry_after
from amazon_tracker.diff import diff
from amazon_tracker.parser import price_from, parse_listing
from bs4 import BeautifulSoup


class TestParsePrice(unittest.TestCase):
    def test_usd_dot(self):
        self.assertEqual(parse_price("$11.99"), 11.99)

    def test_eu_comma(self):
        self.assertEqual(parse_price("7,32 €"), 7.32)

    def test_eu_comma_with_k(self):
        self.assertEqual(parse_price("1.234,56 €"), 1234.56)

    def test_usd_with_k(self):
        self.assertEqual(parse_price("$1,234.56"), 1234.56)

    def test_none_and_empty(self):
        self.assertIsNone(parse_price(None))
        self.assertIsNone(parse_price(""))

    def test_garbage(self):
        self.assertIsNone(parse_price("free!"))


class TestRetryAfter(unittest.TestCase):
    def test_header_used(self):
        self.assertEqual(retry_after(429, {"Retry-After": "45"}, 0, (30, 90)), 45)

    def test_header_capped(self):
        self.assertEqual(retry_after(503, {"Retry-After": "9999"}, 0, (30, 90)), 300)

    def test_fallback(self):
        self.assertEqual(retry_after(429, None, 0, (30, 90)), 30)
        self.assertEqual(retry_after(429, {}, 2, (30, 90)), 90)


class TestIsBlock(unittest.TestCase):
    def test_marker_small_body(self):
        body = b"api-services-support@amazon.com"
        self.assertTrue(is_block(200, body, ("api-services-support@amazon.com",), 20000))

    def test_marker_but_large_body(self):
        body = b"api-services-support@amazon.com" + b"x" * 50000
        self.assertFalse(is_block(200, body, ("api-services-support@amazon.com",), 20000))

    def test_no_marker(self):
        body = b"OK" + b"x" * 1000
        self.assertFalse(is_block(200, body, ("block",), 20000))


class TestDiff(unittest.TestCase):
    def test_first_run_none(self):
        self.assertIsNone(diff(None, []))

    def test_new_asin(self):
        prev = {"A": {"rank": 5, "price": 10.0, "title": "A"}}
        rows = [{"asin": "B", "rank": 1, "price": 20.0},
                {"asin": "A", "rank": 5, "price": 10.0}]
        d = diff(prev, rows)
        self.assertEqual(len(d["new"]), 1)
        self.assertEqual(d["new"][0]["asin"], "B")

    def test_rise_and_fall(self):
        prev = {"A": {"rank": 10, "price": 1.0, "title": "A"},
                "B": {"rank": 2, "price": 1.0, "title": "B"}}
        rows = [{"asin": "A", "rank": 5, "price": 1.0},   # 上升 5 名
                {"asin": "B", "rank": 8, "price": 1.0}]   # 下降 6 名
        d = diff(prev, rows)
        self.assertEqual(len(d["up"]), 1)
        self.assertEqual(len(d["down"]), 1)

    def test_price_change(self):
        prev = {"A": {"rank": 1, "price": 100.0, "title": "A"}}
        rows = [{"asin": "A", "rank": 1, "price": 110.0}]  # 涨 10%
        d = diff(prev, rows)
        self.assertEqual(len(d["price"]), 1)

    def test_out(self):
        prev = {"A": {"rank": 1, "price": 1.0, "title": "A"},
                "B": {"rank": 2, "price": 1.0, "title": "B"}}
        rows = [{"asin": "A", "rank": 1, "price": 1.0}]
        d = diff(prev, rows)
        self.assertEqual(len(d["out"]), 1)
        self.assertEqual(d["out"][0][0], "B")

    def test_price_below_threshold(self):
        prev = {"A": {"rank": 1, "price": 100.0, "title": "A"}}
        rows = [{"asin": "A", "rank": 1, "price": 102.0}]  # 2% 涨
        d = diff(prev, rows)
        self.assertEqual(len(d["price"]), 0)


AMAZON_ITEM = """
<div id="gridItemRoot" class="a-column a-span12 a-span-last" data-asin="B0TEST1234">
<div class="a-zgrid a-zg-grid">
  <a class="a-link-normal" href="/dp/B0TEST1234">
    <img alt="Bluetooth Headphones X" src="img.jpg"/>
  </a>
  <span class="a-size-base a-color-price"><span class="a-price">
    <span class="a-price-whole">17</span><span class="a-price-fraction">87</span>
  </span></span>
  <span class="a-icon-alt">4.6 out of 5 stars</span>
  <a href="/dp/B0TEST1234#customerReviews"><span class="a-size-small">20050</span></a>
</div>
</div>
"""

AMAZON_PAGE = """
<html><body>
<div id="gridItemRoot" class="a-column a-span12 a-span-last" data-asin="B0TEST1234">
  <span class="zg-bdg-text">#1</span>
  <div>
    <a class="a-link-normal" href="/dp/B0TEST1234">
      <img alt="Bluetooth Headphones X" src="img.jpg"/>
    </a>
    <span class="a-price">
      <span class="a-price-whole">17</span><span class="a-price-fraction">87</span>
    </span>
    <span class="a-icon-alt">4.6 out of 5 stars</span>
    <a href="/dp/B0TEST1234#customerReviews"><span class="a-size-small">20050</span></a>
  </div>
</div>
</body></html>
"""


class TestPriceFrom(unittest.TestCase):
    def test_joins_whole_and_fraction(self):
        soup = BeautifulSoup(AMAZON_ITEM, "html.parser")
        node = soup.find(id="gridItemRoot")
        self.assertEqual(price_from(node), 17.87)


class TestParseListing(unittest.TestCase):
    def setUp(self):
        from amazon_tracker.config import Market
        self.market = Market("com", "www.amazon.com", "USD", "en-US,en;q=0.9")

    def test_extracts_one_item(self):
        rows = parse_listing(AMAZON_PAGE, self.market, top=10)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["asin"], "B0TEST1234")
        self.assertEqual(r["rank"], 1)
        self.assertEqual(r["title"], "Bluetooth Headphones X")
        self.assertEqual(r["price"], 17.87)
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["rating"], 4.6)
        self.assertEqual(r["reviews"], 20050)


if __name__ == "__main__":
    unittest.main(verbosity=2)
