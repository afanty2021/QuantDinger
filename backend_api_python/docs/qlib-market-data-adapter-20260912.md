# qlib 行情数据接入 CNStock 数据源设计方案（2026-09-12）

状态：**已实施（2026-09-12）**。本文档描述把本地 qlib bin 格式的 A 股日线数据接入 QuantDinger `CNStock` 数据源 fallback 链的方案，评审（有条件通过）后已落地；实施与验证记录见文末。

## 背景与目标

QuantDinger 已内置 `CNStock` 市场模块（research / backtest / paper），但在线数据链路存在两个痛点：

- 免费链路（腾讯 → yfinance → AkShare）受网络与限流影响，回测重复取数不稳定；
- Twelve Data 覆盖中国股票需要付费计划。

本地 qlib 数据目录（由用户的增量更新脚本维护，含 A 股与 ETF 日线）质量稳定、离线可用。目标：

1. 在 `CNStockDataSource` 的 fallback 链头部新增一个**离线 Tier 0**：qlib 数据可满足请求时优先返回，否则按现有链条继续在线取数。
2. 读 qlib bin 文件**不引入 qlib 依赖**（`np.fromfile` 逐次整读 + 进程内 LRU 缓存解析后的数组；`numpy` 顺手提升为 `requirements.txt` 直接依赖——此前仅经 pandas 传递引入，lock 已固定 `numpy==2.5.1`，无版本变化）。
3. 默认关闭（`QLIB_CN_DATA_DIR` 未设置时零行为变化），运营者显式配置后生效。

## 非目标

- 不支持 qlib 分钟线数据（本地维护的是日线；标准 cn_data 也只有日线）。
- 不改动 `get_ticker()` 实时报价链路（qlib 是 EOD 数据，报价继续走腾讯源）。但 `get_realtime_price` 的 1D K 线回退路径**会**经过本层，靠默认严格新鲜度模式保护（陈旧日历下整体 fall through）。
- 不支持 A 股实盘交易（`live_trading/` 无 A 股券商适配器，维持现状）。
- 不做数据入库（不写 PostgreSQL、不建 K 线缓存表），qlib 目录保持只读。
- 不改动 `DataSourceFactory` 的 market 路由（保持"按调用方传入 market 路由、不做 symbol 推断"的约定）。
- 不涉及 OpenAPI / Agent Gateway / MCP 变更（纯数据源内部层）。

## 现状与数据契约

### QuantDinger 侧（消费契约）

消费收口：所有消费方均收敛于 `DataSourceFactory` → `CNStockDataSource`——图表 / AI 分析 / 指标信号经 `app/services/kline.py::KlineService`，V2 回测经 `app/services/strategy_v2/market_data.py`，采集器（`market_data_collector`）直连 factory。因此数据源层接入后全部自动受益。**注意**：`KlineService.get_realtime_price` 在 ticker 失败时会回退取 `1D` 最近 2 根 K 线充当"实时价"（组合估值、行情报价的定价路径），该路径同样命中本层——这是新鲜度必须默认严格模式（见"窗口覆盖判定"）的直接原因。

行契约（`app/data_sources/base.py`）：

```python
[{"time": int, "open": float, "high": float, "low": float, "close": float, "volume": float}, ...]
# time 为 Unix 秒；调用方传 timeframe/limit/before_time/after_time
# 基类 filter_and_limit() 负责切片与截断
```

现有 CNStock fallback 链：

| Tier | 来源 | 覆盖周期 | 说明 |
|---|---|---|---|
| 1 | Twelve Data | 全周期 | 需付费 key |
| 2 | 腾讯 fqkline | 1D / 1W | 免费，`adj="qfq"` 前复权 |
| 3 | yfinance | 全周期 | 海外限流 |
| 4 | AkShare | 分钟级 + 1W | 东财接口，海外不稳定 |

关键事实：**现有腾讯层返回的已是前复权价（qfq）**，因此"qlib 复权价 vs 在线原始价"的顾虑不成立；真正的风险是各来源复权基准不一致（见"复权"一节）。

