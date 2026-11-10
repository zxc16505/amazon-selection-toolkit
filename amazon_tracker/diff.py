# -*- coding: utf-8 -*-
"""与上一次快照的变化对比。纯函数。"""
from __future__ import annotations


DiffResult = dict[str, list]


def diff(prev: dict[str, dict] | None, rows: list[dict], rank_threshold: int = 3,
         price_threshold: float = 0.03) -> DiffResult | None:
    """对比两次快照。

    prev: {asin: {"rank": int, "price": float|None, "title": str}}
    rows: [{asin, rank, price, currency, ...}]
    返回 None 表示首次运行（没有可对比的基线）。
    rank_threshold: 排名变化多少算显著（正数表示新进榜，负数表示掉出）
    price_threshold: 价格相对变化多少算显著（3% 即 0.03）
    """
    if prev is None:
        return None
    cur = {r["asin"]: r for r in rows}
    out: DiffResult = {"new": [], "up": [], "down": [], "out": [], "price": []}
    for asin, r in cur.items():
        if asin not in prev:
            out["new"].append(r)
            continue
        d = prev[asin]["rank"] - r["rank"]            # 正数 = 排名上升
        if d >= rank_threshold:
            out["up"].append((r, prev[asin]["rank"]))
        elif d <= -rank_threshold:
            out["down"].append((r, prev[asin]["rank"]))
        p0 = prev[asin]["price"]
        if p0 and r["price"] and abs(r["price"] - p0) / p0 >= price_threshold:
            out["price"].append((r, p0))
    for asin, p in prev.items():
        if asin not in cur:
            out["out"].append((asin, p["title"], p["rank"]))
    return out


def report(market_code: str, list_type: str, category: str, run_date: str,
           rows: list[dict], d: DiffResult | None, note: str = "") -> None:
    print(f"\nAmazon {market_code} / {list_type} / {category} @ {run_date}"
          f"（{len(rows)} 条）{note}")
    if d is None:
        for r in rows[:5]:
            print(f"  #{r['rank']:>2}  {(r['title'] or '')[:52]:<52} {r['price'] or '--'} {r['currency']}")
        print("  （首次运行，下次再跑即可看到排名/价格变化）")
        return
    for key, label in [("new", "新进榜"), ("up", f"上升 {rank_threshold} 名以上"),
                       ("down", f"下降 {rank_threshold} 名以上")] \
            if False else [("new", "新进榜"), ("up", "上升 3 名以上"), ("down", "下降 3 名以上")]:
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
