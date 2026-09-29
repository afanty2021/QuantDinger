# 公开股票池基础库与基本面数据约定

## 1. 默认公开股票池

QuantDinger 可以从公开数据源维护以下股票池。实际成员数量与来源版本会随
再平衡、数据源可用性和完整性校验变化，不应把文档中的示例数量当作交易保证。

| 股票池 | 来源与约束 |
|---|---|
| 沪深300、中证500 | 中证指数公开成分接口，经 AKShare 适配 |
| 标普500 | `datasets/s-and-p-500-companies`，ODC PDDL |
| 纳斯达克100 | `Gary-Strauss/NASDAQ100_Constituents`，MIT；底层数据来自 Wikipedia，需保留 CC BY-SA 署名 |
| 加密市值 Top-100 | CoinGecko 当前市值排序接口 |
| 恒生指数系列 | 恒生指数公司公开 factsheet，包括 HSI、HSTECH、HSCEI 和 HSHDYI |

首次导入只建立当时可观察到的成分快照，不会伪造更早的历史成员。后续更新会
关闭被剔除成分的有效区间，并为新增成分建立新的 `valid_from`，逐步积累平台
自己的时点历史。

刷新命令：

```bash
python scripts/refresh_public_universe_snapshots.py \
  --universes sp500,nasdaq100,csi300,csi500,crypto_top100,hk_hsi_core50,hk_tech30,hk_china_enterprises50,hk_high_dividend50 \
  --as-of YYYY-MM-DD
```

先增加 `--dry-run` 检查数量。脚本内置完整性门槛，数量异常时拒绝写库。

## 2. 港股和 ETF 分类

港股指数池使用恒生指数公司 factsheet，ETF 分类直接使用证券主表：

- 港股指数池：恒生指数核心50、恒生科技30、恒生国企50、恒生高股息50；保存官方权重和行业。
- 港股核心 ETF：`HKStock + etf + is_hot`，由证券目录维护宽基、科技、红利、黄金和债券 ETF
- 美股核心 ETF：`USStock + etf + is_hot`，由证券目录维护主流宽基、行业、债券和商品 ETF
- 全量港股仍保留在证券搜索主表，不默认作为截面回测池。

证券同步会读取 HKEX `ListOfSecurities.xlsx` 的 `Category` 字段，把 `Equity` 和 `Exchange Traded Products` 分开。美国证券目录使用 Nasdaq Trader 的 `ETF` 标记。

## 3. 基本面数据

`qd_fundamental_snapshots` 保存时点基本面。最重要的两个日期是：

- `period_end`：报表所属期间结束日。
- `available_at`：市场实际能看到这份数据的日期，回测只能从该日开始使用。

支持字段：

```text
revenue
net_income
book_value
shareholder_equity
total_debt
free_cash_flow
shares_outstanding
market_cap
```

小市值因子优先使用供应商给出的 `market_cap`；如果缺失，则使用当日收盘价乘以当时已公开的 `shares_outstanding`。财报值会从 `available_at` 起向后生效，不会回填到公告日前。

CSV 导入：

```bash
python scripts/import_fundamental_snapshots.py fundamentals.csv \
  --source manual_csv \
  --source-version YYYY-MM
```

CSV 至少包含：

```text
market,symbol,period_end,available_at,frequency,currency
```

其余基本面字段可以为空。做小市值策略至少需要 `market_cap`，或同时提供 `shares_outstanding` 和日线收盘价。

## 4. 已知限制

- 公开快照适合产品启动和研究验证，不等价于官方商业指数授权。
- 标普和纳斯达克名称、原始成分展示及商业使用仍应在正式运营前复核许可。
- 加密 Top-100 当前未自动排除稳定币、包装资产和质押衍生品，生产规则需要另行固化。
- 真正的历史基本面需要接入财务数据源，手工 CSV 只适合小规模验证。
- 小市值策略在没有时点市值或股本数据时会得到空排名，系统不会用股价或成交额伪造市值。