时间戳约定：`app/data_sources/tencent.py::parse_tencent_kline_time` 用 `datetime.strptime(...).timestamp()` **按服务器本地时区**解析日期字符串。新层必须沿用同一约定，保证跨 tier 行时间对齐。

### qlib 侧（存储契约）

以 `qlib_dir` 为根（例如 `~/.qlib/qlib_data/cn_data`）：

```text
calendars/day.txt            # 交易日历，每行一个 YYYY-MM-DD
instruments/all.txt          # SH600519<TAB>2001-08-27<TAB>2024-12-31（代码、起、止）
features/sh600519/open.day.bin
features/sh600519/close.day.bin
features/sh600519/high.day.bin / low.day.bin / volume.day.bin / factor.day.bin
```

bin 格式（`qlib/data/storage/file_storage.py`、`scripts/dump_bin.py` 验证）：小端 float32 一维数组，**首元素是该字段序列在交易日历上的起始索引**，其后为数值，按日历顺序对齐；UPDATE 模式为文件尾部 append。读取方式（`np.fromfile` 整读，**不使用 mmap**，理由见"线程与更新并发"）：

```python
raw = np.fromfile(path, dtype="<f4")
start_index = int(raw[0])
values = raw[1:]          # 对应 calendar[start_index : start_index + len(values)]
```

复权方向（qlib dump 管线验证，`scripts/data_collector/yahoo/collector.py::YahooNormalize1d.adjusted_price`）：**存储价 = 原始价 × factor，存储量 = 原始量 ÷ factor**；即 `原始价 = 复权价 ÷ factor`、`原始量 = 复权量 × factor`（`factor = adjclose / close`，前复权语义下 factor ≤ 1）。实施时用已知股票抽样对比腾讯原始价**与原始量**双向校验（写成可重复运行的 `scripts/` 校验脚本），防止约定记反。

精度：float32 约 7 位有效数字，高价股（如 1500+ CNY）绝对误差约 1e-4，为 qlib 格式固有属性，可接受，勿误判为读取 bug。

qlib 侧停牌日为 NaN（缺口以 NaN 填充对齐日历）。

## 方案总览

### 方案 A（推荐）：CNStockDataSource 内新增 Tier 0

新文件 `app/data_sources/qlib_cn.py` 提供 `fetch_qlib_daily_klines(...)` 帮助函数（签名风格对齐现有 `fetch_yfinance_klines` / `fetch_akshare_minute_klines`），`cn_stock.py` 在 Tier 1 之前调用。零路由改动，缓存、限流、`SHOW_CN_STOCK` / `ENABLED_MARKETS` 可见性全部复用。

### 方案 B（否决）：工厂注册独立命名数据源

`DataSourceFactory` 按 market 类别路由且明确"不做 symbol 推断"，独立命名源需要路由、前端策略配置与文档多处配合，收益仅是"可显式选择来源"。当前需求（离线优先 + 兜底）用 Tier 0 即可满足，故否决，留作后续演进。

## 模块设计

### 新文件 `app/data_sources/qlib_cn.py`

```python
class QlibBinStore:
    """只读 qlib bin 存储：日历/标的进程内加载一次，字段数组 np.fromfile 逐次整读 + LRU。"""
    def __init__(self, root: str): ...
    def validate(self) -> bool                        # calendars/ instruments/ features/ 目录齐备
    def calendar_epochs(self) -> list[int] | None     # day.txt 逐行按 parse_tencent_kline_time 同约定解析
    def last_calendar_epoch(self) -> int | None
    def intervals(self, qlib_code: str) -> list[tuple[date, date]] | None  # instruments/all.txt，支持多区间
    def read_field(self, qlib_code: str, field: str) -> tuple[int, np.ndarray] | None
        # np.fromfile 整读 features/<code>/<field>.day.bin
        # 校验：文件大小为 4 的倍数、头索引在 [0, len(calendar)] 内、len(values) 与之吻合
        # 任一校验失败返回 None（交由上层 fall through）
```

