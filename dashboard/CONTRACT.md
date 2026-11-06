# A股涨停直播看板 — 实现契约 v1（Lead 维护，勿改）

单文件后端 + 单页前端。项目根目录 = `ashare-limitup-tracker/`。
交付物：`app.py`（后端，所有数据逻辑）、`page.html`（唯一 HTML 页面）、`requirements.txt`、`README.md`。
运行方式：`python app.py` → http://127.0.0.1:8000 。

**硬性约束**
- 免费 akshare，不得使用付费/需要 key 的数据源。绝不编造数据：取不到就显示"暂无数据"，保留上一份好数据并亮红色 stale 横幅。
- 每个 akshare 调用都包 try/except；两次 akshare 调用之间全局至少间隔 1.0 秒（跨线程也要节流）。
- 列名按关键词防御式映射（中文字段名可能变），未知列只 log 一次，不得崩溃。
- 前端纯静态单页（Chart.js 走 CDN，可离线时显示"N/A"但不得报错中断整页）。
- 页面中文标签、深色主题。所有时间用北京时间（Asia/Shanghai）。

---

## 1. 文件所有权（写作用域）

| 文件 | 负责人 |
|---|---|
| `app.py`, `requirements.txt` | backend-dev |
| `page.html` | frontend-dev |
| `tests/`, `scripts/`（只读验证脚本） | qa-verifier |
| `CONTRACT.md`, `README.md` | Lead |

其他人不得写别人作用域的文件。Lead 负责最终集成与验收。

---

## 2. HTTP 接口

### `GET /`
返回 `page.html` 内容（同目录静态读取，`Cache-Control: no-store`），`Content-Type: text/html; charset=utf-8`。

### `GET /api/snapshot?watch=600519,000001`
返回 JSON（UTF-8，`Content-Type: application/json`）。`watch` 可选，逗号分隔 6 位代码（允许带 `sh/sz` 前缀，后端归一化为 6 位）。
这是前端唯一轮询的数据源；**前端不要自己直接调外部 API**。

### `GET /api/health`
`{"ok": true, "uptime_sec": 12.3}`，不做任何 akshare 调用（给测试用）。

### `GET /api/refresh?watch=...`
强制刷新一次（同步等待，最多约 30s），返回与 `/api/snapshot` 相同结构；用于手动/测试。

---

## 3. `/api/snapshot` JSON 结构（字段名/单位严格按此）

```jsonc
{
  "ok": true,                       // 服务本身是否正常（即便数据 stale 也常为 true）
  "generated_at": "2026-11-05T00:30:12+08:00",   // 服务器当前北京时间 ISO8601
  "refreshed_at": "2026-11-04T15:00:10+08:00",   // 最近一次"成功拉全量"的时间；从未成功=null
  "trade_date": "20261104",         // 实际数据所属交易日 YYYYMMDD
  "requested_date": "20261105",     // 按规则本该取的日期 YYYYMMDD
  "is_fallback": true,              // trade_date != requested_date 时为 true
  "fallback_note": "20261105 盘前无数据，已回退到最近交易日 20261104",  // 正常时为 null
  "market_phase": "closed",         // pre_open | auction | trading | lunch | closed | non_trading_day
  "refreshing": false,
  "stale": {"is_stale": false, "since": null, "reason": null},
      // is_stale=true 时 since="2026-11-04T14:59:02+08:00"（最后成功时刻），reason="akshare 拉取失败: ..."
  "stats": {
    "limit_up_count": 66,
    "broken_count": 14,
    "limit_down_count": 2,
    "broken_rate": 0.175,           // 0..1（炸板/(涨停+炸板)），分母 0 时为 null
    "max_streak": 6,               // 连板高度；无数据=0
    "prev_limit_up_premium": 2.31, // 昨日涨停股今日平均涨跌幅 %，可为负；不可用=null
    "prev_limit_up_count": 55
  },
  "themes": [                       // 题材榜（按概念分组涨停股），已排序
    {
      "name": "机器人",
      "count": 12,
      "earliest_first_seal": "09:25:00",
      "is_industry_fallback": false,   // true = 概念映射缺失，用所属行业兜底
      "stocks": [ /* Stock 对象，按 first_seal 升序 */ ]
    }
  ],
  "ladder": [                       // 连板梯队，按 streak 降序，只含涨停股
    {"streak": 6, "stocks": [ /* Stock 对象，按 first_seal 升序 */ ]},
    {"streak": 5, "stocks": [ ... ]}
  ],
  "broken": [                       // 炸板股池
    {
      "code": "600123", "name": "示例股份",
      "concepts": ["机器人"], "industry": "通用设备",
      "high_pct": 10.01,            // 当日最高涨幅 %（数据源有"最高涨跌幅/涨跌幅"时按实际；取不到则为 null）
      "current_pct": 3.21,          // 当前涨幅 %
      "break_times": 2,             // 炸板次数
      "first_seal": "09:31:00",     // 首次封板时间，没有则 null
      "break_time": "10:23:45"      // 炸板时间（有该列则填，否则 null）
    }
  ],
  "limit_down": [
    {"code": "600124", "name": "示例跌停", "pct": -9.98, "concepts": [], "industry": "..."}
  ],
  "timeline": [                     // 首次封板时间线（Chart.js 用）
    {"code": "600123", "name": "示例股份", "first_seal": "09:25:00", "theme": "机器人",
     "streak": 3, "seal_amount_yi": 1.23}
  ],
  "watchlist": [                    // 自选股实时行情（来自 stock_zh_a_spot_em，含非涨停股）
    {"code": "600519", "name": "贵州茅台", "price": 1500.0, "pct": -1.23,
     "is_limit_up": false, "is_limit_down": false, "in_limit_up_pool": false,
     "streak": null, "ok": true, "error": null}
  ],
  "concepts": {"ready": false, "loaded": 120, "total": 456, "updated_at": null,
               "note": "概念映射构建中 120/456，完成后自动刷新"},
  "warnings": ["..."],              // 非致命问题（例如某个数据源失败但主数据可用）
  "log_file": "data/20261104.csv"   // 本次快照写入的 CSV 相对路径；未写=null
}
```

