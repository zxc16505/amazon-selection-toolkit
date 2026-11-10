# 跨境选品工具（Amazon 榜单追踪 + 上架素材生成）

按站点采集 Amazon 榜单（排名、ASIN、标题、价格、评分、评论数），落 SQLite；
每次运行与上一次快照做 diff：新进榜 / 上升 / 下降 / 掉出 / 价格变动。

在此之上提供**上架素材生成**：采集目标品与竞品详情页，产出竞品价格带、
高频关键词、类目合规预检报告，**全程只读，不触发任何上架/提交动作**。

自建，用于理解跨境电商选品侧的数据采集与变化追踪，**不是生产系统**：
单机脚本、单日一次、无分布式调度、无代理池。

---

## 一、执行链路

```
当天已采过？──是──> 跳过，不产生任何请求
   │否
站点配置（域名 / 本币 / 语言）
   → 请求（cookie 复用 + 条件请求 + 钉住币种）
   → 拦截检测 ──命中──> 整轮立即停止，退出码 2
   → 解析（div#gridItemRoot 逐条取 排名 / ASIN / 标题 / 价格 / 评分 / 评论数）
   → 落库（SQLite，主键 = 日期 + 站点 + 榜单类型 + 类目 + ASIN）
   → 与上一次快照 diff
   → 控制台输出变化
```

站点之间随机间隔 5-10 秒；单站点失败只记录原因，不阻塞其他站点入库；
**命中拦截特征时，整轮立即中断，不重试、不换站点继续。**

## 二、针对「连续访问被拦截」的四项处理

按拦截页提示「To discuss automated access to Amazon data…」反查，
社区与官方文档里被反复提到的有效手段集中在**少发请求、别伪装成新客户端**，
而不是「想办法绕过」。这里实现了其中不涉及伪装的部分：

| 措施 | 做法 | 为什么有用 |
| --- | --- | --- |
| **当天去重** | 同一站点同一榜单同一天最多请求一次，`--force` 才重跑 | 直接把「连续访问」这个前提消掉，是收益最大的一条 |
| **会话复用** | cookie 落盘到 `data/cookies.json`，下次沿用同一 session-id | 每次都是全新会话是最容易被判定为脚本的特征之一 |
| **条件请求** | 记 ETag / Last-Modified，下次带 `If-None-Match` / `If-Modified-Since`，未变更返回 304 | 榜单页在小时内变化不大，304 不下载正文，请求量不变但流量与解析成本下降 |
| **退避** | 429/503 优先读 `Retry-After`，没有则 30s、90s 递增，单站点最多 3 次 | 固定间隔和立刻重试都是可识别模式；退避也比「多开并发」安全 |

**明确不做**（不是做不到，是不该做）：

- 代理池 / IP 轮换：换 IP 继续打是被识别的特征，不是解法
- 验证码识别 / 打码平台
- 浏览器指纹伪装、UA 随机化
- 多账号轮换

遇到拦截就停。这个项目采的是公开榜单，**不值得为一次采集把出口 IP 搭进去**——
被标记之后影响的是这台机器上所有要走同一个出口的事。

## 三、上架素材生成（只读）

`material` 子命令做两件事：**给出对标数据**、**提前拦下违规文案**。
它不登录卖家后台、不调 SP-API、不写任何平台字段——产出是一份给运营看的 Markdown。

```
目标 ASIN + 竞品 ASIN 列表
   → 逐个取商品详情页（复用第二节的 cookie 复用 / 条件请求 / 拦截即停）
   → 解析：标题、品牌、价格、评分、评论数、类目路径、五点描述
   → 汇总：竞品价格带（最低 / 中位 / 最高）、高频词与二元短语
   → 合规预检：标题超长、五点不足、禁用绝对化表述、类目宣称红线
   → 输出 data/material/<ASIN>-<日期>.md 与同名 .json
```

合规预检分两级：`[阻断]` 是明确不能上架的（如电子类目宣称医疗功效、
文案含 `best seller` / `#1` / `guarantee`），`[提醒]` 是建议修改的（如五点条数不足）。
规则全部外置在 `config.yaml` 的 `material:` 段，改规则不用改代码。

关于「自动上架」：**不做**。合规通道只有 Amazon SP-API 的 Listings Items API，
且需要卖家授权；用 RPA 点卖家后台发布商品属于违规操作，有封店与法律风险。
所以这里把边界划在「素材与预检」，最后一步的提交留给运营在后台人工确认。

## 四、实测输出

`python tracker.py --markets com --category electronics --top 30`

```
Amazon com / bestsellers / electronics @ 2026-11-10（30 条） 200 OK
  # 1  blink plus plan with monthly auto-renewal            11.99 USD
  # 2  Apple EarPods Headphones with USB-C Plug, Wired Ear  17.87 USD
  # 3  Apple AirTag (2nd Generation): Tracker for Keychain, 19.13 USD
  # 4  Apple AirPods Pro 3 Wireless Earbuds with Active Noi 183.44 USD
  # 5  Amazon Fire TV Stick 4K Plus with AI-powered Fire TV 39.99 USD
  （首次运行，下次再跑即可看到排名/价格变化）
```

同一天再跑：

