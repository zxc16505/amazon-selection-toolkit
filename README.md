# crossborder-tracker

做服饰电商出身，一直好奇亚马逊那边的榜单是怎么动的，就写了个脚本自己跑。功能说起来不复杂：每天把 bestsellers / new-releases 的排名、ASIN、标题、价格、评分、评论数采下来存 SQLite，跟上次快照 diff 一下——新进榜的、掉出去的、涨价降价的，一眼就能看到。后来公司那边要上品，顺手加了 `material` 子命令：抓目标品和几个竞品的详情页，算竞品价格带、提高频词，再按规则过一遍合规预检，出一份 Markdown 给运营看。全程只读，不碰卖家后台，没有任何上架动作。

单机脚本，一天跑一次，没有调度也没有代理池，这个量级用不上。

## 用法

```bash
pip install -r requirements.txt

# 榜单追踪
python tracker.py --markets com              # 默认类目 electronics、取前 30
python tracker.py --markets com co.uk de --top 10
python tracker.py --list new-releases        # 新品榜
python tracker.py --markets com --force      # 当天已采过仍重跑（不建议）

# 上架素材生成
python tracker.py material --asin B012345678 --market com --category electronics \
    --competitors B087654321 B0ABCDEF12
python tracker.py material --asin B012345678 --market com --category electronics \
    --from-html a.html b1.html b2.html       # 离线：从本地 HTML 解析，不发请求
```

站点代码：`com` `co.uk` `de` `co.jp` `ca` `com.au`。我这边的网络环境下 `co.jp` 一直连不上，其他站点正常。

`--from-html` 不发请求：第一个文件当目标品，其余当竞品。没网的时候、或者只想调解析和规则的时候用它，CI 也是这么测的。

跑一次的输出大概长这样：

```
Amazon com / bestsellers / electronics @ 2026-11-10（30 条） 200 OK
  # 1  blink plus plan with monthly auto-renewal            11.99 USD
  # 2  Apple EarPods Headphones with USB-C Plug, Wired Ear  17.87 USD
  # 3  Apple AirTag (2nd Generation): Tracker for Keychain, 19.13 USD
  （首次运行，下次再跑即可看到排名/价格变化）
```

## 被拦过一次之后，思路就定了：少发请求

跑到第三轮的时候，amazon.com 突然只返回一个 3.8KB 的页面（正常榜单页 450KB 上下），正文一句话："To discuss automated access to Amazon data please contact …"。翻了翻社区讨论，结论挺一致：连续访问、每次都是全新会话，是最容易被判成脚本的两个特征；有效的做法是少发请求，而不是想办法绕。

所以现在这套策略全围绕一个"少"字：同站点同榜单当天最多采一次，`--force` 才重跑；cookie 落盘到 `data/cookies.json`，下次沿用同一个 session-id；记 ETag / Last-Modified，页面没变直接 304，连正文都不下载；429/503 先读 `Retry-After`，没有就 30s、90s 递增，最多试 3 次；站点之间随机睡 5-10 秒，单站点失败只记原因，不阻塞其他站点。

判定拦截也留了余地：页面小于 20KB 且命中特征串，两个条件同时成立才算，免得误伤正常页。一旦命中，整轮请求全部停掉，退出码 2。代理池、打码平台、指纹伪装这些不打算碰——一天一次的采集，不值得把出口 IP 搭进去，被标记影响的是这台机器走同一出口的所有事。

## 踩过的坑

价格的小数点会被吞掉，这个坑我查了半天才发现不怪正则：页面给读屏器的 `a-offscreen` 文本是 `$1787` 而不是 `$17.87`，直接用价格直接放大一百倍。真实价格藏在 `a-price-whole` 和 `a-price-fraction` 两段里，得自己拼（`17` + `87`）。`a-text-price` 是划线原价，也得排除，不然取到的是原价。

币种会跟着出口地域走。没指定币种的时候，从国内访问 amazon.com 返回的居然是 CNY 报价——同一条商品美国站 `$11.99`，这边 `CNY 80.07`。一开始以为是汇率换算问题，后来才弄明白是站点按访问者地域直接给了不同价目。按站点显式带 `i18n-prefs` / `lc-main` 两个 cookie 就好了。

还有一条：解析到 0 条必须报错。榜单页结构一改版 selector 就全空，这种时候宁可报「页面结构可能变化」跳过该站点，也不能静默写一堆空数据进库。

## 素材生成的边界

`material` 干两件事：给对标数据，提前拦违规文案。预检分两级，`[阻断]` 是明确不能上的（电子类目宣称医疗功效、文案带 `best seller` / `#1` / `guarantee` 之类），`[提醒]` 是建议改的（五点条数不足这种）。规则全在 `config.yaml` 的 `material:` 段，改规则不动代码。竞品默认最多采 5 个，一次别打太多页。

至于自动上架——不做。合规通道只有 SP-API 的 Listings Items API，还得卖家授权；拿 RPA 去点卖家后台发布，封店风险是实打实的。所以边界就划在素材和预检，最后一步提交留给运营在后台人工确认。

## 查历史

落库之后直接 SQL 查就行，比如找比上次掉了 10 名以上的 ASIN：

```sql
WITH t AS (
  SELECT asin, title, rank,
         LAG(rank) OVER (PARTITION BY asin ORDER BY run_date) AS prev_rank
  FROM ranks WHERE market='com' AND category='electronics')
SELECT asin, title, prev_rank, rank, rank - prev_rank AS delta
FROM t WHERE prev_rank IS NOT NULL AND rank - prev_rank >= 10
ORDER BY delta DESC;
```

`data/request_log` 表记了每次请求的状态、耗时和备注，被拦了能回看是从哪一次开始的。

## 合规

只取 robots.txt 的 `User-agent: *` 段没禁止的公开榜单页，不登录、不采买家信息，请求头就是常规那几个（UA / Accept / Accept-Language / Referer），不做指纹伪装。

## 结构

`tracker.py` 只是个兼容入口（`python tracker.py` == `python -m amazon_tracker.cli`），逻辑都在 `amazon_tracker/` 下。`fetch.py` 是唯一发网络请求的地方，节流、304、拦截判定全在里面，以后要动请求行为只改这一个文件；`parser`、`detail`、`material`、`diff` 都是纯函数，单测直接覆盖；`storage` 管 SQLite；`cli` 保持薄。

`tests/` 下 36 条单测，fixtures 是离线 HTML，CI 不联网。`data/` 是运行时产物，已 gitignore。
