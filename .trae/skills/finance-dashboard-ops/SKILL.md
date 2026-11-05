---
name: finance-dashboard-ops
description: 财经监控台迭代运维流水线。Invoke when 用户要给 demonh6.github.io 仪表盘新增/替换数据源、加行情/情绪/风险指标、改前端面板，或提到 update_data.py、dashboard.json、量化监控台、VIX/量能/告警——按"探测数据源→脚本集成→前端渲染→Actions上线→线上验证"五步执行。
---

# 财经监控台迭代运维

对 demonh6.github.io（/workspace 即仓库根）做任何"加数据源 / 加指标 / 改面板"的迭代，
按本流水线执行。核心原则：**先探测后编码、单源失败不拖垮全局、Actions 环境与沙箱有差异**。

## 架构速查

```
scripts/update_data.py   采集脚本（akshare/requests 多源，schema_version 递增）
data/dashboard.json      产物（每小时 GitHub Actions 重新生成并提交，[skip ci]）
index.html               单文件前端（无构建，fetch dashboard.json 渲染）
legacy.html              旧博客页（Strata 模板 + 深色主题覆盖层）
.github/workflows/update-dashboard.yml  cron 整点 + push(scripts/**等) 触发
```

- 前端容错：JSON 字段缺失 → 显示"暂缺/—"；因此脚本侧**任何新源都必须 try/except 优雅降级**。
- schema_version 每次结构变更 +1（当前 7）。

## 第一步：数据源探测（必做，先于编码）

用一次性 python 脚本实测每个候选接口（sandbox 有代理墙，Actions 是海外直连，两者结果可能不同——
**双环境结论都要拿到才算数**；sandbox 被墙但 Actions 大概率通的源，允许带优雅降级上线）：

```python
import akshare as ak, requests, warnings
warnings.filterwarnings('ignore')
# akshare 接口：先 dir(ak) 按关键词找，再小样本调用确认列名/行数
# 自建 requests 源：带 UA；东财接口补 Referer: https://quote.eastmoney.com
```

已验证可用的源（2026-11）：
| 用途 | 源 | 备注 |
|---|---|---|
| A股/港股/美股指数日线+量能 | akshare 新浪系（stock_zh_index_daily / stock_hk_index_daily_sina / index_us_stock_sina） | 均含 volume |
| 商品日线+量能 | ak.futures_foreign_hist | volume 列全 0，量能需跳过 |
| VIX/VIX9D/VIX3M/VIX6M 实时+历史 | cdn.cboe.com/api/global/delayed_quotes/{quotes,charts/historical}/_VIX.json | CBOE 官方，稳定；期限结构替代不可达的 TACO |
| 美债10Y | **美财政部官方CSV** home.treasury.gov/.../daily-treasury-rates.csv/{年}/all（MM/DD/YYYY，"10 Yr"列，倒序） | 首选；FRED(fred.stlouisfed.org) 沙箱+Actions 双环境 ReadTimeout 勿依赖；中债 bond_zh_us_rate 与新浪 bond_gb_us_sina 会连续停更（date 滞后），仅作末级降级 |
| 恐惧贪婪 | api.alternative.me/fng/ | 稳定 |
| A股广度 | push2.eastmoney.com ulist.np get f104/f105/f106, secids=1.000001,0.399106 | 海外偶发 Push2RateLimitError，clist 全表降级也会限流 |
| 华尔街见闻快讯 | api-one-wscn.awtmt.com/apiv1/content/lives | JSON，title/content_text/display_time/uri |
| 谷歌新闻RSS | news.google.com/rss/search?q=…&hl=zh-CN | 沙箱墙外，Actions 可达 |
| ~~新浪财经~~ → 东财早报 | ak.stock_info_cjzc_em | stock_info_global_sina 已失效返回空；早报发布时间早会被时间排序沉底 |
| TACO | numerco.com | **数据中心 IP 被拒，双环境均不通，已改用 CBOE 期限结构** |
| 加密实时/历史 | OKX→Binance→CoinGecko→Bitstamp 四级 | tickers 端点返回全量，必须按目标本地过滤 |
| 中国波指 QVIX | ak.index_option_300index_qvix | 数据全 NaN，勿用 |
| Stooq | stooq.com/q/d/l | JS 防护页，勿用 |
| 金十数据 | flash-api.jin10.com | 沙箱被墙，未验证 Actions，勿依赖 |
| Yahoo chart | query1.finance.yahoo.com | 沙箱被拦；Actions 未验证，勿依赖 |

