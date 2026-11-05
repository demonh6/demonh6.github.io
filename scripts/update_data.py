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

# 波动告警阈值（单日涨跌幅 %）：(warning, danger)
ALERT_THRESHOLDS = {
    "index": (3.0, 5.0),
    "commodity": (2.5, 4.0),
    "crypto": (5.0, 8.0),
}
# 10 年期美债收益率单日变动告警阈值（基点 bp）
BOND_ALERT_BP = (8.0, 15.0)


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
                    "name": name, "code": legacy, "price": price,
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
        return {"name": name, "price": to_float(price), "change": to_float(change),
                "change_pct": pct(change_pct)}

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
        "value": value,
        "unit": "%",
        "change_bp": change_bp,
        "date": str(last.get("日期", "")),
    }


def fetch_commodities():
    """贵金属与能源（新浪 hf_ 外盘实时，涨跌基准=昨结算价）。"""
    targets = [
        ("hf_GC", "COMEX黄金", "commodity"),
        ("hf_SI", "COMEX白银", "commodity"),
        ("hf_CL", "WTI原油", "commodity"),
        ("hf_OIL", "布伦特原油", "commodity"),
    ]
    hq = _sina_hq([t[0] for t in targets])
    result = []
    for sym, name, _ in targets:
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
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "quote_time": (f[6] or "") + " " + (f[12] or "") if len(f) > 12 else (f[6] or ""),
        })
    if not result:
        raise RuntimeError("新浪外盘实时未返回有效数据")
    return result


