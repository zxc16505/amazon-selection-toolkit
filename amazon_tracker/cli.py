# -*- coding: utf-8 -*-
"""命令行入口。"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

from .config import (BASE, COOKIE_FILE, CACHE_FILE, DATA, DB, PATH_OF, Config)
from .diff import diff, report
from .fetch import RiskControl, fetch
from .parser import parse_listing
from .storage import (already_done, ensure_schema, log_request, previous, save)
from .utils import dump_json, load_json


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="跨境榜单追踪：采集 + 落库 + 与上次快照对比")
    ap.add_argument("--markets", nargs="+", default=["com"],
                    help="站点代码，可选 " + " ".join(Config.load().markets.keys()))
    ap.add_argument("--category", default="electronics")
    ap.add_argument("--list", dest="list_type", default="bestsellers", choices=list(PATH_OF))
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--force", action="store_true", help="当天已采过仍重新请求（不建议）")
    ap.add_argument("--config", default=None, help="自定义 config.yaml 路径")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load() if args.config is None else Config.load(BASE / args.config)

    DATA.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = sqlite3.connect(DB)
    ensure_schema(con)

    def log(market, list_type, category, status, http, ms, note):
        log_request(con, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    market, list_type, category, status, http, ms, note)
        con.commit()

    cache = load_json(CACHE_FILE, {})
    session = requests.Session()
    session.cookies.update(load_json(COOKIE_FILE, {}))

    failed, skipped = {}, []
    for i, code in enumerate(args.markets):
        market = cfg.markets.get(code)
        if market is None:
            failed[code] = f"未知站点（可用：{' '.join(cfg.markets)}）"
            continue
        if i:
            time.sleep(random.uniform(5, 10))

        list_type_path = PATH_OF[args.list_type]
        if not args.force and already_done(con, run_date, code, args.list_type, args.category):
            skipped.append(code)
            continue

        try:
            html, note = fetch(session, market, list_type_path, args.category, cache, cfg)
            prev = previous(con, code, args.list_type, args.category, run_date)
            if html is None:
                skipped.append(code)
                log(code, args.list_type, args.category, "304", 304, 0, note)
                print(f"\nAmazon {code} / {args.category}：{note}，沿用当天已入库数据")
                continue
            rows = parse_listing(html, market, args.top)
            if not rows:
                failed[code] = "解析到 0 条（页面结构可能变化）"
                log(code, args.list_type, args.category, "ZERO", 200, 0, "parse empty")
                continue
            d = diff(prev, rows)
            save(con, run_date, code, args.list_type, args.category, rows)
            con.commit()
            report(code, args.list_type, args.category, run_date, rows, d, f" {note}")
            log(code, args.list_type, args.category, "OK", 200, 0, note)
        except RiskControl as e:
            con.commit()
            dump_json(COOKIE_FILE, requests.utils.dict_from_cookiejar(session.cookies))
            dump_json(CACHE_FILE, cache)
            print(f"\n[停止] {e}\n已中断本轮全部后续请求。同一 IP 短期内不要再跑，"
                  f"也不要换 IP 接着跑。", file=sys.stderr)
            return 2
        except Exception as e:
            failed[code] = str(e)
            log(code, args.list_type, args.category, "ERR", None, 0, str(e))

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