**坑位清单（历次踩过）**：
1. 自定义源函数必须给默认参数 `def _src(_kwargs=None)`——collect 用 `fn(**{})` 调用，
   必需参数会在 Actions 上炸 TypeError（akshare 函数自带默认参所以平时看不出来）。
2. 多源新闻聚合必须**按源轮询**取条目（每源最新条优先）——纯时间排序会让高频源霸屏、
   早报类（清晨发布）全部沉底被挤出。
3. GitHub schedule 高峰期会整点跳过（曾连续 5 小时全跳）：加错峰第二 cron "33 * * * *" 翻倍机会；
   push 事件偶发被吞，重推小改动即可恢复；workflow_dispatch 需要 workflow 权限的 token（默认 403）。
4. OKX `tickers` 忽略 instId 参数返回全市场 → 拉全量后按目标集合本地过滤。
5. 告警阈值用 20日σ 倍数（2σ警戒/3σ高危），别用拍脑袋的固定百分比。
6. 沙箱与 Actions 网络环境不同：东财 push2 沙箱常限流、Actions 偶发；谷歌新闻/numerco 沙箱墙外。
   每个源必须在目标环境（Actions 日志）确认，沙箱结论不算数。

## 第二步：脚本集成

1. 新增 `fetch_xxx()`：内部每个源独立 try/except + log("OK/FAIL …")，失败返回部分数据或抛给 safe()。
2. 行情类标的统一走 `_attach(item, closes, vols)` → `hist/sigma/_vols`；
   量能由 `_finalize_volumes` 收尾（volume/vol_chg_pct/vol_pctile/vol_hist），在 main 里对五个分组调用。
3. 指标类挂 `fetch_sentiment()`（sentiment.*）；新闻源加入 fetch_news 的 collect 列表并给 tag；
   人物/机构关键词加进 KEY_FIGURES（大国聚焦：美中欧日政要/央行/科技巨头）。
4. main() 的 payload：schema_version+1、新字段、disclaimer 更新；`errors` 记录每个模块状态。
5. `python3 -m py_compile` + 本地跑一轮，逐字段核对 JSON（含缺省路径：模拟某源失败）。

## 第三步：前端（index.html 单文件）

- 区块顺序：告警 → 风险与情绪仪表（auto-fit 卡片网格）→ 行情分组 → 关键人物 → 新闻+五行 → 折叠。
- 卡片风格遵循现有 tokens（--card/--gold/up红/down绿）；数字用 .num；指数红涨绿跌。
- 走势图：行内 sparkSVG(hist) + 展开大图 bigChartSVG(it)（渐变面积+基准虚线+高/低标注+量能柱）。
- 展开面板 meta：30日区间 / 20日σ / 今日 xσ 徽章 / 今日量·较5日均量·近1年分位 / 源·时间。
- 新指标卡缺省路径必须写（"—"+原因文案），如 TACO"本轮源不可达，暂缺"。
- 改完 `node --check` 提取的 <script>。

## 第四步：上线与验证

1. 提交脚本+前端，**不要提交本地生成的 dashboard.json**（沙箱缺加密等源）：
   `git checkout data/dashboard.json` 后再 add。
2. push 自动触发 Actions（约 2-4 分钟）。`gh run list --workflow update-dashboard.yml --limit 1` 等成功。
3. `git pull` 拉回 Actions 产物，核对新字段齐全。
4. GitHub Pages CDN 缓存 max-age=600：**HTML 与 JSON 都要等 ~10 分钟或用 ?v=bust 穿透**。
5. 线上验证用 browser agent：检查新卡渲染/缺省文案/无 console error；先 `curl ?t=$(date +%s)` 确认 JSON 已刷新再验页面。
6. Actions 日志排错：`gh run view <id> --log | grep FAIL`（FAIL 行含异常类型）。

## 输出契约

每轮迭代结束向用户报告：新数据源及双环境可用性、schema 版本、前端变更点、
线上验证结果（PASS 项 + 优雅缺省项及原因）、以及 commit hash。
