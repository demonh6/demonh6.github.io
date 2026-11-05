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
# 历史数据缺失时使用的长期日均波动率经验值（%）：
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


def _attach(item, closes):
    """给标的补充 hist（最近30日收盘序列）与 sigma（20日波动率%）字段。"""
    if closes:
        item["hist"] = [round(c, 4) for c in closes[-HIST_DAYS:]]
        s = _sigma20(closes)
        if s:
            item["sigma"] = s


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

def fetch_us10y():
    """美国 10 年期国债收益率（bond_zh_us_rate，取末两个有效值算日变动 bp）。"""
    import akshare as ak

    df = ak.bond_zh_us_rate()
    if df is None or df.empty:
        raise RuntimeError("美债收益率接口返回为空")
    valid = []
    for i in range(len(df) - 1, -1, -1):
        row = df.iloc[i]
        v = to_float(row.get("美国国债收益率10年"))
        if v is not None and v > 0:
            valid.append(row)
            if len(valid) >= 2:
                break
    if not valid:
        raise RuntimeError("美债收益率末行无有效值")
    last = valid[0]
    prev = valid[1] if len(valid) >= 2 else None
    value = to_float(last.get("美国国债收益率10年"))
    change_bp = None
    if prev is not None and value is not None:
        prev_v = to_float(prev.get("美国国债收益率10年"))
        if prev_v is not None:
            change_bp = round((value - prev_v) * 100, 1)
    return {
        "name": "美国10年期国债收益率",
        "sym": "US10Y",
        "value": value,
        "unit": "%",
        "change_bp": change_bp,
        "date": str(last.get("日期", "")),
    }


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
            _attach(it, _closes(df))
        time.sleep(0.3)

    for it in markets.get("hk") or []:
        df = job(f"港股 {it['name']}", lambda c=it["code"]: ak.stock_hk_index_daily_sina(symbol=c))
        if df is not None:
            _attach(it, _closes(df))
        time.sleep(0.3)

    for it in markets.get("us") or []:
        sym = US_HIST_SYMBOLS.get(it["name"])
        if sym:
            df = job(f"美股 {it['name']}", lambda s=sym: ak.index_us_stock_sina(symbol=s))
            if df is not None:
                _attach(it, _closes(df))
            time.sleep(0.3)

    for it in markets.get("global") or []:
        sname = GLOBAL_HIST_NAMES.get(it["name"])
        if sname:
            df = job(f"全球 {it['name']}", lambda s=sname: ak.index_global_hist_sina(symbol=s))
            if df is not None:
                _attach(it, _closes(df))
            time.sleep(0.3)

    for it in key_assets.get("commodities") or []:
        fsym = (it.get("code") or "").replace("hf_", "")
        if fsym:
            df = job(f"商品 {it['name']}", lambda s=fsym: ak.futures_foreign_hist(symbol=s))
            if df is not None:
                _attach(it, _closes(df))
            time.sleep(0.3)

    for it in key_assets.get("crypto") or []:
        code = it.get("code")
        closes = job(f"加密 {code}(OKX)", lambda c=code: _crypto_hist_okx(c))
        if not closes:
            closes = job(f"加密 {code}(Binance)", lambda c=code: _crypto_hist_binance(c))
        _attach(it, closes or [])
        time.sleep(0.3)

    bond = key_assets.get("bond")
    if bond:
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