def _crypto_via_okx():
    r = requests.get("https://www.okx.com/api/v5/market/tickers",
                     params={"instType": "SPOT", "instId": "BTC-USDT"},
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data") or []
    out = []
    for d in data:
        last = to_float(d.get("last"))
        open24 = to_float(d.get("open24h"))
        if not last or not open24:
            continue
        out.append({"name": d.get("instId", "").replace("-", ""),
                    "price": last,
                    "change": round(last - open24, 2),
                    "change_pct": round((last - open24) / open24 * 100, 2)})
    return out


def _crypto_via_binance():
    out = []
    for sym, name in (("BTCUSDT", "BTCUSDT"), ("ETHUSDT", "ETHUSDT")):
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr",
                         params={"symbol": sym}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        last = to_float(d.get("lastPrice"))
        chg = pct(d.get("priceChangePercent"))
        if last and chg is not None:
            out.append({"name": name, "price": last,
                        "change": to_float(d.get("priceChange")),
                        "change_pct": chg})
    return out


def _crypto_via_coingecko():
    r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                     params={"ids": "bitcoin,ethereum", "vs_currencies": "usd",
                             "include_24hr_change": "true"},
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    out = []
    for key, name in (("bitcoin", "BTCUSDT"), ("ethereum", "ETHUSDT")):
        v = d.get(key, {})
        price = to_float(v.get("usd"))
        chg24 = pct(v.get("usd_24h_change"))
        if price and chg24 is not None:
            out.append({"name": name, "price": price,
                        "change": round(price - price / (1 + chg24 / 100), 2),
                        "change_pct": chg24})
    return out


def _crypto_via_bitstamp():
    out = []
    for pair, name in (("btcusd", "BTCUSDT"), ("ethusd", "ETHUSDT")):
        r = requests.get(f"https://www.bitstamp.net/api/v2/ticker_hour/{pair}/",
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        last = to_float(d.get("last"))
        open24 = to_float(d.get("open"))
        if last and open24:
            out.append({"name": name, "price": last,
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
# 波动告警
# --------------------------------------------------------------------------

def _mk_alert(level, asset, message):
    return {"level": level, "asset": asset, "message": message,
            "time": datetime.now(CST).strftime("%Y-%m-%d %H:%M")}


def _threshold_alert(name, change_pct, kind, direction_hint):
    """单标的阈值告警：返回 alert 或 None。"""
    w, d = ALERT_THRESHOLDS[kind]
    v = abs(change_pct)
    if v >= d:
        level = "danger"
    elif v >= w:
        level = "warning"
    else:
        return None
    return _mk_alert(
        level, name,
        f"{name}单日{'涨' if change_pct > 0 else '跌'}{v:.2f}%，超过{level}阈值"
        f"（{d if level == 'danger' else w:.1f}%），{direction_hint}")


def compute_alerts(markets, key_assets):
    """单标的阈值告警 + 跨资产联动提示。"""
    alerts = []

    index_items = []
    for group, kind in (("a_share", "index"), ("hk", "index"), ("us", "index"),
                        ("global", "index")):
        for it in markets.get(group) or []:
            if it.get("change_pct") is not None:
                index_items.append(it)

    for it in index_items:
        a = _threshold_alert(it["name"], it["change_pct"], "index",
                             "需警惕资金连锁调仓与跨市场传导。")
        if a:
            alerts.append(a)

    for it in key_assets.get("commodities") or []:
        a = _threshold_alert(it["name"], it.get("change_pct"), "commodity",
                             "商品波动可能向相关板块与通胀预期传导。")
        if a:
            alerts.append(a)

    for it in key_assets.get("crypto") or []:
        a = _threshold_alert(it["name"], it.get("change_pct"), "crypto",
                             "加密资产高波动期常伴随风险偏好急变。")
        if a:
            alerts.append(a)

    bond = key_assets.get("bond")
    if bond and bond.get("change_bp") is not None:
        w, d = BOND_ALERT_BP
        v = abs(bond["change_bp"])
        if v >= d:
            level = "danger"
        elif v >= w:
            level = "warning"
        else:
            level = None
        if level:
            alerts.append(_mk_alert(
                level, bond["name"],
                f"{bond['name']}单日变动 {bond['change_bp']:+.1f}bp（阈值 {d:.0f}bp），"
                "美债利率剧烈波动将影响全球风险资产定价。"))

    # 联动规则 1：≥2 个主要股指同向波动超 2.5% → 共振提示
    up = [it["name"] for it in index_items if (it.get("change_pct") or 0) >= 2.5]
    down = [it["name"] for it in index_items if (it.get("change_pct") or 0) <= -2.5]
    if len(up) >= 2:
        alerts.append(_mk_alert("warning", "全球股指",
                                f"{'、'.join(up[:3])}等 {len(up)} 个主要股指单日齐涨超2.5%，"
                                "风险偏好显著升温，注意追高风险。"))
    if len(down) >= 2:
        alerts.append(_mk_alert("danger" if len(down) >= 3 else "warning", "全球股指",
                                f"{'、'.join(down[:3])}等 {len(down)} 个主要股指单日齐跌超2.5%，"
                                "全球风险资产共振走弱，警惕连锁抛售。"))

    # 联动规则 2：避险资产与风险资产同动 → 避险情绪提示
    gold = next((g for g in key_assets.get("commodities") or []
                 if "黄金" in g["name"] and g.get("change_pct") is not None), None)
    if gold:
        if gold["change_pct"] >= 1.5 and down:
            alerts.append(_mk_alert("warning", "避险情绪",
                                    f"黄金单日上涨 {gold['change_pct']:.2f}%，"
                                    f"同时{'、'.join(down[:2])}走跌，避险情绪与风险资产分化。"))
        elif gold["change_pct"] <= -1.5 and up:
            alerts.append(_mk_alert("warning", "风险偏好",
                                    f"黄金单日下跌 {abs(gold['change_pct']):.2f}%，"
                                    f"同时{'、'.join(up[:2])}走强，资金流向风险资产。"))

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
    alerts = compute_alerts(markets, key_assets)

    now = datetime.now(CST)
    freq_note = {
        "hourly": "每小时更新一次（整点触发）；如需改为每日更新，调整 GitHub Actions cron 即可。",
    }.get(INTERVAL, "每日更新一次；如需改为每小时更新，调整 GitHub Actions cron 即可。")
    payload = {
        "schema_version": 3,
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
        "disclaimer": "行情与新闻数据来自公开来源，仅供研究参考，不构成投资建议；接口可能因上游网站变化而失效。告警为程序化阈值提示，不构成投资建议。",
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
