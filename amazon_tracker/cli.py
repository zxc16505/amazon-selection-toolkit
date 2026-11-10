# -*- coding: utf-8 -*-
"""命令行入口。

两个子命令：
  crawl     采集榜单 → 落库 → 与上次快照对比（原行为）
  material  采集竞品详情页 → 生成上架素材模板与合规预检（只读）

向后兼容：不带子命令时等价于 crawl，`python tracker.py --markets com` 仍可用。
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import (BASE, COOKIE_FILE, CACHE_FILE, DATA, DB, PATH_OF, Config)
from .detail import parse_detail
from .diff import diff, report
from .fetch import RiskControl, fetch, fetch_detail
from .material import build_material, render_markdown
from .parser import parse_listing
from .storage import (already_done, ensure_schema, log_request, previous, save)
from .utils import dump_json, load_json


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="跨境选品数据工具：榜单追踪 + 上架素材生成")
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("crawl", help="采集榜单并对比上次快照")
    c.add_argument("--markets", nargs="+", default=["com"],
                   help="站点代码，可选 " + " ".join(Config.load().markets.keys()))
    c.add_argument("--category", default="electronics")
    c.add_argument("--list", dest="list_type", default="bestsellers", choices=list(PATH_OF))
    c.add_argument("--top", type=int, default=30)
    c.add_argument("--force", action="store_true", help="当天已采过仍重新请求（不建议）")
    c.add_argument("--config", default=None)

    m = sub.add_parser("material", help="采集竞品详情页，生成上架素材模板（只读，不上架）")
    m.add_argument("--asin", required=True, help="目标商品 ASIN")
    m.add_argument("--competitors", nargs="*", default=[], help="竞品 ASIN，空格分隔")
    m.add_argument("--market", default="com")
    m.add_argument("--category", default="electronics")
    m.add_argument("--from-html", nargs="+", default=None,
                   help="用本地 HTML 代替网络请求（离线演示/回归用）：第一个是目标品，"
                        "其余当作竞品")
    m.add_argument("--out", default=None, help="输出目录，默认 data/material")
    m.add_argument("--config", default=None)
    return ap


def _open_env(cfg: Config):
    DATA.mkdir(parents=True, exist_ok=True)
    cache = load_json(CACHE_FILE, {})
    session = requests.Session()
    session.cookies.update(load_json(COOKIE_FILE, {}))
    return session, cache


def _persist(session, cache):
    dump_json(COOKIE_FILE, requests.utils.dict_from_cookiejar(session.cookies))
    dump_json(CACHE_FILE, cache)


def cmd_crawl(args) -> int:
    cfg = Config.load() if args.config is None else Config.load(BASE / args.config)
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con = sqlite3.connect(DB)
    ensure_schema(con)

    def log(market, list_type, category, status, http, ms, note):
        log_request(con, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    market, list_type, category, status, http, ms, note)
        con.commit()

    session, cache = _open_env(cfg)
    failed, skipped = {}, []
    for i, code in enumerate(args.markets):
        market = cfg.markets.get(code)
        if market is None:
            failed[code] = f"未知站点（可用：{' '.join(cfg.markets)}）"
            continue
        if i:
            time.sleep(random.uniform(5, 10))

        if not args.force and already_done(con, run_date, code, args.list_type, args.category):
            skipped.append(code)
            continue

        try:
            html, note = fetch(session, market, PATH_OF[args.list_type], args.category, cache, cfg)
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
            _persist(session, cache)
            print(f"\n[停止] {e}\n已中断本轮全部后续请求。同一 IP 短期内不要再跑，"
                  f"也不要换 IP 接着跑。", file=sys.stderr)
            return 2
        except Exception as e:
            failed[code] = str(e)
            log(code, args.list_type, args.category, "ERR", None, 0, str(e))

    _persist(session, cache)
    con.close()

    if skipped:
        print(f"\n当天已采过，跳过（--force 可强制重跑）: {' '.join(skipped)}")
    if failed:
        print("\n未完成站点（不影响其他站点入库）:")
        for m, err in failed.items():
            print(f"  {m:7s} {err}")
    print(f"\n库文件: {DB}")
    return 1 if failed else 0


def cmd_material(args) -> int:
    cfg = Config.load() if args.config is None else Config.load(BASE / args.config)
    market = cfg.markets.get(args.market)
    if market is None:
        print(f"未知站点 {args.market}，可用：{' '.join(cfg.markets)}", file=sys.stderr)
        return 1
    if not args.from_html and len(args.competitors) > cfg.material.get("max_competitors", 5):
        print("竞品数量超过配置上限，请减少后重试", file=sys.stderr)
        return 1

    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = Path(args.out) if args.out else DATA / "material"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.from_html:
        files = args.from_html
        target = parse_detail(Path(files[0]).read_text(encoding="utf-8", errors="replace"), market)
        target["asin"] = target.get("asin") or args.asin
        competitors = []
        for f in files[1:]:
            c = parse_detail(Path(f).read_text(encoding="utf-8", errors="replace"), market)
            c["asin"] = c.get("asin") or Path(f).stem
            competitors.append(c)
        print(f"离线模式：目标 {files[0]}，竞品 {len(competitors)} 个")
    else:
        session, cache = _open_env(cfg)
        con = sqlite3.connect(DB)
        ensure_schema(con)

        def log(status, note):
            log_request(con, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        args.market, "detail", args.category, status, 200, 0, note)
            con.commit()

        plans = [(args.asin, "target")] + [(a, "competitor") for a in args.competitors]
        parsed: dict[str, dict] = {}
        try:
            for i, (asin, role) in enumerate(plans):
                if i:
                    time.sleep(random.uniform(5, 10))
                try:
                    html, note = fetch_detail(session, market, asin, cache, cfg)
                    if html is None:
                        print(f"  {asin} 未变更（304），跳过")
                        continue
                    parsed[asin] = parse_detail(html, market)
                    parsed[asin]["asin"] = asin
                    log("OK", note)
                    print(f"  {asin} 解析完成（{role}）")
                except RiskControl:
                    raise
                except Exception as e:
                    log("ERR", str(e))
                    print(f"  {asin} 失败：{e}")
        except RiskControl as e:
            con.commit()
            _persist(session, cache)
            print(f"\n[停止] {e}\n已中断本轮全部后续请求。", file=sys.stderr)
            con.close()
            return 2
        _persist(session, cache)
        con.close()

        if args.asin not in parsed:
            print("目标商品未取到数据，无法生成素材", file=sys.stderr)
            return 1
        target = parsed[args.asin]
        competitors = [v for k, v in parsed.items() if k != args.asin]

    material = build_material(target, competitors, args.category, cfg.material)
    md = render_markdown(material, args.market, run_date)
    stem = f"{args.asin}-{run_date}"
    (out_dir / f"{stem}.md").write_text(md, encoding="utf-8")
    (out_dir / f"{stem}.json").write_text(
        json.dumps(material, ensure_ascii=False, indent=2), encoding="utf-8")

    errors = [i for i in material["issues"] if i["level"] == "error"]
    print(f"\n素材已生成：{out_dir / (stem + '.md')}")
    print(f"竞品样本 {material['competitor_count']} 个，"
          f"关键词 {len(material['keywords'])} 个，"
          f"阻断项 {len(errors)} 个")
    for i in errors:
        print(f"  [阻断] {i['message']}")
    print("\n本命令只读公开商品页，不包含任何上架/提交动作。")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("crawl", "material"):
        argv = ["crawl"] + argv
    args = build_parser().parse_args(argv)
    return cmd_crawl(args) if args.cmd == "crawl" else cmd_material(args)


if __name__ == "__main__":
    raise SystemExit(main())