**Stock 对象**（themes / ladder 内）：
```jsonc
{
  "code": "600123", "name": "示例股份",
  "streak": 3,                     // 连板数
  "first_seal": "09:25:00",        // 首次封板时间 HH:MM:SS
  "last_seal": "10:11:00",         // 最后封板时间，没有则 null
  "seal_amount_yuan": 123456789.0, // 封板资金，元（浮点）
  "seal_amount_yi": 1.23,          // 封板资金，亿（保留 2 位）
  "break_times": 1,                // 炸板次数
  "turnover": 12.3,                // 换手率 %
  "float_cap_yuan": 12345678901.0, // 流通市值，元
  "float_cap_yi": 123.46,          // 流通市值，亿（保留 2 位）
  "industry": "通用设备",
  "concepts": ["机器人", "减速器"],  // 最多 2 个（最热概念优先）
  "is_watch": false
}
```
- 所有 code 是 6 位字符串（`"000001"`）；金额数值型；缺失值用 `null`，**不要**用 `"-"`/`""`/`NaN`（NaN 会导致 JSON 非法）。
- 接口返回必须可被 `json.dumps(..., ensure_ascii=False, allow_nan=False)` 序列化。

---

## 4. 后端行为规范

### 4.1 交易日与回退
- 用 `ak.tool_trade_date_hist_sina()` 取交易日历（缓存到 `data/trade_calendar.json`，进程内+文件级，一天一刷；失败时按"工作日=交易日"退化，并记 warning）。
- `requested_date` 规则：当前北京时间 < 09:25 或**非交易日** → 最近一个已开盘交易日；否则今天。
- 实际取数：先取 `requested_date`；若 `stock_zt_pool_em` 返回空/报错，则往前找最近交易日重试（最多回溯 10 个自然日）。成功日 = `trade_date`，不同则 `is_fallback=true` + 中文说明。
- 必须能在**非交易日运行**：自动展示最近交易日快照。