帮助函数（`cn_stock.py` 调用的唯一入口）：

```python
def fetch_qlib_daily_klines(
    tencent_code: str, timeframe: str, limit: int,
    before_time: int | None, after_time: int | None,
) -> list[dict] | None:
    # 返回 None = 本层无法满足，调用方继续走在线 tier
    # 返回 list = 完整满足，经 filter_and_limit 后直接返回
```

要点：

- **符号映射**：复用 `normalize_cn_code` 的产物（`sh600519` / `sz000001`），转 qlib 特征目录名（小写、无分隔）。`instruments/all.txt` 支持同一代码多区间行（qlib 的 list-of-intervals 语义），请求窗口与任一区间相交即视为标的可取。
- **周期**：仅接受 `1D` 与 `1W`（后者由日线聚合，见下）；其余周期直接返回 `None` fall through。
- **窗口起点推导**（优先级固定）：① `after_time` 优先——在日历上二分定位首个 `time >= after_time` 的会话；② 否则 `before_time`——二分定位最后一个 `time < before_time` 的会话后**往前数 `limit - 1` 个交易日**；③ 否则从日历**末日**往前数 `limit - 1` 个交易日。一律按**交易日**（日历行）计数，不是自然日。**1W 例外**：`limit` 计数的是周线根数，起点按 `limit` 个自然周（`limit × 7` 天）回推再二分，聚合后由 `filter_and_limit` 截到 `limit` 根——与腾讯 weekly 层的计数语义一致。`after_time` 存在时 `truncate=False`（回测全窗口），`limit` 不参与窗口裁剪。
- **窗口覆盖判定（含尾部语义）**：完整覆盖 = *请求窗口内所有已完成交易日都在 qlib 日历中*。形式化：设 `end_bound = before_time`（未设则 +∞），`last_completed` = 最近已完成的 A 股交易日（用 `exchange-calendars` 的 XSHG 日历 + 本地时间是否已过 15:05 收盘判定；库不可用时视为"无法验证新鲜度"，按不满足处理并限频告警）。则要求：
  - **起点覆盖**：`after_time`（或推导出的窗口起点）≥ 日历首日，且不早于字段 bin 的实际起始位置（bin 起点晚于窗口起点 = 左边缘数据空洞，同样整体 `None`，不得静默截短返回）；
  - **终点覆盖**：`last_calendar_epoch ≥ min(end_bound, last_completed)`——历史回测窗口（`end_bound` 远早于日历末日）不受数据新鲜度影响；触及"现在"的窗口（图表、实时价回退）要求日历覆盖到最近已完成交易日；
  - **默认严格模式**：终点覆盖不满足 → 整体返回 `None` fall through 到在线层（功能不退化，只是不走离线层）。`QLIB_CN_LENIENT=1` 显式豁免终点覆盖（纯离线部署用），此时图表尾部可能静默缺最近 N 根 K 线，由运营者自担。
  - 理由：避免"qlib 前半段 + 在线后半段"拼接出复权基准不一致的序列，这对回测正确性是硬约束；严格尾部判定同时保护 `get_realtime_price` 的定价回退路径不被陈旧 close 污染。
