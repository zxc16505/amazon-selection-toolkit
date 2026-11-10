# -*- coding: utf-8 -*-
"""上架素材生成与合规校验。纯函数，不发请求、不写平台。"""
from __future__ import annotations

import re
import statistics
from collections import Counter


# 英文停用词 + 电商 listing 里的噪声词
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "before", "being", "between",
    "both", "but", "by", "can", "could", "did", "do", "does", "each", "few", "for",
    "from", "had", "has", "have", "how", "if", "in", "into", "is", "it", "its", "may",
    "more", "most", "not", "of", "on", "only", "or", "other", "our", "out", "over",
    "same", "should", "so", "some", "such", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "those", "through", "to", "under", "up",
    "very", "was", "were", "what", "when", "where", "which", "while", "who", "why",
    "will", "with", "would", "you", "your", "yours",
    # 电商噪声
    "new", "pack", "pcs", "piece", "pieces", "set", "inch", "inches", "cm", "mm",
    "color", "colors", "amazon", "product", "item", "items", "use", "used", "using",
    "easy", "high", "quality", "also", "made", "make", "perfect", "great", "good",
    "best", "designed", "design", "include", "includes", "including", "one", "two",
    "three", "four", "five", "other", "all", "any", "about", "after", "just", "like",
}

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-.]+")


def _tokens(text: str) -> list[str]:
    """保留数字与型号（如 5.3 / 40h）参与相邻判断，避免过滤后产生假相邻短语。"""
    return [t.lower().strip("'-.") for t in TOKEN_RE.findall(text or "")]


def _is_word(tok: str) -> bool:
    return len(tok) >= 3 and tok[0].isalpha() and tok not in STOPWORDS


def extract_keywords(texts: list[str], top_n: int = 15) -> list[dict]:
    """从竞品标题 + 五点里提高频词与二元短语。返回 [{phrase, count, kind}]。"""
    uni: Counter = Counter()
    bi: Counter = Counter()
    for t in texts:
        toks = _tokens(t)
        for w in toks:
            if _is_word(w):
                uni[w] += 1
        for a, b in zip(toks, toks[1:]):
            if _is_word(a) and _is_word(b):
                bi[f"{a} {b}"] += 1

    out = ([{"phrase": p, "count": c, "kind": "bigram"} for p, c in bi.items() if c >= 2]
           + [{"phrase": p, "count": c, "kind": "unigram"} for p, c in uni.items() if c >= 2])
    out.sort(key=lambda x: (-x["count"], x["kind"] != "bigram", x["phrase"]))
    return out[:top_n]


def price_band(prices: list[float]) -> dict | None:
    """竞品价格带：最低 / 中位 / 最高。少于 2 个价格返回 None。"""
    vals = sorted(p for p in prices if p)
    if len(vals) < 2:
        return None
    return {
        "min": vals[0],
        "median": round(statistics.median(vals), 2),
        "max": vals[-1],
        "count": len(vals),
    }


def check_listing(title: str, bullets: list[str], price: float | None,
                  category: str, rules: dict) -> list[dict]:
    """按配置里的规则检查一份 listing 草案。返回问题列表，level: error/warn。"""
    issues: list[dict] = []
    title_max = rules.get("title_max_chars", 200)
    bullet_max = rules.get("bullet_max_chars", 500)

    if not title:
        issues.append({"level": "error", "code": "TITLE_EMPTY", "message": "标题为空"})
    elif len(title) > title_max:
        issues.append({"level": "error", "code": "TITLE_TOO_LONG",
                       "message": f"标题 {len(title)} 字符，超过 {title_max}"})

    if len(bullets) < 3:
        issues.append({"level": "warn", "code": "BULLETS_FEW",
                       "message": f"五点描述只有 {len(bullets)} 条，建议 3-5 条"})
    for i, b in enumerate(bullets, 1):
        if len(b) > bullet_max:
            issues.append({"level": "error", "code": "BULLET_TOO_LONG",
                           "message": f"第 {i} 条五点 {len(b)} 字符，超过 {bullet_max}"})

    if price is None:
        issues.append({"level": "warn", "code": "NO_PRICE", "message": "没有解析到价格"})

    haystack = (title + " " + " ".join(bullets)).lower()
    for phrase in rules.get("banned_phrases", []):
        if phrase.lower() in haystack:
            issues.append({"level": "error", "code": "BANNED_PHRASE",
                           "message": f"命中平台禁用的绝对化/促销表述：{phrase}"})

    for rule in rules.get("category_rules", {}).get(category, []):
        m = re.search(rule["pattern"], haystack, re.I)
        if m:
            issues.append({"level": "error", "code": "CATEGORY_CLAIM",
                           "message": f"{rule['message']}（命中：{m.group(0)}）"})
    return issues


