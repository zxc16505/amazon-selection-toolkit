# -*- coding: utf-8 -*-
"""跨境榜单追踪（Amazon Best Sellers / New Releases）

按站点采集榜单（排名、ASIN、标题、价格、评分、评论数），落 SQLite，
每次运行与上一次快照做 diff：新进榜 / 上升 / 下降 / 掉出 / 价格变动。

针对「连续访问被拦截」做了四件事（都是降低请求量与保持会话一致性，
不涉及代理池、验证码绕过、指纹伪装）：
  1. 当天已采过就跳过：同一站点同一天最多请求一次（--force 才重跑）
  2. 会话复用：cookie 落盘，下次仍带同一 session-id，不每次都像新客户端
  3. 条件请求：带 If-None-Match / If-Modified-Since，页面没变直接 304，不下载
  4. 退避：429/503 读 Retry-After 再退，最多 3 次；命中拦截特征则整轮立即停止

其它实测约束：
  - 币种随出口 IP 地域漂移：不显式指定 i18n-prefs 时，国内出口访问
    amazon.com 返回 CNY 报价。这里按站点显式钉住币种。
  - 价格小数点在 a-offscreen 里被吞掉（"$1787" 实为 $17.87），
    必须由 a-price-whole + a-price-fraction 拼回。
"""
import argparse
import json
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DB = DATA / "ranks.sqlite3"
COOKIE_FILE = DATA / "cookies.json"
CACHE_FILE = DATA / "http_cache.json"

# 站点 -> (域名, 本币, Accept-Language)
MARKETS = {
    "com":    ("www.amazon.com",    "USD", "en-US,en;q=0.9"),
    "co.uk":  ("www.amazon.co.uk",  "GBP", "en-GB,en;q=0.9"),
    "de":     ("www.amazon.de",     "EUR", "de-DE,de;q=0.9"),
    "co.jp":  ("www.amazon.co.jp",  "JPY", "ja-JP,ja;q=0.9"),
    "ca":     ("www.amazon.ca",     "CAD", "en-CA,en;q=0.9"),
    "com.au": ("www.amazon.com.au", "AUD", "en-AU,en;q=0.9"),
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 拦截页只有几 KB，正常榜单页约 450KB；用「页面小 + 命中特征」两个条件一起判，避免误伤
BLOCK_MARKERS = ("api-services-support@amazon.com", "Robot Check",
                 "automated access to Amazon data", "Enter the characters you see below")
BLOCK_MAX_LEN = 20000
MAX_ATTEMPTS = 3
BACKOFF = (30, 90)

PATH_OF = {"bestsellers": "/gp/bestsellers/{}", "new-releases": "/gp/new-releases/{}"}


class RiskControl(Exception):
    """页面出现拦截特征，必须停止全部后续请求。"""


# ---------- 小工具 ----------
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def dump_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_price(raw: str):
    """把 '7,32 €' / '$11.99' / '1.234,56' 解析成 float。"""
    if not raw:
        return None
    s = re.sub(r"[^\d.,]", "", raw)
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        tail = s.split(",")[-1]
        s = s.replace(",", ".") if len(tail) == 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def price_from(node):
    """取现价。

    a-offscreen 里的文本是 "$1787"（小数点在部分币种下被吞掉），不能直接用；
    真实价格由 a-price-whole + a-price-fraction 两段拼成。
    a-text-price 是划线原价，必须排除。
    """
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


def is_block(resp) -> bool:
    if len(resp.content) > BLOCK_MAX_LEN:
        return False
    return any(m in resp.text for m in BLOCK_MARKERS)


def retry_after(resp, idx) -> float:
    v = resp.headers.get("Retry-After")
    if v and v.strip().isdigit():
        return min(int(v), 300)
    return BACKOFF[min(idx, len(BACKOFF) - 1)]


# ---------- 采集 ----------
def fetch(session, market, list_type, category, cache, log):
    """返回 (html 或 None 表示 304, 说明)。"""
    host, currency, lang = MARKETS[market]
    url = "https://" + host + PATH_OF[list_type].format(category)
    headers = {
        "User-Agent": UA,
        "Accept-Language": lang,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://" + host + "/",
    }
    cached = cache.get(url) or {}
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]

    last = "unknown"
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            wait = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            time.sleep(wait)
        t0 = time.time()
        try:
            r = session.get(url, timeout=30, headers=headers,
                            cookies={"i18n-prefs": currency,
                                     "lc-main": lang.split(",")[0].replace("-", "_")})
        except requests.RequestException as e:
            last = type(e).__name__
            log(market, list_type, category, "ERR", None, time.time() - t0, last)
            continue

        ms = round((time.time() - t0) * 1000)
        if r.status_code == 304:
            log(market, list_type, category, "304", 304, ms, "未修改，不下载")
            return None, "304 Not Modified"
        if is_block(r):
            log(market, list_type, category, "BLOCK", r.status_code, ms,
                f"{len(r.content)}B 拦截页")
            raise RiskControl(f"{market} 返回拦截页（HTTP {r.status_code}，{len(r.content)} 字节）")
        if r.status_code in (429, 503):
            wait = retry_after(r, attempt)
            last = f"HTTP {r.status_code}"
            log(market, list_type, category, "RETRY", r.status_code, ms, f"退避 {wait}s")
            if attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep(wait)
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}"
            log(market, list_type, category, "ERR", r.status_code, ms, last)
            break

        log(market, list_type, category, "OK", 200, ms, f"{len(r.content)}B")
        cache[url] = {"etag": r.headers.get("ETag"),
                      "last_modified": r.headers.get("Last-Modified")}
        return r.text, "200 OK"
    raise RuntimeError(last)