- **停牌 NaN**：返回前丢弃 OHLC 任一非有限的行（QuantDinger 全局 JSON 编码器会把 NaN 清洗为 null，语义上等价于该日无 K 线）；volume 非有限按 0 处理。不向前填充——与在线层行为一致。
- **1W 聚合**：按**自然周**分组（周一为界；ISO 年+周会把 12/30–01/03 跨年周拆成两根，与腾讯 weekly 层分桶不符——腾讯按自然周、标注周内最后一个交易日，已实测验证），OHLC 取极值/首尾、`volume` 求和、`time` 取**周内最后一根日线的时间戳**（与腾讯 weekly 层落点对齐，保证跨 tier 时间戳可比）。
- **切片**：日历二分定位 + `filter_and_limit`（含 `truncate=(after_time is None)` 语义，与现有 tier 完全一致）。
- **线程与更新并发**：**不使用 mmap**——qlib 目录由增量更新脚本在 backend 运行期间 append，mmap 只读映射在更新方 rewrite/截断时可触发 SIGBUS 击穿 worker 进程，且 append 中途读取会产生撕裂行；改为每次 `np.fromfile` 整读（单字段约 20KB，读放大可忽略），文件大小 4 字节对齐校验可拦住绝大多数撕裂读。日历与 instruments 一次性加载后只读；解析后的字段数组按 `(symbol, field)` 维度 `functools.lru_cache(maxsize=1024)` 缓存（硬编码，不做配置项），约 20MB 量级。更新脚本约定：只允许 append，不允许 rewrite/截断已有 bin；若必须重建数据，重启 backend 进程。
- **新鲜度告警**：宽松模式下 `last_calendar_epoch` 落后"今日"超过阈值（默认 7 个自然日，`QLIB_CN_STALENESS_DAYS` 可调）时限频打一条 warning，**不拒绝服务**；严格模式下终点覆盖不满足时限频打一条 warning（说明本层为何降级），同样不拒绝服务。

### `app/data_sources/cn_stock.py` 改动

`get_kline()` 中 Tier 1 之前插入：

```python
# Tier 0: local qlib bin data (offline, operator opt-in via QLIB_CN_DATA_DIR).
# All-or-nothing: returns None on any coverage gap so we never stitch
# a locally-adjusted series with an online one.
if tf in ("1D", "1W"):
    rows = fetch_qlib_daily_klines(code, tf, lim, before_time, after_time)
    if rows:
        return self.filter_and_limit(
            rows, limit=lim, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )
```

`get_ticker()` 不改动。qlib 目录单例封装在 `qlib_cn.get_qlib_store()` 内：`QLIB_CN_DATA_DIR` 为空时为 `None`（功能整体关闭）；配置了但目录无效时 log warning 并置 `None`（同样关闭，不影响在线链路）。

### 配置

`app/config/data_sources.py` 增加（沿用现有 `_config_str` 模式，支持 addon config + env 覆盖）：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `QLIB_CN_DATA_DIR` | 空（关闭） | qlib 数据根目录，须含 `calendars/`、`instruments/`、`features/` |
| `QLIB_CN_LENIENT` | `0`（严格） | `1` = 豁免终点覆盖（允许陈旧尾部），供纯离线部署；默认严格模式下终点覆盖不满足即整体 fall through |
| `QLIB_CN_STALENESS_DAYS` | `7` | 日历落后告警阈值（自然日），宽松模式下的日志参数，同时用于严格模式降级时的限频告警 |

`env.example` 增加注释示例，说明数据目录可用 qlib `scripts/get_data.py` 或用户自有增量脚本维护。

## 复权一致性决策（评审重点）

| 项 | 决策 | 理由 |
|---|---|---|
| 暴露价格语义 | 按 qlib 存储**原样暴露**（复权价 = 原始价 × factor），不做 factor 还原 | 现有腾讯层同为前复权；复权价收益率序列可直接用于回测与指标 |
| 暴露成交量语义 | 按 qlib 存储原样暴露（**复权量 = 原始量 ÷ factor**）；腾讯层 volume 为原始量 | 两者量纲在有分红股票上系统性偏离（factor < 1 的年份差 1/factor 倍）。因禁止拼接，单序列内部一致；跨层信号对比与用户对照东财原始量时会看到差异，运营者需知悉。实施校验时 volume 与 price 一并抽样 |
| 跨 tier 拼接 | 禁止（窗口覆盖不足时整体 fall through） | 各来源复权基准（锚定日）不同，拼接会产生虚假跳变 |
| factor 方向校验 | 以 qlib dump 管线约定（存储价 = 原始价 × factor、存储量 = 原始量 ÷ factor）为假设，写成可重复运行的 `scripts/` 抽样校验脚本，对比腾讯原始价**与原始量**双向验证 | 方向记反会导致整层数据错误，必须用已知数据校验而非仅凭文档；校验脚本便于用户更换数据目录后自检 |
| paper 盘定价 | 使用 qlib 层数据定价时，成交价与在线层可能存在复权基准差异 | 在"操作检查清单"中提示运营者：同一策略避免在 qlib 层与在线层之间频繁切换取数来源 |