def build_material(target: dict, competitors: list[dict], category: str,
                   rules: dict) -> dict:
    """汇总成一份上架素材：目标品 + 竞品价格带 + 关键词 + 合规检查。"""
    prices = [c["price"] for c in competitors if c.get("price")]
    texts = [c["title"] for c in competitors if c.get("title")]
    for c in competitors:
        texts.extend(c.get("bullets") or [])

    band = price_band(prices)
    keywords = extract_keywords(texts)
    issues = check_listing(target.get("title", ""), target.get("bullets") or [],
                           target.get("price"), category, rules)

    return {
        "target": target,
        "category": category,
        "competitor_count": len(competitors),
        "price_band": band,
        "keywords": keywords,
        "issues": issues,
        "suggestions": _suggest(target, band, keywords),
    }


def _suggest(target: dict, band: dict | None, keywords: list[dict]) -> list[str]:
    out = []
    if band and target.get("price"):
        p = target["price"]
        if p < band["min"]:
            out.append(f"定价 {p} 低于竞品价格带下沿 {band['min']}，注意毛利与定位")
        elif p > band["max"]:
            out.append(f"定价 {p} 高于竞品价格带上沿 {band['max']}，需要卖点支撑")
        else:
            out.append(f"定价 {p} 落在竞品价格带 {band['min']}–{band['max']} 内")
    miss = [k["phrase"] for k in keywords if k["kind"] == "bigram"][:5]
    if miss:
        out.append("竞品高频短语（建议自查标题/五点是否覆盖）：" + "、".join(miss))
    if not target.get("bullets"):
        out.append("目标品没有解析到五点描述，需人工补全后再上架")
    return out


def render_markdown(material: dict, market_code: str, run_date: str) -> str:
    """渲染成给运营看的上架素材模板（Markdown）。章节号按实际内容动态编号。"""
    t = material["target"]
    band = material["price_band"]
    cn = "一二三四五六七八九十"
    sections: list[tuple[str, list[str]]] = []

    sections.append(("目标商品", [
        "| 字段 | 值 |",
        "| --- | --- |",
        f"| 标题 | {t.get('title') or '—'} |",
        f"| 品牌 | {t.get('brand') or '—'} |",
        f"| 价格 | {t.get('price') or '—'} {t.get('currency') or ''} |",
        f"| 评分 | {t.get('rating') or '—'}（{t.get('reviews') or 0} 条评论）|",
        f"| 类目路径 | {t.get('category_path') or '—'} |",
    ]))

    if band:
        sections.append(("竞品价格带", [
            "| 最低 | 中位 | 最高 | 样本数 |",
            "| --- | --- | --- | --- |",
            f"| {band['min']} | {band['median']} | {band['max']} | {band['count']} |",
        ]))

    if material["keywords"]:
        body = ["| 词/短语 | 出现次数 | 类型 |", "| --- | --- | --- |"]
        for k in material["keywords"]:
            body.append(f"| {k['phrase']} | {k['count']} | "
                        f"{'短语' if k['kind'] == 'bigram' else '单词'} |")
        sections.append(("竞品高频词", body))

    errors = [i for i in material["issues"] if i["level"] == "error"]
    warns = [i for i in material["issues"] if i["level"] == "warn"]
    check_body: list[str] = []
    if not errors and not warns:
        check_body.append("- 未发现规则命中项（不等于平台审核必过）")
    check_body += [f"- [阻断] `{i['code']}` {i['message']}" for i in errors]
    check_body += [f"- [提醒] `{i['code']}` {i['message']}" for i in warns]
    sections.append(("合规预检", check_body))

    if material["suggestions"]:
        sections.append(("建议", [f"- {s}" for s in material["suggestions"]]))

    lines = [
        f"# 上架素材 · {t.get('asin') or '未知 ASIN'}",
        "",
        f"- 站点：{market_code}　类目：{material['category']}　生成日期：{run_date}",
        f"- 竞品样本：{material['competitor_count']} 个",
        "- 本文件只做素材准备与合规预检，**不含任何上架/提交动作**",
        "",
    ]
    for idx, (title, body) in enumerate(sections):
        lines += [f"## {cn[idx]}、{title}", ""] + body + [""]

    lines += ["---", "",
              "> 素材由脚本从公开商品页生成，价格为采集当时快照；"
              "上架动作与最终文案由运营在后台人工确认。"]
    return "\n".join(lines)
