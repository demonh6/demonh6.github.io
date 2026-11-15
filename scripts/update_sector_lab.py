#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行业估值分位 / 板块轮动 —— 日频全量数据生成器
==================================================

输出: data/sector_lab.json （首页「行业估值分位 · 板块轮动」模块的日频基准层）

更新层级（配合 .github/workflows/）：
  1) 日频全量（本脚本）：行业估值分位、分位轨迹、动量、盈利状态 —— 每个交易日
     收盘后跑一次（估值报表由数据源在收盘后发布），次日早晨再兜底跑一次。
  2) 小时级快照（scripts/update_sector_live.py）：只更新 ETF / 指数实时行情，
     不重算 10 年分位（分位本身是日频，盘中重算没有意义）。
  3) 浏览器端 JSONP：页面直连行情接口，盘中每 60 秒刷新，比定时文件更实时；
     接口不可达时自动降级到第 2 层的快照文件。

数据源（公开接口，无需密钥）：
  1. 估值：东方财富数据中心「行业估值明细」RPT_VALUEINDUSTRY_DET
     申万行业逐交易日 PE_TTM / PE_LAR / PB_MRQ / PS_TTM / PEG_CAR /
     PCF_OCF_TTM / NUM / LOSS_COUNT / TOTAL_MARKET_CAP，历史自 2017-01 起。
  2. 走势：腾讯行情 fqkline 周线（前复权），标的为对应行业 ETF + 沪深300ETF 基准。

口径说明（页面上同步标注）：
  * 分位 = 当前值在「近 10 年有效日频序列」中的百分位（<= 当前值的样本占比）。
    PE 剔除 <=0（亏损）与 >300（微利异常），PB 剔除 <=0 与 >30。
  * 历史某一时点的分位，一律用「该时点往前 10 年」的滚动窗口重算，
    不使用未来数据，因此分位轨迹 / 斜率不存在前视偏差。
  * 走势为 ETF 前复权周线收盘，前端按所选区间首个有效值归一化为 100；
    ETF 上市时间不同，早期缺失以 null 表示，不回填。
  * 「估值分位」来自申万行业成分整体，「走势/动量」来自可交易 ETF，
    是两套独立口径。基准为沪深300ETF(510300) 前复权周线。
  * 亏损家数占比、PEG 为数据源原始字段，仅作辅助判读，不参与分位计算。

用法：
  python scripts/update_sector_lab.py
  python scripts/update_sector_lab.py --out /path/to/sector_lab.json

依赖：仅 Python 标准库（可用于 GitHub Actions / 任意 CI）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "data", "sector_lab.json")

VALUATION_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PRICE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
BENCH_CODE = "sh510300"
BENCH_NAME = "沪深300ETF"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://data.eastmoney.com/",
}

# ---------------------------------------------------------------- 板块定义
# group 用于周期分类；board 为估值报表申万行业名；code 为走势 ETF。
# logic 是「怎么读这个板块的分位」；checks 是动态解读面板里的验证清单。
GROUPS = [
    {
        "id": "cyclical", "name": "周期资源", "color": "#d9b23c",
        "clock": ["复苏", "过热"],
        "desc": "盈利随商品价格与产能周期大幅波动",
        "logic": "PE 低而 PB 高，常是盈利处于周期峰值（分母被撑大）；PE 高甚至亏损而 PB 低，反而多出现在盈利谷底。周期股看 PE 容易读反，应以 PB / 商品价格位置为主。",
        "checks": ["对应商品价格与库存处在周期何处", "产能投放 / 限产与资本开支计划", "行业亏损面与 ROE 是否见底", "分红与长协价能否支撑低 PB"],
    },
    {
        "id": "financial", "name": "金融地产", "color": "#4f9cf9",
        "clock": ["复苏", "过热"],
        "desc": "信用扩张与利率敏感，杠杆经营",
        "logic": "银行、券商以 PB 为主锚：PE 受拨备、减值、自营收益扰动较大。低 PB 可能反映资产质量预期，需与不良率、息差一起看。",
        "checks": ["净息差 / 不良率与拨备覆盖变化", "社融、信贷与利率环境", "券商业绩对成交额与两融的弹性", "分红率与再融资摊薄"],
    },
    {
        "id": "growth", "name": "科技成长", "color": "#a77bf3",
        "clock": ["衰退", "复苏"],
        "desc": "估值由远期成长预期与流动性定价",
        "logic": "高 PE 往往对应高预期（高估位置），但高增速可消化估值；单看分位不够，需叠加盈利增速 / PEG。产业周期与流动性拐点比静态分位更关键。",
        "checks": ["订单 / 出货与盈利增速能否兑现预期", "PEG 与研发投入的转化效率", "行业亏损面（高 PE 是否由亏损造成）", "产业资本开支与库存周期位置"],
    },
    {
        "id": "defensive", "name": "消费防御", "color": "#43d69c",
        "clock": ["滞胀", "衰退"],
        "desc": "盈利相对稳定，需求与政策驱动",
        "logic": "盈利波动小，PE/PB 分位的可比性较强；双低多对应需求或政策压制，双高多对应抱团溢价。适合与自身历史中枢纵向比较。",
        "checks": ["终端需求与渠道库存", "提价能力 / 政策扰动", "现金流与分红稳定性", "与自身历史估值中枢的偏离幅度"],
    },
]

