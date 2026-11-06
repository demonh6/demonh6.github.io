#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仪表盘数据更新脚本
==================
采集全球主要股市指数、关键标的（美债/贵金属/能源/加密货币）、全球要闻、
今日五行数据与波动告警，写入 data/dashboard.json，供 index.html 前端渲染。

更新频率由 GitHub Actions 的 cron 控制（当前每小时一次），本脚本本身与
频率无关——每次运行都拉取最新数据并在 JSON 中写入 generated_at / interval。

数据来源（均为公开接口，仅供研究参考，不构成投资建议）：
  - A股指数   akshare.stock_zh_index_spot_sina（新浪）
  - 港股指数   akshare.stock_hk_index_spot_sina（新浪）
  - 美股指数   新浪 hq 实时（gb_ 前缀，盘中实时）+ staticdata 历史降级
  - 全球指数   akshare.index_global_spot_em（东财）→ 新浪历史序列降级
  - 美债10Y   akshare.bond_zh_us_rate（中国债券信息网）
  - 贵金属/能源 新浪 hq 实时（hf_ 前缀：COMEX金/银、NYMEX原油、布伦特）
  - 加密货币   OKX → Binance → CoinGecko → Bitstamp 多级降级
  - 全球要闻   东财 + 新浪 + 富途 三源聚合去重
  - 今日五行   lunar-python（干支/纳音/宜忌）+ 本地五行生克题材推演
  - 波动告警   本地规则：单标的阈值 + 跨资产联动提示