### 4.2 akshare 数据源与关键词映射
| 用途 | 接口 | 关键词映射（中文字段） |
|---|---|---|
| 涨停池 | `ak.stock_zt_pool_em(date=YYYYMMDD)` | 代码, 名称, 连板数/连板, 首次封板时间, 最后封板时间, 封板资金, 炸板次数, 所属行业/行业, 换手率, 流通市值 |
| 炸板池 | `ak.stock_zt_pool_zbgc_em(date=YYYYMMDD)` | 代码, 名称, 涨跌幅, 最高涨跌幅/涨跌幅, 炸板次数, 首次封板时间, 炸板时间, 所属行业 |
| 跌停池 | `ak.stock_zt_pool_dtgc_em(date=YYYYMMDD)` | 代码, 名称, 涨跌幅, 所属行业 |
| 昨日涨停溢价 | `ak.stock_zt_pool_previous_em(date=YYYYMMDD)` | 代码, 名称, 涨跌幅 → 求平均涨跌幅 |
| 自选实时 | `ak.stock_zh_a_spot_em()` | 代码, 名称, 最新价, 涨跌幅（一次拉全市场后过滤自选） |
| 概念列表 | `ak.stock_board_concept_name_em()` | 板块名称/概念名称, 涨跌幅 → 用于排序热度优先级 |
| 概念成分 | `ak.stock_board_concept_cons_em(symbol=概念名)` | 代码, 名称 → 建 stock→concept 映射 |
- 列名映射用**关键词包含匹配**（例如含"封板时间"），大小写/空格/`-`不敏感；找不到必需列时该数据源整体判失败并记 warning，不要整页崩。
- 未匹配到的列：每个函数每进程只 `log.warning` 一次（格式 `[columns] stock_zt_pool_em 未识别列: [...]`）。

### 4.3 概念映射（慢，后台线程）
- 每日一份缓存 `data/concept_map_YYYYMMDD.json`（`{code: [concept, ...]}`）与 `data/concept_list_YYYYMMDD.json`。
- 后台线程逐个概念调 `stock_board_concept_cons_em`，每 1.0~1.3s 一个，每完成 20 个落盘一次；概念顺序按当日板块涨幅降序（热点优先）。
- 概念映射缺失/未完成时：用涨停池的"所属行业"作为题材兜底（`is_industry_fallback=true`），并在 `concepts` 字段报告进度；**不得因此阻塞页面**。
- 每只股票最多展示 2 个概念：取"今日涨停家数最多的概念"排序，同数量再按概念当日涨幅降序；行业兜底只在该股没有已知概念时使用。
- 题材分组：组内股票按 first_seal 升序；题材排序：count 降序 → 最早 first_seal 升序 → 名称。

### 4.4 定时刷新
- 北京时间 **09:25–11:30、13:00–15:00** 每 60s 拉一次；其余时段不拉，继续展示最后快照（启动时若无任何快照则强制拉一次）。
- 后台刷新线程与 `/api/refresh` 互斥（同一把锁），避免并发打爆。
- 每次成功刷新：内存快照更新 + CSV 追加 + `refreshed_at` 更新。
- 失败：保留上一份好数据；`stale.is_stale=true`、`since`=上次成功时间、`reason`=异常摘要；重试不改变成功时间。从未成功时 `refreshed_at=null` 且前端显示"暂无数据"。
- 交易时段内若距上次成功 > 180s，也算 stale。

### 4.5 CSV 落盘
- 路径 `data/YYYYMMDD.csv`，YYYYMMDD = `trade_date`（数据日，不是墙钟日；非交易日回退时仍写数据日文件）。
- 表头（首行，utf-8-sig）：`timestamp,code,name,status,streak,first_seal,seal_amount,break_times,concepts`
- 每次成功刷新对当日行追加：涨停股 `status=limit_up`、炸板股 `status=broken`、跌停股 `status=limit_down`。
  `timestamp` 为刷新时间 `YYYY-MM-DD HH:MM:SS`；`seal_amount` 单位元；`concepts` 用 `|` 连接；CSV 必须正确转义逗号（用 csv 模块）。
- 并发写加锁。

### 4.6 启动与配置
- `python app.py` 直接可用；`PORT` 环境变量可覆盖端口（默认 8000），host 默认 `127.0.0.1`。
- 环境变量 `LIMITUP_DISABLE_BG=1`：不启动后台刷新/概念线程（QA 单测用）；`LIMITUP_FAKE_FAIL=1`：每次拉取都抛异常（QA 验证 stale 用）。
- 启动日志：`[startup] ...`、`[refresh] ...` 带耗时；所有日志走 `logging`（INFO 级，stderr 带时间）。
- 纯函数（可单测，不依赖网络/线程）：`pick_col`/`map_limit_up_row`/`resolve_dates`/`build_stats`/`group_themes`/`build_ladder`/`compute_phase`/`should_refresh`/`format_hms`/`to_yi`。
- `/api/snapshot` 在后台线程锁内**拷贝快照**后返回，不要让请求线程等待网络。

---

## 5. 前端规范（page.html，单文件，内联 CSS/JS）