SECTORS = [
    dict(id="coal",       name="煤炭",     group="cyclical",  board="煤炭开采",   code="sh515220", proxy="煤炭ETF"),
    dict(id="oil",        name="石油石化", group="cyclical",  board="油气开采Ⅱ",  code="sh561360", proxy="石油ETF"),
    dict(id="nonferrous", name="有色金属", group="cyclical",  board="工业金属",   code="sh512400", proxy="有色金属ETF"),
    dict(id="steel",      name="钢铁",     group="cyclical",  board="普钢",       code="sh515210", proxy="钢铁ETF"),
    dict(id="bank",       name="银行",     group="financial", board="银行Ⅱ",      code="sh512800", proxy="银行ETF"),
    dict(id="broker",     name="证券",     group="financial", board="证券Ⅱ",      code="sh512880", proxy="证券ETF"),
    dict(id="semi",       name="半导体",   group="growth",    board="半导体",     code="sh512480", proxy="半导体ETF"),
    dict(id="telecom",    name="通信设备", group="growth",    board="通信设备",   code="sh515880", proxy="通信ETF"),
    dict(id="software",   name="科技",     group="growth",    board="软件开发",   code="sh515000", proxy="科技ETF"),
    dict(id="liquor",     name="白酒",     group="defensive", board="白酒Ⅱ",      code="sh512690", proxy="酒ETF"),
    dict(id="pharma",     name="医药",     group="defensive", board="化学制药",   code="sh512010", proxy="医药ETF"),
    dict(id="power",      name="电力",     group="defensive", board="电力",       code="sh561560", proxy="电力ETF"),
]

LOOKBACK_YEARS = 10
PE_MIN, PE_MAX = 0.0, 300.0
PB_MIN, PB_MAX = 0.0, 30.0
VAL_COLUMNS = ("BOARD_NAME,BOARD_CODE,TRADE_DATE,PE_TTM,PE_LAR,PB_MRQ,PS_TTM,PCF_OCF_TTM,"
               "PEG_CAR,TOTAL_MARKET_CAP,NUM,LOSS_COUNT")


# ---------------------------------------------------------------- 网络工具
def http_json(url: str, params: dict, retries: int = 3):
    full = url + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError("请求失败 %s : %s" % (full[:110], last))


def log(msg: str):
    print("[sector-lab] " + msg, flush=True)