def parse(html: str, market: str, top: int):
    soup = BeautifulSoup(html, "html.parser")
    _, currency, _ = MARKETS[market]
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
            "currency": currency,
            "rating": float(m.group(1)) if m else None,
            "reviews": int(re.sub(r"\D", "", rev_el.get_text()) or 0) if rev_el else 0,
        })
    return rows


# ---------- 存储 ----------
def ensure_db(con):
    con.execute("""CREATE TABLE IF NOT EXISTS ranks (
        run_date   TEXT NOT NULL,
        market     TEXT NOT NULL,
        list_type  TEXT NOT NULL,
        category   TEXT NOT NULL,
        rank       INTEGER NOT NULL,
        asin       TEXT NOT NULL,
        title      TEXT,
        price      REAL,
        currency   TEXT,
        rating     REAL,
        reviews    INTEGER,
        PRIMARY KEY (run_date, market, list_type, category, asin))""")
    con.execute("""CREATE TABLE IF NOT EXISTS request_log (
        ts         TEXT NOT NULL,
        market     TEXT, list_type TEXT, category TEXT,
        status     TEXT, http INTEGER, ms INTEGER, note TEXT)""")


def already_done(con, run_date, market, list_type, category) -> bool:
    row = con.execute(
        "SELECT 1 FROM ranks WHERE run_date=? AND market=? AND list_type=? AND category=? LIMIT 1",
        (run_date, market, list_type, category)).fetchone()
    return row is not None


def save(con, run_date, market, list_type, category, rows):
    con.executemany(
        "INSERT OR REPLACE INTO ranks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(run_date, market, list_type, category, r["rank"], r["asin"], r["title"],
          r["price"], r["currency"], r["rating"], r["reviews"]) for r in rows])


def previous(con, market, list_type, category, run_date):
    prev = con.execute(
        "SELECT MAX(run_date) FROM ranks WHERE market=? AND list_type=? AND category=? AND run_date<?",
        (market, list_type, category, run_date)).fetchone()[0]
    if not prev:
        return None
    return {r[0]: {"rank": r[1], "price": r[2], "title": r[3]}
            for r in con.execute(
                "SELECT asin, rank, price, title FROM ranks WHERE run_date=? AND market=? "
                "AND list_type=? AND category=?", (prev, market, list_type, category))}


def diff(prev, rows):
    if prev is None:
        return None
    cur = {r["asin"]: r for r in rows}
    out = {"new": [], "up": [], "down": [], "out": [], "price": []}
    for asin, r in cur.items():
        if asin not in prev:
            out["new"].append(r)
            continue
        d = prev[asin]["rank"] - r["rank"]
        if d >= 3:
            out["up"].append((r, prev[asin]["rank"]))
        elif d <= -3:
            out["down"].append((r, prev[asin]["rank"]))
        p0 = prev[asin]["price"]
        if p0 and r["price"] and abs(r["price"] - p0) / p0 >= 0.03:
            out["price"].append((r, p0))
    for asin, p in prev.items():
        if asin not in cur:
            out["out"].append((asin, p["title"], p["rank"]))
    return out