# 具体传导路径（" → " 分隔，前端渲染为链式流程图）：
# 参考宏观对冲基金风险传导框架与风险平价/CTA/波动率策略的常见联动机制。
ALERT_CHAINS = {
    "a_share": "权重板块异动 → 两融与量化策略调仓 → 行业ETF资金申赎 → 中小盘流动性传导",
    "hk": "恒指系异动 → 股指期货/期权对冲盘增加 → 南向资金流向变化 → A/H溢价联动A股",
    "us": "美股估值重定价 → 全球股票ETF再平衡 → 亚太市场次日开盘联动 → 北向资金流向变化",
    "global": "欧/亚股指异动 → 跨市场风险偏好重定价 → 全球ETF资金再平衡 → 美股时段联动放大",
    "gold": "避险资金涌入黄金 → 实际利率与美元指数波动 → 贵金属板块与黄金股联动 → 通胀对冲需求上升",
    "silver": "白银急波动 → 贵金属与工业双重属性定价 → 金银比价波动 → 光伏/电子产业链成本传导",
    "oil": "油价急变 → 通胀预期重定价 → 债券收益率与商品货币波动 → 能源/航空/化工板块分化",
    "crypto": "币价急波动 → 高杠杆头寸连环清算 → 稳定币与链上流动性收缩 → 纳指等科技股风险偏好联动",
    "bond": "美债收益率急变 → 全球资产定价锚移动 → 成长股估值与美元流动性重定价 → 新兴市场资金流向逆转",
    "resonance_up": "多市场共振上涨 → 隐含波动率回落 → 杠杆与趋势策略加仓 → 回撤时同向抛售风险积聚",
    "resonance_down": "多市场共振下跌 → VIX隐含波动率飙升 → 风险平价/杠杆策略强制减仓 → 同向抛售螺旋与流动性挤兑",
    "hedge": "黄金与股指走势背离 → 资金在避险与风险资产间再平衡 → 防御/成长板块分化 → 行业轮动加速",
    "risk_on": "黄金回落与股指同强 → 资金从避险资产流出 → 风险偏好回升 → 成长/周期板块受益",
}


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
                         ALERT_CHAINS.get(group, ALERT_CHAINS["global"]))
        if a:
            alerts.append(a)

    for it in key_assets.get("commodities") or []:
        cname = it["name"]
        if "黄金" in cname:
            ck = "gold"
        elif "白银" in cname:
            ck = "silver"
        elif "原油" in cname:
            ck = "oil"
        else:
            ck = "oil"
        a = _sigma_alert(cname, it.get("change_pct"), it.get("sigma"), "commodity",
                         ALERT_CHAINS[ck])
        if a:
            alerts.append(a)

    for it in key_assets.get("crypto") or []:
        a = _sigma_alert(it["name"], it.get("change_pct"), it.get("sigma"), "crypto",
                         ALERT_CHAINS["crypto"])
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
            alerts.append(_mk_alert(
                level, bond["name"],
                f"{bond['name']}单日变动 {bond['change_bp']:+.1f}bp，"
                f"达20日历史波动率的 {mult:.1f} 倍（σ={s_bp:.1f}bp，突破 {line:.0f}σ {level_name}线{approx}）",
                ALERT_CHAINS["bond"]))

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


def fetch_news():
    """全球财经要闻：东财 + 新浪 + 富途 三源聚合，标题去重、时间倒序。

    消息面不依赖单一来源；任一源失败不影响其余源的聚合结果。
    """
    import akshare as ak

    errors = []
    pool = []
    for fn, kw, tag in (
        (ak.stock_info_global_em, {}, "东财"),
        (ak.stock_info_global_sina, {}, "新浪"),
        (ak.stock_info_global_futu, {}, "富途"),
    ):
        try:
            records = _df_to_records(fn(**kw))
            news = _norm_news(records)
            if news:
                for n in news:
                    n["source"] = tag
                pool.extend(news)
                log(f"OK   要闻-{tag}（{len(news)} 条）")
            else:
                errors.append(f"{fn.__name__}: 空数据")
        except Exception as e:  # noqa: BLE001 - 单源失败不聚合中断
            errors.append(f"{fn.__name__}: {type(e).__name__} {e}")
        time.sleep(1)

    if not pool:
        raise RuntimeError("；".join(errors) or "全部新闻源失败")
    return _dedup_news(pool)[:NEWS_LIMIT]


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
    news, errors["全球要闻"] = safe(fetch_news, "全球要闻")
    wuxing, errors["今日五行"] = safe(fetch_wuxing, "今日五行")

    markets = {
        "a_share": a_idx,
        "hk": hk_idx,
        "us": us_idx,
        "global": global_idx,
    }
    attach_histories(markets, key_assets)   # 补充30日走势与20日σ（告警基准）
    alerts = compute_alerts(markets, key_assets)

    now = datetime.now(CST)
    freq_note = {
        "hourly": "每小时更新一次（整点触发）；如需改为每日更新，调整 GitHub Actions cron 即可。",
    }.get(INTERVAL, "每日更新一次；如需改为每小时更新，调整 GitHub Actions cron 即可。")
    payload = {
        "schema_version": 5,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S CST"),
        "generated_at_iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "interval": INTERVAL,  # hourly | daily（由环境变量/调度控制）
        "update_frequency_note": freq_note,
        "markets": markets,
        "key_assets": key_assets,
        "alerts": alerts,
        "news": news,
        "wuxing": wuxing,
        "errors": {k: v for k, v in errors.items() if v},
        "disclaimer": "行情与新闻数据来自公开来源，仅供研究参考，不构成投资建议；接口可能因上游网站变化而失效。告警基于各标的20日历史波动率（σ）倍数（2σ警戒/3σ高危），传导路径为程序化生成的机制示意，不构成投资建议。",
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