def _num(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _cutoff(as_of: str, years: int = LOOKBACK_YEARS) -> str:
    return (datetime.strptime(as_of, "%Y-%m-%d") - timedelta(days=365 * years)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 估值分位
def fetch_valuation(board: str):
    """取某申万行业全历史日频估值序列（按日期升序）。"""
    rows, page, pages = [], 1, 1
    while page <= pages and page <= 20:
        data = http_json(VALUATION_API, {
            "reportName": "RPT_VALUEINDUSTRY_DET",
            "columns": VAL_COLUMNS,
            "filter": '(BOARD_NAME="%s")' % board,
            "pageNumber": str(page),
            "pageSize": "500",
            "sortColumns": "TRADE_DATE",
            "sortTypes": "1",
        })
        result = data.get("result") or {}
        batch = result.get("data") or []
        rows.extend(batch)
        pages = int(result.get("pages") or 1)
        if not batch:
            break
        page += 1
    seen, clean = set(), []
    keys = ("pe", "PE_TTM"), ("pe_lar", "PE_LAR"), ("pb", "PB_MRQ"), ("ps", "PS_TTM"), \
           ("pcf", "PCF_OCF_TTM"), ("peg", "PEG_CAR"), ("cap", "TOTAL_MARKET_CAP"), \
           ("num", "NUM"), ("loss", "LOSS_COUNT")
    for r in rows:
        d = (r.get("TRADE_DATE") or "")[:10]
        if not d or d in seen:
            continue
        seen.add(d)
        item = {"date": d}
        for short, src in keys:
            item[short] = _num(r.get(src))
        clean.append(item)
    clean.sort(key=lambda x: x["date"])
    return clean


def percentile_of(series, current, lo, hi):
    """当前值在有效历史序列中的百分位。"""
    if current is None or not (lo < current <= hi):
        return None
    vals = [v for v in series if v is not None and lo < v <= hi]
    if len(vals) < 60:
        return None
    below = sum(1 for v in vals if v <= current)
    return round(100.0 * below / len(vals), 1)


def pct_at(rows, idx, key, lo, hi):
    """滚动窗口分位：只用 idx 及之前的数据，避免前视偏差。"""
    cur = rows[idx].get(key)
    cutoff = _cutoff(rows[idx]["date"])
    vals = [r.get(key) for r in rows[:idx + 1] if r["date"] >= cutoff]
    return percentile_of(vals, cur, lo, hi)


def stat(vals, lo, hi):
    v = sorted(x for x in vals if x is not None and lo < x <= hi)
    if not v:
        return None
    return {"min": round(v[0], 2), "median": round(v[len(v) // 2], 2), "max": round(v[-1], 2)}


def build_valuation(rows):
    if not rows:
        return None
    n = len(rows)
    as_of = rows[-1]["date"]
    window = [r for r in rows if r["date"] >= _cutoff(as_of)]
    pe_series = [r["pe"] for r in window]
    pb_series = [r["pb"] for r in window]

    cur_pe, cur_pb = rows[-1]["pe"], rows[-1]["pb"]
    pe_pct = percentile_of(pe_series, cur_pe, PE_MIN, PE_MAX)
    pb_pct = percentile_of(pb_series, cur_pb, PB_MIN, PB_MAX)

    # 分位斜率：20 / 60 / 250 个交易日前的同口径滚动分位
    def ago(k, key, lo, hi):
        i = n - 1 - k
        return pct_at(rows, i, key, lo, hi) if i >= 0 else None

    # 近 1 年 PB 分位轨迹（每 10 个交易日采样一次，供 sparkline）
    trail = []
    start = max(0, n - 250)
    for i in range(start, n, 10):
        p = pct_at(rows, i, "pb", PB_MIN, PB_MAX)
        trail.append([rows[i]["date"], p])
    if trail and trail[-1][0] != as_of:
        trail.append([as_of, pb_pct])

    last = rows[-1]
    peg = last.get("peg")
    pe_lar = last.get("pe_lar")
    num, loss = last.get("num"), last.get("loss")
    cap = last.get("cap")

    return {
        "as_of": as_of,
        "window_start": window[0]["date"],
        "samples": len(window),
        "pe": round(cur_pe, 2) if cur_pe else None,
        "pb": round(cur_pb, 2) if cur_pb else None,
        "ps": round(last["ps"], 2) if last.get("ps") else None,
        "pcf": round(last["pcf"], 2) if last.get("pcf") else None,
        "peg": round(peg, 2) if peg else None,
        "pe_lar": round(pe_lar, 2) if pe_lar else None,
        "earnings_trend": round((cur_pe / pe_lar - 1) * 100, 1) if cur_pe and pe_lar else None,
        "members": int(num) if num else None,
        "loss_count": int(loss) if loss is not None else None,
        "loss_ratio": round(loss / num * 100, 1) if num and loss is not None else None,
        "cap_yi": round(cap / 1e8, 0) if cap else None,
        "pe_pct": pe_pct,
        "pb_pct": pb_pct,
        "pe_pct_20d": ago(20, "pe", PE_MIN, PE_MAX),
        "pb_pct_20d": ago(20, "pb", PB_MIN, PB_MAX),
        "pe_pct_60d": ago(60, "pe", PE_MIN, PE_MAX),
        "pb_pct_60d": ago(60, "pb", PB_MIN, PB_MAX),
        "pe_pct_1y": ago(250, "pe", PE_MIN, PE_MAX),
        "pb_pct_1y": ago(250, "pb", PB_MIN, PB_MAX),
        "pb_trail": trail,
        "pe_stat": stat(pe_series, PE_MIN, PE_MAX),
        "pb_stat": stat(pb_series, PB_MIN, PB_MAX),
    }


# ---------------------------------------------------------------- 板块走势
def fetch_weekly(code: str):
    data = http_json(PRICE_API, {"param": "%s,week,,,600,qfq" % code})
    node = (data.get("data") or {}).get(code) or {}
    bars = node.get("qfqweek") or node.get("week") or []
    out = {}
    for b in bars:
        if len(b) >= 3:
            v = _num(b[2])  # [date, open, close, high, low, ...]
            if v and v > 0:
                out[b[0][:10]] = round(v, 4)
    return out


def ret_of(series: dict, weeks: int):
    """按有效周收盘算区间涨跌幅(%)，不足窗口返回 None。"""
    vals = [series[d] for d in sorted(series) if series[d]]
    if len(vals) <= weeks or not vals[-1 - weeks]:
        return None
    return round((vals[-1] / vals[-1 - weeks] - 1) * 100, 1)


def build_price_matrix(sectors):
    per = {}
    for s in sectors:
        try:
            per[s["id"]] = fetch_weekly(s["code"])
            log("走势 %-6s %s  %d 周" % (s["name"], s["code"], len(per[s["id"]])))
        except Exception as exc:  # noqa: BLE001
            per[s["id"]] = {}
            log("走势 %-6s 失败: %s" % (s["name"], str(exc)[:90]))
    try:
        bench_series = fetch_weekly(BENCH_CODE)
        log("基准 %s %d 周" % (BENCH_CODE, len(bench_series)))
    except Exception as exc:  # noqa: BLE001
        bench_series = {}
        log("基准失败: %s" % str(exc)[:90])

    dates = sorted({d for series in list(per.values()) + [bench_series] for d in series})
    if dates:
        dates = [d for d in dates if d >= _cutoff(dates[-1], 12)]
    matrix = {sid: [series.get(d) for d in dates] for sid, series in per.items()}
    return {
        "dates": dates,
        "series": matrix,
        "benchmark": {"name": BENCH_NAME, "code": BENCH_CODE,
                      "series": [bench_series.get(d) for d in dates]},
        "per": per,
        "bench_series": bench_series,
    }


def build_momentum(pm, sectors):
    """给每个板块附加多周期收益与相对沪深300的超额收益。"""
    bench = pm["bench_series"]
    b = {k: ret_of(bench, w) for k, w in
         (("ret_1m", 4), ("ret_3m", 13), ("ret_6m", 26), ("ret_1y", 52), ("ret_3y", 156))}
    for s in sectors:
        series = pm["per"].get(s["id"], {})
        for k, w in (("ret_1m", 4), ("ret_3m", 13), ("ret_6m", 26), ("ret_1y", 52), ("ret_3y", 156)):
            s[k] = ret_of(series, w)
        s["rs_1m"] = round(s["ret_1m"] - b["ret_1m"], 1) if s["ret_1m"] is not None and b["ret_1m"] is not None else None
        s["rs_3m"] = round(s["ret_3m"] - b["ret_3m"], 1) if s["ret_3m"] is not None and b["ret_3m"] is not None else None
        s["rs_1y"] = round(s["ret_1y"] - b["ret_1y"], 1) if s["ret_1y"] is not None and b["ret_1y"] is not None else None
        s["pe_pb_gap"] = round(s["pb_pct"] - s["pe_pct"], 1) if s.get("pb_pct") is not None and s.get("pe_pct") is not None else None
    return {k: v for k, v in b.items()}


def build_market(sectors):
    pb = sorted(s["pb_pct"] for s in sectors if s.get("pb_pct") is not None)
    pe = sorted(s["pe_pct"] for s in sectors if s.get("pe_pct") is not None)
    old = sorted(s["pb_pct_1y"] for s in sectors if s.get("pb_pct_1y") is not None)
    if not pb:
        return {}
    gaps = [s for s in sectors if s.get("pe_pb_gap") is not None]
    gaps.sort(key=lambda s: abs(s["pe_pb_gap"]), reverse=True)
    return {
        "temp": round(pb[len(pb) // 2], 1),
        "temp_1y": round(old[len(old) // 2], 1) if old else None,
        "median_pe_pct": round(pe[len(pe) // 2], 1) if pe else None,
        "low_count": sum(1 for x in pb if x < 30),
        "high_count": sum(1 for x in pb if x > 70),
        "mid_count": sum(1 for x in pb if 30 <= x <= 70),
        "max_gap": {"id": gaps[0]["id"], "name": gaps[0]["name"], "gap": gaps[0]["pe_pb_gap"]} if gaps else None,
    }


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="生成行业估值分位与板块轮动数据（日频全量）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON 路径")
    args = ap.parse_args()

    sectors = []
    for s in SECTORS:
        try:
            rows = fetch_valuation(s["board"])
            val = build_valuation(rows)
            if not val:
                raise RuntimeError("估值序列为空")
            item = dict(s)
            item.update(val)
            sectors.append(item)
            log("估值 %-6s %-8s PE=%-7s 分位=%-6s PB=%-6s 分位=%-6s 亏损=%-5s (%d 日)" % (
                s["name"], s["board"], val["pe"], val["pe_pct"], val["pb"], val["pb_pct"],
                val["loss_ratio"], val["samples"]))
        except Exception as exc:  # noqa: BLE001
            log("估值 %-6s 失败: %s" % (s["name"], str(exc)[:110]))
            sectors.append(dict(s, pe_pct=None, pb_pct=None, pe=None, pb=None, samples=0))

    pm = build_price_matrix(SECTORS)
    bench_ret = build_momentum(pm, sectors)
    market = build_market(sectors)
    price = {"dates": pm["dates"], "series": pm["series"], "benchmark": pm["benchmark"]}

    dates = price["dates"]
    as_of = max((s.get("as_of") or "" for s in sectors), default="") or (dates[-1] if dates else "")
    now = datetime.now(timezone(timedelta(hours=8)))

    payload = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M") + " (北京时间)",
        "as_of": as_of,
        "price_as_of": dates[-1] if dates else "",
        "lookback_years": LOOKBACK_YEARS,
        "valuation_source": "东方财富数据中心 · 行业估值明细（申万行业，日频 PE_TTM / PB_MRQ / PEG / 亏损家数）",
        "price_source": "腾讯行情 · 行业 ETF 与 沪深300ETF(510300) 前复权周线收盘",
        "quote_source": "东方财富行情接口（浏览器 JSONP 直连，盘中每 60 秒刷新；失败时回退小时级快照）",
        "method": "分位 = 当前值在近10年有效日频序列中的百分位；PE 剔除<=0及>300，PB 剔除<=0及>30；历史分位用滚动窗口重算，无前视偏差。走势按所选区间首值归一化为100；超额收益相对沪深300ETF。",
        "refresh": {
            "valuation": "每个交易日收盘后 + 次日早晨各一次（数据源为日频，盘中不会变化）",
            "quote": "浏览器盘中实时（60秒轮询）；GitHub Actions 交易时段每小时生成兜底快照",
        },
        "disclaimer": "历史统计口径的客观展示，仅供研究参考，不构成投资建议；低估不等于上涨，高估不等于下跌。",
        "groups": GROUPS,
        "sectors": sectors,
        "price": price,
        "benchmark_ret": bench_ret,
        "market": market,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    log("完成 -> %s (%d 板块, %d 个周线日期, %.1f KB, 估值温度 %s)" % (
        args.out, len(sectors), len(dates), os.path.getsize(args.out) / 1024.0, market.get("temp")))


if __name__ == "__main__":
    main()