深色主题（背景 #0d1117 级别，卡片 #161b22，红涨绿跌的中国配色：涨=#ff4d4f 红，跌=#00b578 绿，涨停用红色高亮）。中文标签。布局自上而下：

1. **顶栏 Header**：标题、最后刷新时间（北京时间 HH:MM:SS）、数据日/回退提示、统计块：涨停数、炸板数、跌停数、炸板率、连板高度、昨日涨停今日平均溢价（带正负颜色）。`market_phase` 中文映射（盘中/午休/收盘/盘前/非交易日）。刷新按钮。stale 时顶部整条红色横幅："数据已过期：自 HH:MM 起未更新（原因）"。
2. **题材榜 Theme board**：卡片流；每卡标题 `概念名 ×只数`，卡内表格/列表：股票名(代码)、连板数徽章、首次封板时间、封板资金(亿)、炸板次数、流通市值(亿)。点击股票可加入/移出自选（可选，加分）。
3. **连板梯队 Ladder**：横向列，列头 `N连板 (x只)`，最高连板在最左；列内股票名+代码（不同高度用不同徽章颜色）。
4. **炸板股池表**：名称、概念、最高涨幅、当前涨幅、炸板次数、炸板时间。
5. **时间线 Timeline**：Chart.js CDN（`https://cdn.jsdelivr.net/npm/chart.js@4`）横向条形图，y 轴=股票名，x 轴=首次封板时间（按 09:25→15:00 时间轴，用分钟数转时间，倒序/按时间顺序均可），每只股票一根条，按题材着色（题材色板 hash 稳定），tooltip 显示名称/代码/题材/首次封板/连板数。
6. **自选 Watchlist**：输入框（逗号/空格/换行分隔 6 位代码，或点股票加入），保存 localStorage key `limitup.watchlist`；展示每只的实时价/涨跌幅/状态（涨停/跌停/普通），非交易时段说明数据时间。自选股票在页面任何出现处高亮（金色左边框/背景）。
7. **告警**：右上角请求浏览器通知权限按钮；轮询比较前后快照，触发：
   - (a) 自选股**新封涨停**（上一份不在涨停池，本份在）；
   - (b) 连板数 ≥3 的股票**炸板**（上一份在涨停池且连板≥3，本份出现在炸板池）；
   - (c) 炸板率**上穿 40%**（上一份 <0.40 且本份 ≥0.40）。
   触发时：浏览器 Notification（权限已给）+ 页面顶部告警条 + 对应行高亮闪烁；同一事件不重复报（用事件 key 去重，保存在内存 + localStorage 短时）。
8. **数据状态**：轮询 `/api/snapshot?watch=...`，间隔：盘中 60s，非盘中 5 分钟（或保持不变，展示最后快照；有"刷新"按钮手动拉）。请求失败：显示红色横幅"接口请求失败"，保留上次数据，不编造。
- 空数据/字段缺失：显示"暂无数据"占位，不报错。数字格式化：亿保留 2 位、百分比 2 位、时间 HH:MM:SS。
- 响应式：宽度 <1200px 时卡片换行；不出现横向滚动条遮挡。
- 页面无 console error；Chart.js 加载失败时显示占位文本而非抛错。

---

## 6. 验收清单（QA 用）

1. `LIMITUP_DISABLE_BG=1 ./.venv/bin/python app.py` 启动无异常，`/api/health` 200。
2. `curl '/api/snapshot?watch=600519,000001'` 返回合法 JSON 且符合本契约；今天 2026-11-05 00:xx（盘前/非交易时段）应 `trade_date=20261104`、`is_fallback=true`、`stats.limit_up_count>0`。
3. CSV：`data/20261104.csv` 存在，表头正确，行数 = (涨停+炸板+跌停) 数量，UTF-8。
4. 概念映射：`data/concept_map_*.json` 生成且后台线程有日志；页面 `themes` 非空（概念或行业兜底）。
5. Chrome headless 截图（1600x1200）：深色、六个区块齐全、Chart.js 图渲染、无 console error。
6. 非交易日/盘前回退验证 + stale 逻辑单测（`LIMITUP_FAKE_FAIL=1` 时接口仍返回上次好数据且第二次失败后 `stale.is_stale=true`）。
7. `python -m pytest tests/ -q` 全绿（可选但推荐：至少 8 个纯函数/契约单测）。
8. 列名防御：单测传入改名 DataFrame 仍能映射；未知列 warning 只打一次。