def report(market, list_type, category, run_date, rows, d, note=""):
    print(f"\nAmazon {market} / {list_type} / {category} @ {run_date}"
          f"（{len(rows)} 条）{note}")
    if d is None:
        for r in rows[:5]:
            print(f"  #{r['rank']:>2}  {(r['title'] or '')[:52]:<52} {r['price'] or '--'} {r['currency']}")
        print("  （首次运行，下次再跑即可看到排名/价格变化）")
        return
    for key, label in [("new", "新进榜"), ("up", "上升 3 名以上"), ("down", "下降 3 名以上")]:
        if d[key]:
            print(f"  {label}:")
            for item in d[key]:
                if key == "new":
                    print(f"    #{item['rank']:>2}  {item['title'][:48]}")
                else:
                    r, old = item
                    print(f"    #{r['rank']:>2}（原 #{old}）  {r['title'][:44]}")
    if d["price"]:
        print("  价格变动 ≥3%:")
        for r, old in d["price"]:
            print(f"    #{r['rank']:>2}  {old} → {r['price']} {r['currency']}  {r['title'][:36]}")
    if d["out"]:
        print(f"  掉出榜单: {len(d['out'])} 条")
        for asin, title, old in d["out"][:5]:
            print(f"    （原 #{old}） {asin}  {(title or '')[:40]}")
    if not any(d.values()):
        print("  与上次相比无变化")


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description="跨境榜单追踪：采集 + 落库 + 与上次快照对比")
    ap.add_argument("--markets", nargs="+", default=["com"],
                    help="站点代码，可选 " + " ".join(MARKETS))
    ap.add_argument("--category", default="electronics")
    ap.add_argument("--list", dest="list_type", default="bestsellers", choices=list(PATH_OF))
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--force", action="store_true", help="当天已采过仍重新请求（不建议）")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = sqlite3.connect(DB)
    ensure_db(con)

    def log(market, list_type, category, status, http, ms, note):
        con.execute("INSERT INTO request_log VALUES (?,?,?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     market, list_type, category, status, http, int(ms * 1000) if ms < 100 else int(ms), note))
        con.commit()

    cache = load_json(CACHE_FILE, {})
    session = requests.Session()
    session.cookies.update(load_json(COOKIE_FILE, {}))   # 复用上次会话，不每次都是新客户端

    failed, skipped = {}, []
    for i, market in enumerate(args.markets):
        if i:
            time.sleep(random.uniform(5, 10))            # 站点之间随机间隔，不用固定值
        if not args.force and already_done(con, run_date, market, args.list_type, args.category):
            skipped.append(market)
            continue
        try:
            html, note = fetch(session, market, args.list_type, args.category, cache, log)
            prev = previous(con, market, args.list_type, args.category, run_date)
            if html is None:
                skipped.append(market)
                print(f"\nAmazon {market} / {args.category}：{note}，沿用当天已入库数据")
                continue
            rows = parse(html, market, args.top)
            if not rows:
                failed[market] = "解析到 0 条（页面结构可能变化）"
                continue
            d = diff(prev, rows)
            save(con, run_date, market, args.list_type, args.category, rows)
            con.commit()
            report(market, args.list_type, args.category, run_date, rows, d, f" {note}")
        except RiskControl as e:
            con.commit()
            dump_json(COOKIE_FILE, requests.utils.dict_from_cookiejar(session.cookies))
            dump_json(CACHE_FILE, cache)
            print(f"\n[停止] {e}\n已中断本轮全部后续请求。同一 IP 短期内不要再跑，"
                  f"也不要换 IP 接着跑。", file=sys.stderr)
            return 2
        except Exception as e:
            failed[market] = str(e)

    dump_json(COOKIE_FILE, requests.utils.dict_from_cookiejar(session.cookies))
    dump_json(CACHE_FILE, cache)
    con.close()

    if skipped:
        print(f"\n当天已采过，跳过（--force 可强制重跑）: {' '.join(skipped)}")
    if failed:
        print("\n未完成站点（不影响其他站点入库）:")
        for m, err in failed.items():
            print(f"  {m:7s} {err}")
    print(f"\n库文件: {DB}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