```
当天已采过，跳过（--force 可强制重跑）: com
```

落库后可直接用 SQL 查历史，例如「一周内掉了 10 名以上的 ASIN」：

```sql
WITH t AS (
  SELECT asin, title, rank,
         LAG(rank) OVER (PARTITION BY asin ORDER BY run_date) AS prev_rank
  FROM ranks WHERE market='com' AND category='electronics')
SELECT asin, title, prev_rank, rank, rank - prev_rank AS delta
FROM t WHERE prev_rank IS NOT NULL AND rank - prev_rank >= 10
ORDER BY delta DESC;
```

## 五、实测踩到的三个问题

**1. 币种随出口地域漂移。**
不指定币种时，从国内出口访问 `amazon.com` 返回 **CNY 报价**（实测第一条 `CNY 80.07`，
同一条商品在美国站是 `$11.99`）。这不是汇率换算，是站点按访问者地域给了不同价目。
解法：按站点显式设置 `i18n-prefs` / `lc-main` cookie。

**2. 价格小数点在无障碍文本里被吞掉。**
页面对读屏器提供的 `a-offscreen` 文本是 `$1787` 而不是 `$17.87`——直接用会把 17.87 读成 1787。
真实价格由 `a-price-whole` + `a-price-fraction` 两段拼成（`17` + `87`）。
另外 `a-text-price` 是划线原价，必须排除，否则取到的是原价不是现价。

**3. 连续访问会触发自动化访问拦截。**
同一站点短时间重复请求后，返回一张约 3.8KB 的拦截页（正常页面约 450KB），
正文只有一句「To discuss automated access to Amazon data please contact …」。
脚本用「页面小于 20KB **且** 命中特征串」两个条件一起判，避免误伤正常页；
命中后中断整轮全部后续请求，退出码 2。

## 六、合规性

- `https://www.amazon.com/robots.txt` 的 `User-agent: *` 段**未禁止** `/gp/bestsellers/`，本项目只取公开榜单页。
- 榜单页无需登录，不含个人信息；不采集买家信息、不采集订单。
- 请求头只带常规的 UA / Accept / Accept-Language / Referer，不做指纹伪装。

## 七、用法

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

站点代码：`com` `co.uk` `de` `co.jp` `ca` `com.au`。
（实测当前网络环境下 `co.jp` 连接超时，属站点可达性问题，不影响其他站点入库。）

`material` 的 `--from-html` 模式不需要联网：第一个文件是目标品，其余是竞品，
适合在没有可用出口、或只想调解析/规则时把整条链路跑通（CI 就是这么测的）。

`data/request_log` 表记录每次请求的状态、耗时与备注，可以据此看拦截是从哪一次开始的。

## 八、边界

- 未做：分布式调度、失败重试队列、告警推送、增量断点续跑。单日一次跑完即可，量级不需要。
- 排名与价格本身会变，库里存的是「某天看到的快照」，不是实时值。
- 榜单结构改版会导致解析到 0 条，脚本会报「页面结构可能变化」而不是静默入库空数据。
- 素材生成只读详情页、只产出本地文件；**不做自动上架**，也不接管卖家后台。
- 竞品采集数量默认上限 5 个（`config.yaml` 的 `material.max_competitors`），避免一次请求过多页。

## 九、目录

```
tracker.py              兼容入口（python tracker.py == python -m amazon_tracker.cli）
config.yaml             站点 / UA / 拦截特征 / 退避 / 合规规则，外置不改代码
requirements.txt

amazon_tracker/
├── cli.py              argparse 入口：crawl / material 两个子命令
├── config.py           Config / Market 数据类 + YAML 加载
├── fetch.py            HTTP 采集（唯一发网络请求的模块，榜单页与详情页共用）
├── parser.py           榜单页解析 + 价格拼接
├── detail.py           商品详情页解析（标题 / 品牌 / 价格 / 评分 / 类目 / 五点）
├── material.py         关键词、价格带、合规预检、素材 Markdown 渲染（纯函数）
├── storage.py          SQLite 存储 + request_log
├── diff.py             与上次快照对比（新进榜 / 升降 / 掉出 / 价格变动）
└── utils.py            纯函数：parse_price / is_block / retry_after

tests/
├── test_tracker.py     20 条单元测试（解析 / 价格 / 退避 / 拦截 / diff）
├── test_material.py    16 条单元测试（详情解析 / 价格带 / 关键词 / 规则 / 渲染）
└── fixtures/           离线 HTML 样本，测试与演示都不联网

.github/workflows/ci.yml  编译 + 单元测试 + CLI help
data/                  运行时产物（已 .gitignore）
  ├── ranks.sqlite3
  ├── cookies.json
  ├── http_cache.json
  └── material/        生成的上架素材（Markdown + JSON）
```

**为什么要拆模块**：`fetch.py` 是唯一发网络请求的——以后任何想加"多线程并发""代理池""Playwright 渲染"，都改这一个文件；`utils.py` + `diff.py` + `parser.py` + `detail.py` + `material.py` 是纯函数，直接写单元测试覆盖；`cli.py` 保持薄，只管"先判断今天要不要跑 → 跑 → 存 → 对比"或"取详情 → 汇总 → 校验 → 出素材"。