## 测试计划

新增 `tests/test_qlib_cn_data_source.py`，测试内用临时目录构造 mini qlib_dir（手工写入 `day.txt`、`all.txt` 和已知数值的 bin 文件），不依赖真实 qlib 数据：

1. bin 头索引对齐：首元素为非零起始索引时切片正确。
2. `limit` / `before_time` / `after_time` 组合切片与 `truncate` 语义（含回测的 `after_time + before_time + truncate=False` 全窗口用例）。
3. 停牌 NaN 行被丢弃。
4. 符号映射：`600519` / `600519.SH` / `sh600519` 等价；`instruments` 区间外返回 fall-through。
5. 未知代码 / 缺失 bin 文件 / 目录无效 → 返回 `None`，`CNStockDataSource` 继续走在线 tier（mock 在线层验证调用顺序）。
6. `QLIB_CN_DATA_DIR` 未设置时零行为变化（默认关闭回归）。
7. 1W 聚合正确性（含跨年自然周 12/30–01/03 保持一根），并断言 `time` 落在该自然周内最后一根日线上（与腾讯 weekly 层对齐）。
8. 时间戳与 `parse_tencent_kline_time` 同约定（同一日期字符串产出相同 Unix 秒）。
9. 过期日历触发限频 warning 且不拒绝服务。
10. 损坏 bin：文件大小非 4 的倍数、仅 4 字节头、头索引超出日历长度 → 均返回 `None`（不产出错位数组）。
11. 尾部语义：日历落后于"最近已完成交易日"时，严格模式整体 fall through、`QLIB_CN_LENIENT=1` 时允许陈旧尾部返回（mock `_last_completed_session_epoch` 固定值）。
12. `instruments/all.txt` 同一 symbol 多区间行：窗口命中任一区间即可取数。

CI 约束：backend 测试需 PostgreSQL 18 + Redis（`basic-ci.yml` 环境）；本模块无 DB 依赖，但须在 `SKIP_STARTUP_HOOKS=1` 下可独立运行。`data_sources/` 不在 `backend_quality_baseline.json` hotspot 名单内，新增文件不触发结构守卫。

## 部署与回滚

- **无数据库迁移、无 OpenAPI 导出、无 MCP 同步**——改动收敛在 `app/data_sources/`（`qlib_cn.py` 新增 + `cn_stock.py` 接线）+ `app/config/data_sources.py`（QlibCNConfig）+ `scripts/verify_qlib_cn_data.py`（factor 方向抽样校验脚本）+ `requirements.txt`（numpy 提升为直接依赖，lock 无版本变化）+ env.example。
- 上线默认关闭；启用仅需设置 `QLIB_CN_DATA_DIR` 并重启 backend / trading-worker 进程（数据源为进程内单例）。
- 回滚 = 清空环境变量重启；注意 `KlineService` 的 Redis / 进程内缓存（无 `before_time` 的图表请求会走缓存）可能继续命中 qlib 来源数据直至 TTL 过期，要求立即生效时可手动清理 kline 缓存。
- 并发约定：qlib 层任何异常都被限定为"本层返回 fall-through"，不影响在线链路；更新脚本只允许对 bin **append**，不允许 rewrite / 截断（rewrite 期间的读取行为未定义，需重启进程）；`np.fromfile` + 文件大小 4 字节对齐校验兜住 append 撕裂读。

## 评审决议（2026-09-12，评审定夺）

原 5 个开放问题的定案：