任一数据源失败均不会导致脚本失败：对应模块记录 error，前端显示"暂缺"。
"""
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO_ROOT, "data", "dashboard.json")

CST = timezone(timedelta(hours=8))  # 北京时间

# GitHub Actions 可通过环境变量切换更新频率标识（daily / hourly）
INTERVAL = os.environ.get("DASHBOARD_INTERVAL", "daily")
NEWS_LIMIT = int(os.environ.get("DASHBOARD_NEWS_LIMIT", "24"))

SINA_HQ_URL = "https://hq.sinajs.cn/list={symbols}"
SINA_HQ_HEADERS = {"Referer": "https://finance.sina.com.cn"}
HTTP_TIMEOUT = 12

# 加密货币监控目标（OKX instId / Binance & Bitstamp 交易对 / CoinGecko id）
CRYPTO_TARGETS = (
    ("BTC", "比特币"),
    ("ETH", "以太坊"),
    ("SOL", "Solana"),
)

# ----------------------------------------------------------------------
# 告警阈值：基于 20 日历史波动率（σ）的倍数 —— 机构风控通用口径
# 单日波动 ≥2σ 视为警戒（历史分布约前 5% 分位），≥3σ 视为高危（约前 0.3% 分位），
# 取代拍脑袋的固定百分比阈值，使不同波动特性的标的（股指/商品/加密/利率）可比。
# ----------------------------------------------------------------------
ALERT_SIGMA = (2.0, 3.0)       # (warning, danger) σ 倍数
VOL_WINDOW = 20                # 波动率窗口（交易日）
HIST_DAYS = 30                 # 前端走势图展示天数
# 历史数据缺失时的长期日均波动率经验值（%，用于σ告警兜底）：
# 全球主要股指长期日 σ ≈1%~1.5%，商品 ≈1.5%~2%，主流加密 ≈3%~4%，美债10Y ≈5~8bp
FALLBACK_SIGMA = {"index": 1.2, "commodity": 1.8, "crypto": 3.5}
FALLBACK_SIGMA_BP = 6.0        # 美债收益率日变动 σ 经验值（bp）


def log(msg: str) -> None:
    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] {msg}", flush=True)


def to_float(v):
    try:
        f = float(v)
        return round(f, 4) if abs(f) < 1e12 else None
    except (TypeError, ValueError):
        return None


def pct(v):
    f = to_float(v)
    return round(f, 2) if f is not None else None


def _sym_a(code):
    """A股新浪代码 → 交易所标准代码：sh000001 → 000001.SH。"""
    code = (code or "").strip().lower()
    suf = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(code[:2])
    return f"{code[2:]}.{suf}" if suf and len(code) > 2 else code.upper()


def _sym_us(name):
    return US_HIST_SYMBOLS.get(name, name)


def _closes(df, col="close", n=None):
    """从日线 DataFrame 取最近 n 个有效收盘价（>0），时间正序。"""
    n = n or (HIST_DAYS + VOL_WINDOW + 2)
    vals = []
    for i in range(len(df) - 1, -1, -1):
        v = to_float(df.iloc[i].get(col))
        if v is not None and v > 0:
            vals.append(v)
            if len(vals) >= n:
                break
    vals.reverse()
    return vals


def _sigma20(closes, window=VOL_WINDOW):
    """最近 window 个交易日的日收益率标准差（%）；样本不足返回 None。"""
    if not closes or len(closes) < window + 1:
        return None
    rets = []
    for i in range(len(closes) - window, len(closes)):
        prev = closes[i - 1]
        if prev:
            rets.append((closes[i] - prev) / prev * 100)
    if len(rets) < window * 0.8:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return round(var ** 0.5, 3)


def _attach(item, closes, vols=None):
    """给标的补充 hist（最近30日收盘序列）与 sigma（20日波动率%）字段。

    vols 传入日线成交量序列（可选），暂存于 item["_vols"]，
    由 _finalize_volumes 统一计算量能统计并在落盘前剥离。
    """
    if closes:
        item["hist"] = [round(c, 4) for c in closes[-HIST_DAYS:]]
        s = _sigma20(closes)
        if s:
            item["sigma"] = s
    if vols:
        item["_vols"] = [v for v in vols[-260:] if v is not None]


def _vols_of(df, n=260):
    """从日线 DataFrame 取最近 n 个有效成交量（>=0），时间正序。"""
    if df is None or not len(df) or "volume" not in df.columns:
        return None
    vals = []
    for i in range(len(df) - 1, -1, -1):
        v = to_float(df.iloc[i].get("volume"))
        if v is None or v < 0:
            continue
        vals.append(v)
        if len(vals) >= n:
            break
    vals.reverse()
    return vals or None


def _finalize_volumes(items):
    """把 item["_vols"] 换算成量能统计：今日量 / 较5日均量 / 近1年分位 / 30日序列。"""
    import bisect

    for it in items:
        vols = (it or {}).pop("_vols", None)
        if not vols or len(vols) < 10:
            continue
        # 数据源整列无效（如商品期货接口 volume 全 0）时放弃量能统计
        if max(vols) <= 0:
            continue
        vol_today = vols[-1]
        prev5 = vols[-6:-1]
        base5 = sum(prev5) / len(prev5) if prev5 else None
        stats = {"volume": round(vol_today, 2)}
        if base5 and base5 > 0:
            stats["vol_chg_pct"] = round((vol_today / base5 - 1) * 100, 1)
        window = vols[-250:]
        if len(window) >= 30:
            sorted_w = sorted(window)
            rank = bisect.bisect_right(sorted_w, vol_today)
            stats["vol_pctile"] = round(rank / len(sorted_w) * 100, 1)
        stats["vol_hist"] = [round(v, 2) for v in vols[-30:]]
        it.update(stats)


# --------------------------------------------------------------------------
# 行情采集
# --------------------------------------------------------------------------

def _df_to_records(df):
    """DataFrame -> list[dict]，列名为键。"""
    return df.to_dict(orient="records") if df is not None and len(df) else []


def _sina_hq(symbols):
    """新浪 hq 实时行情：返回 {symbol: [字段...]}（GBK 解码）。"""
    r = requests.get(SINA_HQ_URL.format(symbols=",".join(symbols)),
                     headers=SINA_HQ_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    text = r.content.decode("gbk", errors="replace")
    out = {}
    for m in re.findall(r'hq_str_([A-Za-z0-9_$]+)="([^"]*)"', text):
        sym, payload = m
        if payload:
            out[sym] = payload.split(",")
    return out


def _last_valid_rows(df, n=2):
    """从末尾取最近 n 个 close>0 的有效行（跳过当日未更新的坏行）。"""
    rows = []
    for i in range(len(df) - 1, -1, -1):
        try:
            c = float(df.iloc[i].get("close"))
        except (TypeError, ValueError):
            continue
        if c > 0:
            rows.append(df.iloc[i])
            if len(rows) >= n:
                break
    return list(reversed(rows))


def fetch_a_indices():
    """A股主要指数（新浪源）。"""
    import akshare as ak

    targets = [
        ("sh000001", "上证指数"),
        ("sz399001", "深证成指"),
        ("sz399006", "创业板指"),
        ("sh000688", "科创50"),
        ("sh000300", "沪深300"),
        ("sh000016", "上证50"),
        ("sh000905", "中证500"),
    ]
    records = _df_to_records(ak.stock_zh_index_spot_sina())
    by_code = {str(r.get("代码", "")).strip(): r for r in records}
    result = []
    for code, name in targets:
        r = by_code.get(code)
        if not r:
            continue
        result.append({
            "name": name,
            "code": code,
            "sym": _sym_a(code),
            "price": to_float(r.get("最新价")),
            "change": to_float(r.get("涨跌额")),
            "change_pct": pct(r.get("涨跌幅")),
        })
    if not result:
        raise RuntimeError("新浪A股指数接口返回数据为空")
    return result


def fetch_hk_indices():
    """港股主要指数（新浪源，恒生系）。"""
    import akshare as ak

    records = _df_to_records(ak.stock_hk_index_spot_sina())
    wanted = ["恒生指数", "恒生科技指数", "恒生中国企业指数"]
    by_name = {}
    for r in records:
        name = str(r.get("名称", "")).strip()
        if name in wanted and name not in by_name:
            by_name[name] = {
                "name": name,
                "code": str(r.get("代码", "")).strip(),
                "sym": str(r.get("代码", "")).strip(),
                "price": to_float(r.get("最新价")),
                "change": to_float(r.get("涨跌额")),
                "change_pct": pct(r.get("涨跌幅")),
            }
    result = [by_name[n] for n in wanted if n in by_name]
    if not result:
        raise RuntimeError("新浪港股指数接口未返回恒生系指数")
    return result


def fetch_us_indices():
    """美股三大指数：新浪 hq 实时（gb_ 前缀，盘中实时刷新）为主，
    staticdata 历史序列为降级。

    gb_ 字段：0=名称 1=最新价 2=涨跌幅% 3=行情时间 4=涨跌额 ... 26=昨收。
    """
    import akshare as ak

    targets = [
        ("gb_dji", "道琼斯", ".DJI"),
        ("gb_ixic", "纳斯达克", ".IXIC"),
        ("gb_inx", "标普500", ".INX"),
    ]
    result = []
    # 1) 新浪实时
    try:
        hq = _sina_hq([t[0] for t in targets])
        for sym, name, _ in targets:
            f = hq.get(sym)
            if not f or len(f) < 3:
                continue
            result.append({
                "name": name,
                "code": sym,
                "sym": _sym_us(name),
                "price": to_float(f[1]),
                "change": to_float(f[4]) if len(f) > 4 else None,
                "change_pct": pct(f[2]),
                "quote_time": f[3] if len(f) > 3 else "",
            })
    except Exception as e:  # noqa: BLE001
        log(f"新浪美股实时不可用，降级历史序列: {type(e).__name__}")

    # 2) 历史序列降级（补齐缺失项）
    if len(result) < len(targets):
        got = {x["name"] for x in result}
        for sym, name, legacy in targets:
            if name in got:
                continue
            try:
                df = ak.index_us_stock_sina(symbol=legacy)
                valid = _last_valid_rows(df, 2)
                if not valid:
                    continue
                last, prev = valid[-1], (valid[-2] if len(valid) >= 2 else None)
                price = to_float(last.get("close"))
                change, change_pct = None, None
                if prev is not None and price is not None:
                    prev_close = to_float(prev.get("close"))
                    if prev_close:
                        change = round(price - prev_close, 2)
                        change_pct = round((price - prev_close) / prev_close * 100, 2)
                result.append({
                    "name": name, "code": legacy, "sym": _sym_us(name), "price": price,
                    "change": change, "change_pct": change_pct,
                    "quote_time": str(last.get("date", "")),
                })
            except Exception as e:  # noqa: BLE001
                log(f"美股历史降级 {name} 失败: {type(e).__name__}")
            time.sleep(0.5)

    if not result:
        raise RuntimeError("美股实时与历史源均不可用")
    order = {t[1]: i for i, t in enumerate(targets)}
    result.sort(key=lambda x: order.get(x["name"], 99))
    return result


def fetch_global_indices():
    """其他全球指数（亚太/欧洲）：东财实时快照优先，新浪历史序列降级。

    targets: (新浪名称, 展示名, 东财名称)；东财名称为 None 表示东财源无此指数。
    """
    import akshare as ak

    targets = [
        ("日经225指数", "日经225", "日经225"),
        ("首尔综合指数", "韩国KOSPI", "韩国KOSPI"),
        ("印度孟买SENSEX指数", "印度SENSEX", "印度SENSEX"),
        ("中国台湾加权指数", "台湾加权", None),
        ("德国DAX 30种股价指数", "德国DAX", "德国DAX"),
        ("英国富时100指数", "英国富时100", "英国富时100"),
        ("法CAC40指数", "法国CAC40", "法国CAC40"),
        ("欧洲Stoxx50指数", "欧洲斯托克50", "欧洲斯托克50"),
        ("俄罗斯MICEX指数", "俄罗斯MOEX", "俄罗斯MOEX"),
    ]

    def make(name, price, change, change_pct):
        return {"name": name, "sym": GLOBAL_SYMS.get(name, ""), "price": to_float(price),
                "change": to_float(change), "change_pct": pct(change_pct)}

    # 1) 东财实时快照（一次请求覆盖多个指数）
    try:
        df = ak.index_global_spot_em()
        by_name = {str(r.get("名称", "")).strip(): r for r in _df_to_records(df)}
        result = []
        for _, display, em_name in targets:
            if em_name and em_name in by_name:
                r = by_name[em_name]
                result.append(make(display, r.get("最新价"), r.get("涨跌额"), r.get("涨跌幅")))
        if result:
            order = {display: i for i, (_, display, _) in enumerate(targets)}
            result.sort(key=lambda x: order.get(x["name"], 99))
            return result
    except Exception as e:  # noqa: BLE001 - 降级到新浪源
        log(f"东财全球指数不可用，降级新浪历史序列: {type(e).__name__}")

    # 2) 新浪历史序列（逐个指数，末行为最新，末两行算涨跌幅）
    result = []
    for sina_name, display, _ in targets:
        try:
            df = ak.index_global_hist_sina(symbol=sina_name)
            if df is None or df.empty:
                continue
            valid = _last_valid_rows(df, 2)
            if not valid:
                continue
            last = valid[-1]
            prev = valid[-2] if len(valid) >= 2 else None
            price = to_float(last.get("close"))
            change, change_pct = None, None
            if prev is not None and price is not None:
                prev_close = to_float(prev.get("close"))
                if prev_close:
                    change = round(price - prev_close, 2)
                    change_pct = round((price - prev_close) / prev_close * 100, 2)
            result.append(make(display, price, change, change_pct))
        except Exception as e:  # noqa: BLE001 - 单个指数失败跳过
            log(f"新浪全球指数 {display} 失败: {type(e).__name__}")
        time.sleep(0.5)
    if not result:
        raise RuntimeError("东财与新浪全球指数源均不可用")
    order = {display: i for i, (_, display, _) in enumerate(targets)}
    result.sort(key=lambda x: order.get(x["name"], 99))
    return result


# --------------------------------------------------------------------------
# 关键标的采集（美债 / 贵金属 / 能源 / 加密货币）
# --------------------------------------------------------------------------

def _us10y_from_treasury():
    """美国财政部官方日频收益率曲线 CSV（home.treasury.gov），权威且对数据中心友好。"""
    r = requests.get("https://home.treasury.gov/resource-center/data-chart-center/"
                     "interest-rates/daily-treasury-rates.csv/2026/all",
                     params={"type": "daily_treasury_yield_curve",
                             "field_tdr_date_value": "2026", "page": "_format"},
                     headers=_UA_HEADERS, timeout=HTTP_TIMEOUT + 6)
    r.raise_for_status()
    import csv as _csv
    import io as _io
    from datetime import datetime as _dt
    pairs = []
    for row in _csv.DictReader(_io.StringIO(r.text)):
        try:
            d = _dt.strptime(row["Date"], "%m/%d/%Y").strftime("%Y-%m-%d")
            v = to_float(row.get("10 Yr"))
            if v and v > 0:
                pairs.append((d, v))
        except (KeyError, ValueError):
            continue
    pairs.sort()
    if len(pairs) < 2:
        raise ValueError("Treasury 有效行不足")
    (d1, v1), (d2, v2) = pairs[-1], pairs[-2]
    return {"value": v1, "change_bp": round((v1 - v2) * 100, 1),
            "date": d1, "source": "美财政部",
            "hist": [v for _, v in pairs[-HIST_DAYS:]]}


def _us10y_from_fred():
    """FRED（美联储圣路易斯分行）官方 DGS10 日频 CSV，最权威且 T+1 更新。"""
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": "DGS10", "cosd": "2026-01-01"},
                     headers=_UA_HEADERS, timeout=HTTP_TIMEOUT + 8)
    r.raise_for_status()
    rows = []
    for line in r.text.strip().split("\n")[1:]:
        parts = line.split(",")
        if len(parts) >= 2 and parts[1] not in (".", ""):
            rows.append((parts[0], to_float(parts[1])))
    rows = [(d, v) for d, v in rows if v and v > 0]
    if len(rows) < 2:
        raise ValueError("FRED 有效行不足")
    (d1, v1), (d2, v2) = rows[-1], rows[-2]
    return {"value": v1, "change_bp": round((v1 - v2) * 100, 1),
            "date": d1, "source": "FRED(美联储)",
            "hist": [v for _, v in rows[-HIST_DAYS:]]}


def _us10y_from_sina_daily():
    """新浪全球债券日线（bond_gb_us_sina）。"""
    import akshare as ak
    df = ak.bond_gb_us_sina()
    if df is None or df.empty:
        raise RuntimeError("新浪美债日线为空")
    valid = []
    for i in range(len(df) - 1, -1, -1):
        v = to_float(df.iloc[i].get("close"))
        if v is not None and v > 0:
            valid.append((str(df.iloc[i].get("date", ""))[:10], v))
            if len(valid) >= 2:
                break
    if not valid:
        raise RuntimeError("新浪美债日线无有效值")
    (d1, v1), (d2, v2) = valid[0], (valid[1] if len(valid) > 1 else (None, None))
    all_closes = [to_float(df.iloc[i].get("close")) for i in range(len(df))]
    return {"value": v1,
            "change_bp": round((v1 - v2) * 100, 1) if v2 else None,
            "date": d1, "source": "新浪债券",
            "hist": [v for v in all_closes if v and v > 0][-HIST_DAYS:]}


def fetch_us10y():
    """美国 10 年期国债收益率：美财政部 → FRED → 新浪日线 → 中债 四级降级。

    中债/新浪源曾连续停更（date 滞后至 11-04），优先美国官方数据。
    """
    errors = []
    for fn in (_us10y_from_treasury, _us10y_from_fred, _us10y_from_sina_daily):
        try:
            out = fn()
            log(f"OK   美债10Y({out['source']}) {out['value']}% @ {out['date']}")
            return _us10y_finalize(out)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {type(e).__name__}")
            log(f"降级 美债10Y {fn.__name__}: {type(e).__name__}")
    # 末级：中债（可能滞后但聊胜于无），保留原有逻辑
    import akshare as ak
    df = ak.bond_zh_us_rate()
    if df is None or df.empty:
        raise RuntimeError("；".join(errors) or "美债收益率接口返回为空")
    valid = []
    for i in range(len(df) - 1, -1, -1):
        row = df.iloc[i]
        v = to_float(row.get("美国国债收益率10年"))
        if v is not None and v > 0:
            valid.append(row)
            if len(valid) >= 2:
                break
    if not valid:
        raise RuntimeError("；".join(errors) or "美债收益率末行无有效值")
    last = valid[0]
    prev = valid[1] if len(valid) >= 2 else None
    value = to_float(last.get("美国国债收益率10年"))
    change_bp = None
    if prev is not None and value is not None:
        prev_v = to_float(prev.get("美国国债收益率10年"))
        if prev_v is not None:
            change_bp = round((value - prev_v) * 100, 1)
    log(f"OK   美债10Y(中债,可能滞后) {value}% @ {last.get('日期')}")
    return _us10y_finalize({
        "value": value, "change_bp": change_bp,
        "date": str(last.get("日期", "")), "source": "中债信息网",
        "hist": None,
    })


def _us10y_finalize(out):
    """统一美债返回结构，hist/sigma 由 attach_histories 补充或此处直填。"""
    item = {
        "name": "美国10年期国债收益率",
        "sym": "US10Y",
        "value": out["value"],
        "unit": "%",
        "change_bp": out.get("change_bp"),
        "date": out.get("date", ""),
        "source": out.get("source", ""),
    }
    if out.get("hist"):
        item["hist"] = [round(v, 4) for v in out["hist"][-HIST_DAYS:]]
        chg_bp = [(out["hist"][i] - out["hist"][i - 1]) * 100
                  for i in range(max(1, len(out["hist"]) - VOL_WINDOW), len(out["hist"]))]
        if len(chg_bp) >= VOL_WINDOW * 0.8:
            mean = sum(chg_bp) / len(chg_bp)
            var = sum((c - mean) ** 2 for c in chg_bp) / len(chg_bp)
            item["sigma_bp"] = round(var ** 0.5, 2)
    return item


def fetch_commodities():
    """贵金属与能源（新浪 hf_ 外盘实时，涨跌基准=昨结算价）。"""
    targets = [
        ("hf_GC", "COMEX黄金", "GC"),
        ("hf_SI", "COMEX白银", "SI"),
        ("hf_CL", "WTI原油", "CL"),
        ("hf_OIL", "布伦特原油", "BRN"),
    ]
    hq = _sina_hq([t[0] for t in targets])
    result = []
    for sym, name, disp in targets:
        f = hq.get(sym)
        # hf_ 字段：0=最新 2=买 3=卖 4=最高 5=最低 6=时间 7=昨结算 12=日期 13=中文名
        if not f or len(f) < 8:
            continue
        price = to_float(f[0])
        settle = to_float(f[7])
        if price is None or price <= 0:
            continue
        change, change_pct = None, None
        if settle and settle > 0:
            change = round(price - settle, 3)
            change_pct = round((price - settle) / settle * 100, 2)
        result.append({
            "name": name,
            "code": sym,
            "sym": disp,
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "quote_time": (f[6] or "") + " " + (f[12] or "") if len(f) > 12 else (f[6] or ""),
        })
    if not result:
        raise RuntimeError("新浪外盘实时未返回有效数据")
    return result


def _crypto_via_okx():
    # 注意：tickers 端点会忽略 instId 参数返回全部现货交易对，必须本地按目标过滤
    r = requests.get("https://www.okx.com/api/v5/market/tickers",
                     params={"instType": "SPOT"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data") or []
    by_inst = {d.get("instId"): d for d in data}
    out = []
    for code, name in CRYPTO_TARGETS:
        d = by_inst.get(f"{code}-USDT")
        if not d:
            continue
        last = to_float(d.get("last"))
        open24 = to_float(d.get("open24h"))
        if not last or not open24:
            continue
        out.append({"name": name, "code": code, "sym": code,
                    "price": last,
                    "change": round(last - open24, 2),
                    "change_pct": round((last - open24) / open24 * 100, 2)})
    if not out:
        raise RuntimeError("OKX 未返回目标币种")
    return out


def _crypto_via_binance():
    out = []
    for code, name in CRYPTO_TARGETS:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr",
                         params={"symbol": f"{code}USDT"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        last = to_float(d.get("lastPrice"))
        chg = pct(d.get("priceChangePercent"))
        if last and chg is not None:
            out.append({"name": name, "code": code, "sym": code, "price": last,
                        "change": to_float(d.get("priceChange")),
                        "change_pct": chg})
    return out


def _crypto_via_coingecko():
    r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                     params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd",
                             "include_24hr_change": "true"},
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    id_map = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
    out = []
    for code, name in CRYPTO_TARGETS:
        v = d.get(id_map[code], {})
        price = to_float(v.get("usd"))
        chg24 = pct(v.get("usd_24h_change"))
        if price and chg24 is not None:
            out.append({"name": name, "code": code, "sym": code, "price": price,
                        "change": round(price - price / (1 + chg24 / 100), 2),
                        "change_pct": chg24})
    return out


def _crypto_via_bitstamp():
    out = []
    for code, name in CRYPTO_TARGETS:
        r = requests.get(f"https://www.bitstamp.net/api/v2/ticker_hour/{code.lower()}usd/",
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        last = to_float(d.get("last"))
        open24 = to_float(d.get("open"))
        if last and open24:
            out.append({"name": name, "code": code, "sym": code, "price": last,
                        "change": round(last - open24, 2),
                        "change_pct": round((last - open24) / open24 * 100, 2)})
    return out


def fetch_crypto():
    """比特币/以太坊：OKX → Binance → CoinGecko → Bitstamp 多级降级。"""
    errors = []
    for fn in (_crypto_via_okx, _crypto_via_binance,
               _crypto_via_coingecko, _crypto_via_bitstamp):
        try:
            data = fn()
            if data:
                for item in data:
                    item["source"] = fn.__name__.replace("_crypto_via_", "")
                return data
            errors.append(f"{fn.__name__}: 空数据")
        except Exception as e:  # noqa: BLE001 - 单源失败降级
            errors.append(f"{fn.__name__}: {type(e).__name__}")
        time.sleep(0.5)
    raise RuntimeError("；".join(errors) or "全部加密源失败")


def fetch_key_assets():
    """关键标的聚合：美债10Y、贵金属、能源、加密货币（各自独立容错）。"""
    assets = {"bond": None, "commodities": [], "crypto": []}
    errors = {}

    try:
        assets["bond"] = fetch_us10y()
        log("OK   美债10Y")
    except Exception as e:  # noqa: BLE001
        errors["美债10Y"] = f"{type(e).__name__}: {e}"
        log(f"FAIL 美债10Y: {e}")

    try:
        assets["commodities"] = fetch_commodities()
        log("OK   贵金属/能源")
    except Exception as e:  # noqa: BLE001
        errors["贵金属能源"] = f"{type(e).__name__}: {e}"
        log(f"FAIL 贵金属/能源: {e}")

    try:
        assets["crypto"] = fetch_crypto()
        log("OK   加密货币")
    except Exception as e:  # noqa: BLE001
        errors["加密货币"] = f"{type(e).__name__}: {e}"
        log(f"FAIL 加密货币: {e}")

    return assets, errors


# --------------------------------------------------------------------------
# 历史序列与波动率（走势图数据 + σ 告警基准）
# --------------------------------------------------------------------------

# 全球指数：展示名 → 新浪历史接口名称
GLOBAL_HIST_NAMES = {
    "日经225": "日经225指数", "韩国KOSPI": "首尔综合指数", "印度SENSEX": "印度孟买SENSEX指数",
    "台湾加权": "中国台湾加权指数", "德国DAX": "德国DAX 30种股价指数", "英国富时100": "英国富时100指数",
    "法国CAC40": "法CAC40指数", "欧洲斯托克50": "欧洲Stoxx50指数", "俄罗斯MOEX": "俄罗斯MICEX指数",
}
US_HIST_SYMBOLS = {"道琼斯": ".DJI", "纳斯达克": ".IXIC", "标普500": ".INX"}

# 全球指数：展示名 → 国际通用代码（前端展示用）
GLOBAL_SYMS = {
    "日经225": "N225", "韩国KOSPI": "KS11", "印度SENSEX": "BSESN", "台湾加权": "TWII",
    "德国DAX": "DAX", "英国富时100": "FTSE", "法国CAC40": "CAC40",
    "欧洲斯托克50": "SX5E", "俄罗斯MOEX": "MOEX",
}


def _crypto_hist_okx(code):
    r = requests.get("https://www.okx.com/api/v5/market/candles",
                     params={"instId": f"{code}-USDT", "bar": "1D", "limit": "60"},
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data") or []          # 最新在前：[ts,o,h,l,c,...]
    closes = [to_float(row[4]) for row in reversed(data) if len(row) > 4]
    return [c for c in closes if c and c > 0]


def _crypto_hist_binance(code):
    r = requests.get("https://api.binance.com/api/v3/klines",
                     params={"symbol": f"{code}USDT", "interval": "1d", "limit": "60"},
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    closes = [to_float(row[4]) for row in r.json() if len(row) > 4]
    return [c for c in closes if c and c > 0]


def attach_histories(markets, key_assets):
    """为每个标的补充 hist（30日走势）与 sigma（20日波动率）；单标的失败仅跳过。"""
    import akshare as ak

    def job(label, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log(f"历史序列 {label} 失败: {type(e).__name__}")
            return None

    for it in markets.get("a_share") or []:
        df = job(f"A股 {it['name']}", lambda c=it["code"]: ak.stock_zh_index_daily(symbol=c))
        if df is not None:
            _attach(it, _closes(df), _vols_of(df))
        time.sleep(0.3)

    for it in markets.get("hk") or []:
        df = job(f"港股 {it['name']}", lambda c=it["code"]: ak.stock_hk_index_daily_sina(symbol=c))
        if df is not None:
            _attach(it, _closes(df), _vols_of(df))
        time.sleep(0.3)

    for it in markets.get("us") or []:
        sym = US_HIST_SYMBOLS.get(it["name"])
        if sym:
            df = job(f"美股 {it['name']}", lambda s=sym: ak.index_us_stock_sina(symbol=s))
            if df is not None:
                _attach(it, _closes(df), _vols_of(df))
            time.sleep(0.3)

    for it in markets.get("global") or []:
        sname = GLOBAL_HIST_NAMES.get(it["name"])
        if sname:
            df = job(f"全球 {it['name']}", lambda s=sname: ak.index_global_hist_sina(symbol=s))
            if df is not None:
                _attach(it, _closes(df), _vols_of(df))
            time.sleep(0.3)

    for it in key_assets.get("commodities") or []:
        fsym = (it.get("code") or "").replace("hf_", "")
        if fsym:
            df = job(f"商品 {it['name']}", lambda s=fsym: ak.futures_foreign_hist(symbol=s))
            if df is not None:
                _attach(it, _closes(df), _vols_of(df))
            time.sleep(0.3)

    for it in key_assets.get("crypto") or []:
        code = it.get("code")
        closes = job(f"加密 {code}(OKX)", lambda c=code: _crypto_hist_okx(c))
        if not closes:
            closes = job(f"加密 {code}(Binance)", lambda c=code: _crypto_hist_binance(c))
        _attach(it, closes or [])
        time.sleep(0.3)

    bond = key_assets.get("bond")
    if bond and not bond.get("hist"):
        # FRED/新浪主源已填 hist 时跳过（避免中债滞后数据覆盖）
        df = job("美债10Y", ak.bond_zh_us_rate)
        if df is not None:
            yields = _closes(df, col="美国国债收益率10年")
            if yields:
                bond["hist"] = [round(y, 4) for y in yields[-HIST_DAYS:]]
                chg_bp = [(yields[i] - yields[i - 1]) * 100
                          for i in range(max(1, len(yields) - VOL_WINDOW), len(yields))]
                if len(chg_bp) >= VOL_WINDOW * 0.8:
                    mean = sum(chg_bp) / len(chg_bp)
                    var = sum((c - mean) ** 2 for c in chg_bp) / len(chg_bp)
                    bond["sigma_bp"] = round(var ** 0.5, 2)


# --------------------------------------------------------------------------
# 波动告警
# --------------------------------------------------------------------------

# 具体传导路径：按资产与涨跌方向区分（" → " 分隔，前端渲染为链式流程图）。
# 每一步都写明"主体+动作+对象"，描述资金/机制的流动方向，而非抽象概念。
ALERT_CHAINS = {
    "a_share": {
        "up": "权重股(券商/银行)拉升指数 → 两融余额追高、杠杆资金进场 → 量化趋势策略跟随买入 → 行业ETF获净申购 → 中小盘跟涨，但获利盘随时可能兑现",
        "down": "权重股下杀击穿量化风控线 → 两融强平盘涌出放大跌幅 → 止损单连锁触发 → 行业ETF遭净赎回、被动卖出成分股 → 中小盘流动性挤兑，跌势自我强化",
    },
    "hk": {
        "up": "恒指急涨带动牛熊证发行商对冲买盘 → 空头被轧平进一步推升指数 → 南向资金加速流入 → AH溢价收窄，A股联动走强",
        "down": "恒指急跌使牛熊证批量打靶回收 → 发行商对冲抛售正股加深跌势 → 恒科杠杆产品强制减仓 → 南向资金转向观望 → AH溢价走阔，拖累A股情绪",
    },
    "us": {
        "up": "指数急涨 → 看跌期权对冲盘平仓压低VIX → CTA与波动率目标基金按模型机械加仓股票 → 追涨盘拥挤、仓位偏高 → 次日亚太市场高开跟随",
        "down": "指数急跌 → 期权对冲卖压放大跌幅 → 保证金追缴触发杠杆基金强制抛售 → 风险平价/CTA策略集体减仓 → 全球基金卖出亚洲持仓回补美元流动性 → 亚太市场次日低开",
    },
    "global": {
        "up": "欧/亚股指走强 → 全球股票基金获净申购 → 被动资金按权重买入成分股 → 跨市场风险偏好抬升 → 美股开盘联动放大涨幅",
        "down": "欧/亚股指走弱 → 全球股票基金遭净赎回 → 被动资金机械卖出 → 避险资金流向美债与美元 → 美股时段联动下挫",
    },
    "gold": {
        "up": "避险资金涌入黄金ETF与期货 → 金价上行压低实际利率预期 → 美元信用担忧升温 → 黄金股/贵金属板块联动走强 → 通胀对冲需求进一步放大涨幅",
        "down": "金价回落 → 避险盘获利了结 → 黄金ETF持仓下降形成持续抛压 → 黄金股承压 → 资金转出避险资产流向风险市场",
    },
    "silver": {
        "up": "白银急涨（贵金属避险+光伏工业双重买盘）→ 金银比价回落 → 工业采购成本抬升 → 光伏/电子产业链成本传导",
        "down": "白银急跌 → 金银比价走高 → 工业抛压与避险盘同时撤出 → 光伏/电子成本端短期改善",
    },
    "oil": {
        "up": "油价急涨 → 通胀预期抬升推高美债收益率 → 商品货币(加元/澳元)走强 → 航空/物流成本承压、能源股受益 → 央行紧缩预期升温压制成长股",
        "down": "油价急跌 → 通胀预期降温、债券收益率下行 → 能源股与产油国货币承压 → 航空/化工成本改善受益 → 通缩交易抬头",
    },
    "crypto": {
        "up": "币价急涨 → 空头杠杆被连环强平(轧空) → 强平买单进一步推高价格 → 稳定币加速流入加密市场 → 与纳指联动的高风险偏好扩散",
        "down": "币价急跌 → 多头杠杆连环爆仓 → 爆仓卖压加深跌势形成螺旋 → 稳定币脱锚担忧升温、链上流动性收缩 → 纳指科技股风险偏好被拖累",
    },
    "bond": {
        "up": "收益率上行 → 持有美债的银行/基金出现浮亏、被迫减持 → 贴现率抬升压制成长股估值(纳指首当其冲) → 美元走强抽离新兴市场流动性 → 北向资金流入A股放缓",
        "down": "收益率下行 → 债券价格上涨、资金从美元固收转向风险资产 → 贴现率下行支撑成长股估值 → 新兴市场流动性改善 → 北向资金回流A股/港股",
    },
    "resonance_up": "多市场共振上涨 → 隐含波动率下降，杠杆与趋势策略同步加仓 → 仓位高度同向拥挤 → 一旦逆转将触发集体减仓放大回撤——警惕追高与对冲成本抬升",
    "resonance_down": "多市场共振下跌 → VIX飙升 → 风险平价/波动率目标基金被模型强制减仓 → CTA翻空追加卖压 → 保证金追缴引发跨资产抛售螺旋 → 警惕流动性挤兑",
    "hedge": "黄金与股指走势背离 → 避险资金从股票流出转向黄金/美债 → 防御板块(公用事业/医药)与成长板块分化 → 行业轮动加速",
    "risk_on": "黄金回落与股指同强 → 避险盘撤出、资金回流股市 → 风险偏好回升 → 成长/周期板块受益",
}


def _chain_for(key, change):
    """按涨跌方向取传导链；change>0 取 up，<0 取 down，None 兜底 up。"""
    sub = ALERT_CHAINS.get(key)
    if isinstance(sub, dict):
        return sub.get("up" if (change or 0) >= 0 else "down")
    return sub


def _mk_alert(level, asset, message, chain=None):
    a = {"level": level, "asset": asset, "message": message,
         "time": datetime.now(CST).strftime("%Y-%m-%d %H:%M")}
    if chain:
        a["chain"] = chain
    return a


def _sigma_alert(name, change_pct, sigma, kind, chain):
    """σ 倍数告警：单日波动 ≥2σ 警戒、≥3σ 高危（σ=该标的20日历史波动率）。"""
    if change_pct is None:
        return None
    s = sigma or FALLBACK_SIGMA.get(kind)
    approx = "" if sigma else "，σ按长期均值估算"
    if not s or s <= 0:
        return None
    mult = abs(change_pct) / s
    w, d = ALERT_SIGMA
    if mult >= d:
        level, level_name = "danger", "高危"
    elif mult >= w:
        level, level_name = "warning", "警戒"
    else:
        return None
    line = d if level == "danger" else w
    return _mk_alert(
        level, name,
        f"{name}单日{'涨' if change_pct > 0 else '跌'}{abs(change_pct):.2f}%，"
        f"达20日历史波动率的 {mult:.1f} 倍（σ={s:.2f}%，突破 {line:.0f}σ {level_name}线{approx}）",
        chain)


def compute_alerts(markets, key_assets):
    """单标的 σ 倍数告警 + 跨资产联动提示，附具体传导路径。"""
    alerts = []

    index_items = []   # (group, item)
    for group in ("a_share", "hk", "us", "global"):
        for it in markets.get(group) or []:
            if it.get("change_pct") is not None:
                index_items.append((group, it))

    def mult_of(it, kind):
        s = it.get("sigma") or FALLBACK_SIGMA.get(kind) or 0
        return abs(it.get("change_pct") or 0) / s if s > 0 else 0

    for group, it in index_items:
        a = _sigma_alert(it["name"], it["change_pct"], it.get("sigma"), "index",
                         _chain_for(group, it.get("change_pct")))
        if a:
            alerts.append(a)

    for it in key_assets.get("commodities") or []:
        cname = it["name"]
        if "黄金" in cname:
            ck = "gold"
        elif "白银" in cname:
            ck = "silver"
        else:
            ck = "oil"
        a = _sigma_alert(cname, it.get("change_pct"), it.get("sigma"), "commodity",
                         _chain_for(ck, it.get("change_pct")))
        if a:
            alerts.append(a)

    for it in key_assets.get("crypto") or []:
        a = _sigma_alert(it["name"], it.get("change_pct"), it.get("sigma"), "crypto",
                         _chain_for("crypto", it.get("change_pct")))
        if a:
            alerts.append(a)

    bond = key_assets.get("bond")
    if bond and bond.get("change_bp") is not None:
        s_bp = bond.get("sigma_bp") or FALLBACK_SIGMA_BP
        approx = "" if bond.get("sigma_bp") else "，σ按长期均值估算"
        mult = abs(bond["change_bp"]) / s_bp if s_bp > 0 else 0
        w, d = ALERT_SIGMA
        if mult >= d:
            level, level_name = "danger", "高危"
        elif mult >= w:
            level, level_name = "warning", "警戒"
        else:
            level = None
        if level:
            line = d if level == "danger" else w
            direction = "收益率上行" if bond["change_bp"] > 0 else "收益率下行"
            alerts.append(_mk_alert(
                level, bond["name"],
                f"{bond['name']}单日变动 {bond['change_bp']:+.1f}bp（{direction}），"
                f"达20日历史波动率的 {mult:.1f} 倍（σ={s_bp:.1f}bp，突破 {line:.0f}σ {level_name}线{approx}）",
                _chain_for("bond", bond["change_bp"])))

    # 联动规则 1：≥3 个主要股指同向出现 ≥1.5σ 波动 → 共振提示
    up = [it["name"] for _, it in index_items
          if (it.get("change_pct") or 0) > 0 and mult_of(it, "index") >= 1.5]
    down = [it["name"] for _, it in index_items
            if (it.get("change_pct") or 0) < 0 and mult_of(it, "index") >= 1.5]
    if len(up) >= 3:
        alerts.append(_mk_alert("warning", "全球股指",
                                f"{'、'.join(up[:3])}等 {len(up)} 个主要股指单日齐涨且均超1.5倍σ，"
                                "风险偏好显著升温，注意追高风险。",
                                ALERT_CHAINS["resonance_up"]))
    if len(down) >= 3:
        alerts.append(_mk_alert("danger" if len(down) >= 4 else "warning", "全球股指",
                                f"{'、'.join(down[:3])}等 {len(down)} 个主要股指单日齐跌且均超1.5倍σ，"
                                "全球风险资产共振走弱，警惕连锁抛售。",
                                ALERT_CHAINS["resonance_down"]))

    # 联动规则 2：避险资产与风险资产背离 → 避险情绪提示
    gold = next((g for g in key_assets.get("commodities") or []
                 if "黄金" in g["name"] and g.get("change_pct") is not None), None)
    if gold:
        g_mult = mult_of(gold, "commodity")
        if gold["change_pct"] > 0 and g_mult >= 1.5 and down:
            alerts.append(_mk_alert("warning", "避险情绪",
                                    f"黄金单日上涨 {gold['change_pct']:.2f}%（{g_mult:.1f}倍σ），"
                                    f"同时{'、'.join(down[:2])}走跌，避险情绪与风险资产分化。",
                                    ALERT_CHAINS["hedge"]))
        elif gold["change_pct"] < 0 and g_mult >= 1.5 and up:
            alerts.append(_mk_alert("warning", "风险偏好",
                                    f"黄金单日下跌 {abs(gold['change_pct']):.2f}%（{g_mult:.1f}倍σ），"
                                    f"同时{'、'.join(up[:2])}走强，资金流向风险资产。",
                                    ALERT_CHAINS["risk_on"]))

    # 排序：danger 优先，再按时间
    alerts.sort(key=lambda a: 0 if a["level"] == "danger" else 1)
    return alerts


# --------------------------------------------------------------------------
# 要闻采集
# --------------------------------------------------------------------------

def _norm_news(records, title_key="标题", time_key="发布时间", link_key="链接",
               summary_key="摘要"):
    out = []
    for r in records:
        title = str(r.get(title_key, "") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "summary": str(r.get(summary_key, "") or "").strip(),
            "time": str(r.get(time_key, "") or "").strip(),
            "url": str(r.get(link_key, "") or "").strip(),
        })
    return out


def _dedup_news(items):
    """按标题去重（前 18 个字符为指纹），并按时间倒序。"""
    seen = set()
    out = []
    for n in items:
        key = re.sub(r"\s+", "", n["title"])[:18]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(n)
    out.sort(key=lambda x: x["time"], reverse=True)
    return out


KEY_FIGURES = [
    # 美国政要与央行
    ("特朗普", "特朗普", "白宫言论（含Truth Social）对全球风险资产冲击最大"),
    ("白宫", "白宫", "美国官方政策表态的权威渠道"),
    ("贝森特", "贝森特", "美国财长，关税与财政政策的关键传声筒"),
    ("美联储", "美联储", "FOMC决定与官员讲话是全球利率锚"),
    ("鲍威尔", "鲍威尔", "美联储主席，其讲话直接影响美债与美元"),
    ("欧洲央行", "欧洲央行", "欧央行政策影响欧元区与全球流动性"),
    ("拉加德", "拉加德", "欧央行行长"),
    ("日本央行", "日本央行", "日元利率走向影响全球套息交易"),
    # 中国政要、监管与部委
    ("中国央行", "中国央行", "货币政策工具（降准/MLF/LPR）决定A股流动性"),
    ("人民银行", "人民银行", "货币政策工具（降准/MLF/LPR）决定A股流动性"),
    ("证监会", "证监会", "A股核心监管机构的政策直接影响市场情绪"),
    ("国务院", "国务院", "国务院级经济政策影响宏观环境"),
    ("发改委", "发改委", "产业政策与价格调控影响行业"),
    ("商务部", "商务部", "中美贸易与出口管制的对口部门"),
    ("外交部", "外交部", "地缘政治表态影响风险偏好"),
    # 科技巨头与市场人物
    ("马斯克", "马斯克", "特斯拉/SpaceX/X发言影响纳指与加密情绪"),
    ("黄仁勋", "黄仁勋", "英伟达动向是全球AI行情的风向标"),
]
# 大国聚焦：发言人/机构命中后仅保留上述名单（自动过滤挪威央行等小经济体噪音）

NEWS_SOURCE_LIMIT = 30  # 每个新闻源的截取条数

# 情绪仪表配置：alternative.me 恐惧贪婪指数分档 → 中文描述
FEAR_GREED_BUCKETS = [
    (25, "极度恐惧", "#f04a3d"),
    (45, "恐惧", "#f58a3d"),
    (55, "中性", "#d9b23c"),
    (75, "贪婪", "#8ac96f"),
    (101, "极度贪婪", "#26a56b"),
]


def _fg_bucket(val):
    for upper, label, color in FEAR_GREED_BUCKETS:
        if val < upper:
            return label, color
    return "极度贪婪", "#26a56b"


# VIX 分档（CBOE 口径经验区间）
VIX_BUCKETS = [
    (15, "低位", "#26a56b"),
    (20, "正常", "#8ac96f"),
    (25, "升高", "#d9b23c"),
    (30, "高波动", "#f58a3d"),
    (10**9, "恐慌", "#f04a3d"),
]
_UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def _vix_bucket(val):
    for upper, label, color in VIX_BUCKETS:
        if val < upper:
            return label, color
    return "恐慌", "#f04a3d"


def _cboe_quote(ticker):
    """CBOE 官方实时报价：_VIX / _VIX3M。"""
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{ticker}.json"
    r = requests.get(url, headers=_UA_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    d = r.json()["data"]
    return {
        "value": to_float(d.get("current_price")),
        "change": to_float(d.get("price_change")),
        "change_pct": to_float(d.get("price_change_percent")),
    }


def _cboe_hist(ticker, n=30):
    """CBOE 官方历史日线（1990年至今），取最近 n 个收盘价。"""
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_{ticker}.json"
    r = requests.get(url, headers=_UA_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    rows = r.json()["data"]
    closes = [to_float(x.get("close")) for x in rows if x.get("close") is not None]
    return closes[-n:]


def fetch_sentiment():
    """情绪与风险仪表：恐惧贪婪 + A股广度 + VIX + 波动率期限结构。"""
    sentiment = {}

    # 1. 恐惧贪婪指数（alternative.me 第三方）
    try:
        r = requests.get("https://api.alternative.me/fng/",
                         params={"limit": 1, "format": "json"},
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()["data"][0]
        v = int(d["value"])
        label, color = _fg_bucket(v)
        sentiment["fear_greed"] = {
            "value": v, "classification": d["value_classification"],
            "label": label, "color": color,
            "timestamp": datetime.fromtimestamp(int(d["timestamp"]), CST).strftime("%Y-%m-%d"),
            "source": "alternative.me",
        }
        log(f"OK   恐惧贪婪指数 {v}({label})")
    except Exception as e:  # noqa: BLE001
        log(f"FAIL 恐惧贪婪指数: {type(e).__name__}")

    # 2. A股广度：push2 轻量接口（沪+深 涨跌家数），clist 全表降级
    try:
        r = requests.get("https://push2.eastmoney.com/api/qt/ulist.np/get",
                         params={"fltt": "2", "fields": "f12,f14,f104,f105,f106",
                                 "secids": "1.000001,0.399106"},
                         headers={**_UA_HEADERS, "Referer": "https://quote.eastmoney.com"},
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        diff = (r.json().get("data") or {}).get("diff") or []
        up = sum(int(x.get("f104") or 0) for x in diff)
        down = sum(int(x.get("f105") or 0) for x in diff)
        flat = sum(int(x.get("f106") or 0) for x in diff)
        if up + down + flat <= 0:
            raise ValueError("ulist 返回空广度")
        log("OK   A股广度(push2轻量)")
    except Exception as e:  # noqa: BLE001 - 降级到 clist 全表
        log(f"降级 A股广度(ulist不可达: {type(e).__name__})，尝试clist")
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            up = int((df["涨跌幅"] > 0).sum())
            down = int((df["涨跌幅"] < 0).sum())
            flat = int((df["涨跌幅"] == 0).sum())
            log("OK   A股广度(clist降级)")
        except Exception as e:  # noqa: BLE001
            up = down = flat = 0
            log(f"FAIL A股广度: {type(e).__name__}")
    total = up + down + flat
    if total:
        breadth = round(up / total * 100, 1)
        breadth_score = round(min(100, 50 + (breadth - 50) * 2), 1)
        sentiment["a_share_breadth"] = {
            "up": up, "down": down, "flat": flat, "total": total,
            "breadth_pct": breadth, "score": breadth_score,
            "source": "东财",
        }

    # 3. VIX（CBOE 官方）+ 期限结构 + 30日走势
    try:
        q = _cboe_quote("VIX")
        label, color = _vix_bucket(q["value"])
        vix = {
            "value": q["value"], "change": q["change"], "change_pct": q["change_pct"],
            "label": label, "color": color, "source": "CBOE",
        }
        try:
            vix["spark"] = _cboe_hist("VIX", 30)
        except Exception:  # noqa: BLE001
            pass
        try:
            q3 = _cboe_quote("VIX3M")
            vix["vix3m"] = q3["value"]
            vix["term"] = "backwardation(恐慌加剧)" if q["value"] > q3["value"] \
                else "contango(正常)" if q["value"] < q3["value"] * 0.97 else "持平"
        except Exception:  # noqa: BLE001
            pass
        sentiment["vix"] = vix
        log(f"OK   VIX {q['value']}({label})")
    except Exception as e:  # noqa: BLE001
        log(f"FAIL VIX: {type(e).__name__}")

    # 4. 波动率期限结构（CBOE 全家桶，替代不可达的 TACO）
    #    VIX9D > VIX > VIX3M → backwardation（恐慌升温）；VIX9D << VIX3M → contango（平静）
    try:
        term = []
        for t in ("VIX9D", "VIX", "VIX3M", "VIX6M"):
            try:
                q = _cboe_quote(t)
                if q["value"]:
                    term.append({"t": t, "value": q["value"],
                                 "change_pct": q["change_pct"]})
            except Exception:  # noqa: BLE001
                pass
        if len(term) >= 3:
            v9 = next((x["value"] for x in term if x["t"] == "VIX9D"), None)
            v3 = next((x["value"] for x in term if x["t"] == "VIX3M"), None)
            if v9 and v3:
                ratio = round(v9 / v3, 3)
                shape = ("backwardation(短期恐慌升温)" if v9 > v3 * 1.02
                         else "contango(平静期)" if v9 < v3 * 0.92 else "平坦")
            else:
                ratio, shape = None, ""
            sentiment["term_structure"] = {
                "points": term, "ratio_9d_3m": ratio, "shape": shape,
                "source": "CBOE",
            }
            log(f"OK   波动率期限结构 {shape or '数据不全'}")
    except Exception as e:  # noqa: BLE001
        log(f"FAIL 波动率期限结构: {type(e).__name__}")

    # 5. 综合情绪（恐惧贪婪 + A股广度分）
    scores = []
    if "fear_greed" in sentiment:
        scores.append(sentiment["fear_greed"]["value"])
    if "a_share_breadth" in sentiment:
        scores.append(sentiment["a_share_breadth"]["score"])
    if scores:
        avg = round(sum(scores) / len(scores), 1)
        sentiment["composite"] = {
            "score": avg, "label": _fg_bucket(int(avg))[0],
            "color": _fg_bucket(int(avg))[1],
        }
    return sentiment


def fetch_speakers(news_pool):
    """从新闻池中按关键词提取关键人物/机构发言。"""
    speakers = []
    for keyword, label, hint in KEY_FIGURES:
        matched = []
        for n in news_pool or []:
            blob = (n.get("title", "") + " " + n.get("summary", "")).lower()
            if keyword.lower() in blob:
                matched.append(n)
        if matched:
            matched.sort(key=lambda x: x.get("time", ""), reverse=True)
            top = matched[0]
            speakers.append({
                "keyword": keyword, "label": label, "hint": hint,
                "title": top["title"], "summary": top.get("summary", ""),
                "time": top.get("time", ""), "url": top.get("url", ""),
                "source": top.get("source", ""), "mentions": len(matched),
            })
    speakers.sort(key=lambda x: x.get("time", ""), reverse=True)
    return speakers


def _ths_with_retry(_kwargs=None):
    """同花顺全球财经，带一次重试（该源偶发 ReadTimeout）。"""
    import akshare as ak
    try:
        return ak.stock_info_global_ths()
    except Exception:  # noqa: BLE001
        time.sleep(3)
        return ak.stock_info_global_ths()


def _news_wscn(_kwargs=None):
    """华尔街见闻快讯（api-one-wscn.awtmt.com，公开JSON）。"""
    r = requests.get("https://api-one-wscn.awtmt.com/apiv1/content/lives",
                     params={"channel": "global-channel", "limit": NEWS_SOURCE_LIMIT},
                     headers=_UA_HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    items = (r.json().get("data") or {}).get("items") or []
    from datetime import datetime as _dt
    out = []
    for it in items:
        title = str(it.get("title") or it.get("content_text") or "").strip()
        if not title:
            continue
        ts = it.get("display_time")
        tm = _dt.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M") if ts else ""
        out.append({"title": title[:120], "summary": str(it.get("content_text") or "")[:200],
                    "time": tm, "url": str(it.get("uri") or "")})
    return out


def _news_gnews_trump(_kwargs=None):
    """谷歌新闻 RSS：大国政要/央行国际英文报道（hl=zh-CN 优先中文版面）。

    覆盖 X/Truth Social 类发言的二手权威报道；上游被墙时优雅失败。
    """
    r = requests.get("https://news.google.com/rss/search",
                     params={"q": '(特朗普 OR Trump OR 美联储 OR "Truth Social") when:2d',
                             "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
                     headers=_UA_HEADERS, timeout=HTTP_TIMEOUT + 6)
    r.raise_for_status()
    import xml.etree.ElementTree as ET
    root = ET.fromstring(r.content)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        tm = ""
        if pub:
            try:
                from email.utils import parsedate_to_datetime
                tm = parsedate_to_datetime(pub).astimezone(CST).strftime("%Y-%m-%d %H:%M")
            except Exception:  # noqa: BLE001
                pass
        # 谷歌新闻标题带" - 媒体名"后缀，保留（媒体名即来源标识）
        out.append({"title": title[:160], "summary": "", "time": tm, "url": link})
        if len(out) >= NEWS_SOURCE_LIMIT:
            break
    return out


# --------------------------------------------------------------------------
# 券商研报观点（微信点睛精读 + 东财研报速览）
# --------------------------------------------------------------------------

HEAD_BROKERS = ("中金", "中信", "国泰海通", "国泰君安", "华泰", "申万宏源",
                "高盛", "摩根", "摩根士丹利", "大摩", "瑞银", "广发", "海通")
WEIXIN_NOISE = re.compile(r"资料来源|图表|点击小程序|扫码|报告原文|免责声明|风险提示$|中金公司研究部|广告")


def _weixin_extract(url):
    """抓取并解析一篇微信公众号文章（中金点睛等），规则提炼关键结论。

    返回 {title, account, time, url, conclusions: [{section, text}]}。
    """
    UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 Mobile MicroMessenger/8.0.40",
          "Referer": "https://mp.weixin.qq.com/"}
    r = requests.get(url, headers=UA, timeout=HTTP_TIMEOUT + 8)
    r.raise_for_status()
    html = r.text
    tm = re.search(r"var createTime = ['\"]([^'\"]+)", html)
    pub_time = tm.group(1).strip()[:16] if tm else ""

    body_m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*)</div>\s*<script', html, re.S)
    if not body_m:
        raise ValueError("未找到微信正文（可能触发验证）")
    text = re.sub(r"<style.*?</style>|<script.*?</script>", "", body_m.group(1), flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.split("\n")]
    lines = [l for l in lines if l]

    # 拼句：碎片行（微信富文本被拆行）合并回完整句子，短行(<16字)视为小节标题
    sections = []          # [(section_title, sentence, ...) ]
    cur_section = "核心观点"
    buf = ""
    for line in lines:
        if WEIXIN_NOISE.search(line):
            buf = ""
            continue
        if len(line) <= 16 and len(line) >= 3 and not re.search(r'[。；，！？、]$', line) \
                and not re.search(r'[a-zA-Z]{2,}', line) and len(buf) < 4:
            if buf.strip():
                pass
            cur_section = line[:30]
            sections.append({"section": cur_section, "text": None})
            continue
        buf += line
        # 按句切分
        while True:
            m = re.search(r"^(.{24,}?[。！？])", buf)
            if not m:
                break
            sent = m.group(1).strip()
            buf = buf[m.end():]
            if WEIXIN_NOISE.search(sent) or len(sent) > 160:
                continue
            sections.append({"section": cur_section, "text": sent})
    if buf.strip() and len(buf.strip()) >= 24:
        sections.append({"section": cur_section, "text": buf.strip()[:160]})

    conclusions, seen = [], set()
    for s in sections:
        if not s["text"]:
            continue
        key = s["text"][:18]
        if key in seen:
            continue
        seen.add(key)
        conclusions.append(s)
        if len(conclusions) >= 12:
            break

    title_m = re.search(r'id="activity-name"[^>]*>(.*?)</h1>', html, re.S) or \
              re.search(r'<span class="js_title_inner">(.*?)</span>', html, re.S)
    acc_m = re.search(r'id="js_name"[^>]*>(.*?)</a>', html, re.S)
    title = re.sub(r"<[^>]+>", "", title_m.group(1)) if title_m else "微信研报精读"
    title = re.sub(r"\s+", " ", title).strip()[:80]
    account = re.sub(r"<[^>]+>", "", acc_m.group(1)).strip()[:20] if acc_m else ""
    if not conclusions:
        raise ValueError("未提炼到有效结论")
    return {"title": title[:80], "account": account, "time": pub_time,
            "url": url, "conclusions": conclusions}


def fetch_research():
    """研报观点：微信点睛精读（URL 由 data/research_urls.json 配置）+ 东财研报速览。"""
    import json as _json
    import os as _os

    out = {"digest": [], "reports": []}
    # 1. 微信精读：抓配置列表中的每一篇（最新在前），逐篇提炼，全部失败不阻塞
    urls_path = _os.path.join(REPO_ROOT, "data", "research_urls.json")
    urls = []
    if _os.path.exists(urls_path):
        try:
            cfg = _json.load(open(urls_path, encoding="utf-8"))
            urls = cfg if isinstance(cfg, list) else cfg.get("urls", [])
        except Exception as e:  # noqa: BLE001
            log(f"研报URL配置解析失败: {type(e).__name__}")
    for u in urls[:3]:
        try:
            art = _weixin_extract(str(u).strip())
            out["digest"].append(art)
            log(f"OK   微信精读《{art['title'][:24]}》结论{len(art['conclusions'])}条")
        except Exception as e:  # noqa: BLE001
            log(f"FAIL 微信精读 {str(u)[:50]}: {type(e).__name__}")
        time.sleep(1)

    # 2. 东财研报速览：头部机构最新研报，报告名称即浓缩结论
    try:
        import akshare as ak
        df = ak.stock_research_report_em()
        # 最新日期优先（同日内头部机构靠前）——时效性高于机构名气
        df["_head"] = df["机构"].apply(lambda o: any(h in o for h in HEAD_BROKERS))
        df = df.sort_values(["日期", "_head"], ascending=[False, False])
        picked, seen_titles = [], set()
        for _, row in df.iterrows():
            org = str(row.get("机构", ""))
            name = str(row.get("报告名称", "")).strip()
            if len(name) < 10 or name[:14] in seen_titles:
                continue
            seen_titles.add(name[:14])
            picked.append({
                "title": name[:80],
                "org": org[:12],
                "rating": str(row.get("东财评级", ""))[:6],
                "stock": str(row.get("股票简称", ""))[:12],
                "industry": str(row.get("行业", ""))[:10],
                "date": str(row.get("日期", ""))[:10],
            })
            if len(picked) >= 8:
                break
        out["reports"] = picked
        log(f"OK   东财研报速览 {len(picked)} 条")
    except Exception as e:  # noqa: BLE001
        log(f"FAIL 东财研报速览: {type(e).__name__}")

    if not out["digest"] and not out["reports"]:
        raise RuntimeError("研报观点全部源失败")
    return out


def fetch_news():
    """八源聚合：东财国际 + 新浪 + 富途 + 同花顺 + 财新 + 百度财经 + 华尔街见闻 + 谷歌新闻。"""
    import akshare as ak

    source_status = {}
    pool = []

    def collect(fn, kwargs, tag, norm_fn=_norm_news):
        try:
            res = fn(**kwargs)
            if isinstance(res, list):
                # 自定义源（华尔街见闻/谷歌新闻）已返回标准化列表
                news = res[:NEWS_SOURCE_LIMIT]
            else:
                news = norm_fn(_df_to_records(res))[:NEWS_SOURCE_LIMIT]
            if news:
                for n in news:
                    n["source"] = tag
                pool.extend(news)
                source_status[tag] = f"ok({len(news)})"
                log(f"OK   要闻-{tag}（{len(news)} 条）")
            else:
                source_status[tag] = "empty"
        except Exception as e:  # noqa: BLE001
            source_status[tag] = f"FAIL:{type(e).__name__}"
            log(f"FAIL 要闻-{tag}: {type(e).__name__}")

    collect(ak.stock_info_global_em, {}, "东财国际")
    collect(ak.stock_info_cjzc_em, {}, "东财早报")   # 替换失效的新浪全球财经
    collect(ak.stock_info_global_futu, {}, "富途")
    collect(_ths_with_retry, {}, "同花顺")
    collect(ak.stock_news_main_cx, {}, "财新",
            lambda rs: _norm_news(rs, title_key="summary", link_key="url"))
    collect(ak.news_economic_baidu, {}, "百度财经",
            lambda rs: _norm_news(rs, title_key="事件", summary_key="公布", time_key="日期"))
    collect(_news_wscn, {}, "华尔街见闻")
    collect(_news_gnews_trump, {}, "谷歌新闻·国际")
    time.sleep(0.3)

    deduped = _dedup_news(pool)
    if not deduped:
        raise RuntimeError("；".join(f"{k}:{v}" for k, v in source_status.items()) or "全部新闻源失败")

    # 按源轮询取条目（每源最新条优先），避免单一高频源霸屏、早报类沉底
    by_src = {}
    for n in deduped:
        by_src.setdefault(n.get("source", "?"), []).append(n)
    balanced = []
    round_i = 0
    while len(balanced) < NEWS_LIMIT * 2:
        added = False
        for items in by_src.values():
            if round_i < len(items):
                balanced.append(items[round_i])
                added = True
                if len(balanced) >= NEWS_LIMIT * 2:
                    break
        if not added:
            break
        round_i += 1

    source_counts = {}
    for n in balanced:
        source_counts[n.get("source", "?")] = source_counts.get(n.get("source", "?"), 0) + 1
    return balanced[:NEWS_LIMIT], {"sources": source_status, "counts": source_counts}


# --------------------------------------------------------------------------
# 今日五行
# --------------------------------------------------------------------------

GAN_WUXING = {
    "甲": "木", "乙": "木", "丙": "火", "丁": "火",
    "戊": "土", "己": "土", "庚": "金", "辛": "金",
    "壬": "水", "癸": "水",
}
ZHI_WUXING = {
    "子": "水", "丑": "土", "寅": "木", "卯": "木",
    "辰": "土", "巳": "火", "午": "火", "未": "土",
    "申": "金", "酉": "金", "戌": "土", "亥": "水",
}

# 五行 -> 对应行业题材（通俗口径，娱乐化推演用）
SECTOR_MAP = {
    "金": ["金融银行", "证券保险", "贵金属", "有色金属", "钢铁", "军工装备", "汽车机械"],
    "木": ["农林牧渔", "家居家具", "造纸包装", "中医药", "纺织服装", "教育文化"],
    "水": ["水务", "航运港口", "水产养殖", "物流贸易", "旅游酒店", "酒水饮料"],
    "火": ["电力", "光伏储能", "新能源锂电", "半导体电子", "化工", "传媒影视"],
    "土": ["房地产", "基建工程", "建材水泥", "煤炭矿产", "陶瓷", "粮食种植"],
}

# 五行相生：木生火、火生土、土生金、金生水、水生木
SHENG = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
# 五行相克：木克土、土克水、水克火、火克金、金克木
KE = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}


def _sectors(wuxing_list):
    """去重展开行业题材。"""
    seen, out = set(), []
    for wx in wuxing_list:
        for s in SECTOR_MAP.get(wx, []):
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def fetch_wuxing():
    """今日五行：干支/纳音/宜忌（lunar-python）+ 生克题材推演（本地规则）。"""
    from lunar_python import Solar

    now = datetime.now(CST)
    solar = Solar.fromYmd(now.year, now.month, now.day)
    lunar = solar.getLunar()

    day_gz = lunar.getDayInGanZhi()          # 如 壬午
    gan, zhi = day_gz[0], day_gz[1]
    gan_wx, zhi_wx = GAN_WUXING[gan], ZHI_WUXING[zhi]
    day_master = gan_wx                      # 日主 = 天干五行

    # 生克推演：
    #   利好 = 比助(同) + 印(生我) + 财(我克)
    #   利空 = 官杀(克我)；泄秀(我生) 列为中性观察
    favorable = _sectors([day_master, _rev_lookup(SHENG, day_master), KE[day_master]])
    unfavorable = _sectors([_rev_lookup(KE, day_master)])
    watch = _sectors([SHENG[day_master]])

    yi = lunar.getDayYi()
    ji = lunar.getDayJi()

    return {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": "一二三四五六日"[now.weekday()],
        "lunar_date": f"{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
        "year_gz": lunar.getYearInGanZhiByLiChun(),
        "day_gz": day_gz,
        "day_nayin": lunar.getDayNaYin(),
        "gan": {"char": gan, "wuxing": gan_wx},
        "zhi": {"char": zhi, "wuxing": zhi_wx},
        "day_master": day_master,
        "yi": yi[:8],
        "ji": ji[:8],
        "favorable_sectors": favorable,
        "unfavorable_sectors": unfavorable,
        "watch_sectors": watch,
        "color": {"木": "#3aa76d", "火": "#e0533d", "土": "#c28a3d",
                  "金": "#d9b23c", "水": "#3d7fc2"}.get(day_master, "#888"),
        "disclaimer": "五行题材为传统五行学说的娱乐化推演，仅供文化参考，不构成任何投资建议。",
    }


def _rev_lookup(mapping, value):
    """反向查找：谁生我 / 谁克我。"""
    for k, v in mapping.items():
        if v == value:
            return k
    return None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def safe(collect, label):
    try:
        data = collect()
        log(f"OK   {label}")
        return data, None
    except Exception as e:  # noqa: BLE001 - 单项失败不影响整体
        log(f"FAIL {label}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return None, f"{type(e).__name__}: {e}"


def main():
    log(f"开始更新 dashboard 数据（interval={INTERVAL}）")
    errors = {}

    a_idx, errors["A股指数"] = safe(fetch_a_indices, "A股指数")
    hk_idx, errors["港股指数"] = safe(fetch_hk_indices, "港股指数")
    us_idx, errors["美股指数"] = safe(fetch_us_indices, "美股指数")
    global_idx, errors["全球指数"] = safe(fetch_global_indices, "全球指数(亚太/欧洲)")
    key_assets, key_errors = fetch_key_assets()
    errors.update(key_errors)

    sentiment, errors["情绪仪表"] = safe(fetch_sentiment, "情绪仪表")

    news, news_err = safe(fetch_news, "全球要闻(6源聚合)")
    if news is None:
        errors["全球要闻"] = news_err
        news, news_meta = [], {}
    else:
        news, news_meta = news

    speakers, errors["关键人物"] = safe(lambda: fetch_speakers(news), "关键人物发言")

    research, errors["研报观点"] = safe(fetch_research, "研报观点")

    wuxing, errors["今日五行"] = safe(fetch_wuxing, "今日五行")

    markets = {
        "a_share": a_idx,
        "hk": hk_idx,
        "us": us_idx,
        "global": global_idx,
    }
    attach_histories(markets, key_assets)   # 补充30日走势与20日σ（告警基准）

    # 量能统计：今日量/较5日均量/近1年分位/30日序列
    for group in (markets.get("a_share"), markets.get("hk"), markets.get("us"),
                  markets.get("global"), key_assets.get("commodities")):
        _finalize_volumes(group or [])

    alerts = compute_alerts(markets, key_assets)

    now = datetime.now(CST)
    freq_note = {
        "hourly": "每小时更新一次（整点触发）；如需改为每日更新，调整 GitHub Actions cron 即可。",
    }.get(INTERVAL, "每日更新一次；如需改为每小时更新，调整 GitHub Actions cron 即可。")
    payload = {
        "schema_version": 10,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S CST"),
        "generated_at_iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "interval": INTERVAL,
        "update_frequency_note": freq_note,
        "markets": markets,
        "key_assets": key_assets,
        "sentiment": sentiment or {},
        "speakers": speakers or [],
        "research": research or {"digest": [], "reports": []},
        "alerts": alerts,
        "news": news,
        "news_meta": news_meta,
        "wuxing": wuxing,
        "errors": {k: v for k, v in errors.items() if v},
        "disclaimer": "行情与新闻数据来自公开来源（东财/富途/同花顺/财新/百度/华尔街见闻/谷歌新闻/CBOE/FRED/alternative.me），仅供研究参考，不构成投资建议。研报观点：微信精读由规则提炼原文关键句（版权归原公众号），券商速览来自东财研报标题；均不构成投资建议。美债10Y优先FRED官方日频数据（中债源滞后时自动降级）。传导路径按资产与涨跌方向生成，为程序化示意。",
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp_path = OUT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp_path, OUT_PATH)

    ok_modules = sum(1 for v in errors.values() if not v)
    log(f"完成：{ok_modules}/{len(errors)} 模块成功，告警 {len(alerts)} 条 -> {OUT_PATH}")
    # 数据部分失败（行情/新闻）时退出码 0 仍成立——页面可展示"暂缺"；
    # 仅当全部行情源都失败时返回非零，便于 Actions 报警。
    market_ok = any(v is None for v in
                    (errors["A股指数"], errors["美股指数"], errors["全球指数"]))
    sys.exit(0 if (market_ok and news is not None) else 1)


if __name__ == "__main__":
    main()