1. **1W 重采样：做，保留在 phase 1**。门控已写死 `tf in ("1D", "1W")`，只出 1D 会让门控与实现不匹配；聚合逻辑约 20 行，仓库已有重采样先例（`moex.py::_resample`、`strategy_v2/service.py` 的 W-MON），测试面只增 1 组用例。
2. **新鲜度：默认严格模式**（终点覆盖 = 日历须覆盖到最近已完成 A 股交易日，XSHG 日历 + 15:05 收盘判定），否则整体 fall through，在线层兜底、功能不退化；`QLIB_CN_LENIENT=1` 显式豁免，供纯离线部署。7 天阈值降级为告警日志参数。理由：`get_realtime_price` 的 1D 回退路径会把陈旧 close 当实时价用于组合估值，7 天告警对该路径完全不够。
3. **ETF：phase 1 不做，独立提案**。除市场路由问题外，前置缺陷是 `normalize_cn_code` 的前缀规则会把沪市 ETF（`510300`）归为 `SZ` 前缀——先修 symbol-master 再谈路由，否则 ETF 数据接进来也取不到。
4. **指数：phase 1 不纳入**。标准 `all.txt` 通常不含指数而 `features/` 含（因数据版本而异），且指数请求依赖调用方显式传 `sh000300` 格式才能正确归一化。维持 fall through 即现状不变；将来若自有目录含指数 bin，用显式白名单环境变量单独开放，与 ETF 提案合并处理。
5. **lru 上限不做配置项**：1024 个 (symbol, field) 条目按解析后数组缓存约 20MB，量级无害；为不会触及的参数增加 `env.example` 面积不划算，硬编码即可。

评审修复记录：factor 方向声明更正（存储价 = 原始价 × factor，原稿写反）；补充 volume 复权语义与双向抽样校验；窗口起点推导优先级与尾部（终点覆盖）语义落定；读取方式由 mmap 改为 `np.fromfile` + LRU 解析缓存（消除更新并发下的 SIGBUS 风险）；测试计划增补 4 组负面/边界用例；回滚说明补充 kline 缓存清理。

## 实施与验证记录（2026-09-12）

**代码落点**：`app/data_sources/qlib_cn.py`（新增）、`app/data_sources/cn_stock.py`（Tier 0 接线 + 模块头注释）、`app/config/data_sources.py`（`QlibCNConfig`）、`scripts/verify_qlib_cn_data.py`（新增，factor 方向抽样校验）、`requirements.txt`（`numpy==2.5.1` 提升为直接依赖）、`env.example`、`tests/test_qlib_cn_data_source.py`（新增）。无迁移 / 无 OpenAPI / 无 MCP 改动。

**单元测试**：19 个用例全部通过（覆盖测试计划 1–12 全部条目；与 `test_data_source_base.py` 合跑 25 passed）。本地为 py3.12 最小依赖环境（qlib 层无 DB 依赖）；完整 backend 测试矩阵（PostgreSQL + Redis）随 CI 验证。

**真实数据验证**（`~/.qlib/qlib_data/cn_data`，SH600519）：

- `scripts/verify_qlib_cn_data.py` 抽样 4 行：`raw = 复权价 ÷ factor`、`raw量 = 复权量 × factor` 还原值与腾讯未复权价/量**逐位一致**（如 2026-04-22：1409.5000 / 26916），factor 方向定论。
- 宽松模式（`QLIB_CN_LENIENT=1`）：1D / 1W 均由本层出数，1W 自然周聚合正确，陈旧日历触发限频告警。
- 严格模式（默认）：XSHG 日历判定 2026-04-22 落后于最近已完成交易日 → 限频告警 → 整体 fall through，在线层返回最新 bar（2026-09-11）。

**运营注意**：本机 qlib 目录当前停在 2026-04-22（增量脚本未跑）。在日历更新前，严格模式下本层持续 fall through（行为等同现状，无风险）；想让陈旧数据参与回测需显式设 `QLIB_CN_LENIENT=1`。实施中发现并修正一处评审后问题：1W 聚合初版按 ISO 年+周分组会把 12/30–01/03 跨年周拆成两根，已改为按自然周（周一为界）分组并与腾讯 weekly 层实测对齐。
