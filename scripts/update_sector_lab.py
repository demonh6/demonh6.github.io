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

# ---------------------------------------------------------------- 细分行业全量
# 东财 RPT_VALUEINDUSTRY_DET 的 BOARD_NAME 实为「申万二级」（127 个），
# 上面 SECTORS 只是手挑的 12 个「有对应 ETF、可画走势」的重点板块。
# 下面这张表把 127 个二级归到 31 个申万一级，用于前端分组导航；
# 缺任何一个二级都会在运行时告警（见 main），避免数据源扩容后静默漏项。
SW1_MAP = {
    "种植业": "农林牧渔", "渔业": "农林牧渔", "林业Ⅱ": "农林牧渔", "饲料": "农林牧渔",
    "农产品加工": "农林牧渔", "农业综合Ⅱ": "农林牧渔", "养殖业": "农林牧渔", "动物保健Ⅱ": "农林牧渔",
    "化学原料": "基础化工", "化学制品": "基础化工", "化学纤维": "基础化工", "塑料": "基础化工",
    "橡胶": "基础化工", "农化制品": "基础化工", "非金属材料Ⅱ": "基础化工",
    "普钢": "钢铁", "特钢Ⅱ": "钢铁", "冶钢原料": "钢铁",
    "工业金属": "有色金属", "贵金属": "有色金属", "小金属": "有色金属",
    "能源金属": "有色金属", "金属新材料": "有色金属",
    "半导体": "电子", "元件": "电子", "光学光电子": "电子", "消费电子": "电子",
    "电子化学品Ⅱ": "电子", "其他电子Ⅱ": "电子",
    "乘用车": "汽车", "商用车": "汽车", "汽车零部件": "汽车", "汽车服务": "汽车", "摩托车及其他": "汽车",
    "白色家电": "家用电器", "黑色家电": "家用电器", "小家电": "家用电器", "厨卫电器": "家用电器",
    "家电零部件Ⅱ": "家用电器", "照明设备Ⅱ": "家用电器", "其他家电Ⅱ": "家用电器",
    "白酒Ⅱ": "食品饮料", "非白酒": "食品饮料", "饮料乳品": "食品饮料", "休闲食品": "食品饮料",
    "调味发酵品Ⅱ": "食品饮料", "食品加工": "食品饮料",
    "纺织制造": "纺织服饰", "服装家纺": "纺织服饰", "饰品": "纺织服饰",
    "家居用品": "轻工制造", "造纸": "轻工制造", "包装印刷": "轻工制造", "文娱用品": "轻工制造",
    "化学制药": "医药生物", "中药Ⅱ": "医药生物", "生物制品": "医药生物", "医药商业": "医药生物",
    "医疗器械": "医药生物", "医疗服务": "医药生物", "医疗美容": "医药生物",
    "电力": "公用事业", "燃气Ⅱ": "公用事业",
    "航空机场": "交通运输", "铁路公路": "交通运输", "物流": "交通运输", "航运港口": "交通运输",
    "房地产开发": "房地产", "房地产服务": "房地产",
    "一般零售": "商贸零售", "专业连锁Ⅱ": "商贸零售", "互联网电商": "商贸零售",
    "贸易Ⅱ": "商贸零售", "旅游零售Ⅱ": "商贸零售",
    "酒店餐饮": "社会服务", "旅游及景区": "社会服务", "教育": "社会服务", "专业服务": "社会服务",
    "综合Ⅱ": "综合",
    "水泥": "建筑材料", "玻璃玻纤": "建筑材料", "装修建材": "建筑材料",
    "房屋建设Ⅱ": "建筑装饰", "基础建设": "建筑装饰", "专业工程": "建筑装饰",
    "工程咨询服务Ⅱ": "建筑装饰", "装修装饰Ⅱ": "建筑装饰",
    "电池": "电力设备", "光伏设备": "电力设备", "风电设备": "电力设备", "电网设备": "电力设备",
    "电机Ⅱ": "电力设备", "其他电源设备Ⅱ": "电力设备",
    "通用设备": "机械设备", "专用设备": "机械设备", "工程机械": "机械设备",
    "自动化设备": "机械设备", "轨交设备Ⅱ": "机械设备",
    "地面兵装Ⅱ": "国防军工", "航海装备Ⅱ": "国防军工", "航空装备Ⅱ": "国防军工",
    "航天装备Ⅱ": "国防军工", "军工电子Ⅱ": "国防军工",
    "计算机设备": "计算机", "软件开发": "计算机", "IT服务Ⅱ": "计算机",
    "游戏Ⅱ": "传媒", "出版": "传媒", "电视广播Ⅱ": "传媒", "广告营销": "传媒",
    "数字媒体": "传媒", "影视院线": "传媒",
    "通信设备": "通信", "通信服务": "通信",
    "银行Ⅱ": "银行",
    "证券Ⅱ": "非银金融", "保险Ⅱ": "非银金融", "多元金融": "非银金融",
    "个护用品": "美容护理", "化妆品": "美容护理",
    "煤炭开采": "煤炭", "焦炭Ⅱ": "煤炭",
    "炼化及贸易": "石油石化", "油服工程": "石油石化", "油气开采Ⅱ": "石油石化",
    "环保设备Ⅱ": "环保", "环境治理": "环保",
}

# 申万一级 → 四大风格（与原有四象限/风格筛选保持一致）
SW1_STYLE = {
    "煤炭": "cyclical", "石油石化": "cyclical", "有色金属": "cyclical",
    "钢铁": "cyclical", "基础化工": "cyclical", "建筑材料": "cyclical",
    "银行": "financial", "非银金融": "financial", "房地产": "financial",
    "电子": "growth", "计算机": "growth", "通信": "growth", "电力设备": "growth",
    "机械设备": "growth", "国防军工": "growth", "医药生物": "growth", "汽车": "growth",
    "食品饮料": "defensive", "家用电器": "defensive", "农林牧渔": "defensive",
    "公用事业": "defensive", "交通运输": "defensive", "商贸零售": "defensive",
    "社会服务": "defensive", "纺织服饰": "defensive", "轻工制造": "defensive",
    "传媒": "defensive", "美容护理": "defensive", "环保": "defensive",
    "建筑装饰": "defensive", "综合": "defensive",
}

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
            "pageSize": "5000",   # 单板块 10 年约 2400 行，一页拿完，127 个板块才跑得动
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


# ---------------------------------------------------------------- 细分行业全量
def fetch_board_list(as_of=None):
    """取当日全部申万二级板块（约 127 个）的代码与名称。"""
    params = {
        "reportName": "RPT_VALUEINDUSTRY_DET",
        "columns": "BOARD_NAME,BOARD_CODE,TRADE_DATE",
        "pageNumber": "1", "pageSize": "500",
        "sortColumns": "BOARD_CODE", "sortTypes": "1",
    }
    if as_of:
        params["filter"] = "(TRADE_DATE='%s')" % as_of
    else:
        params["sortColumns"] = "TRADE_DATE"
        params["sortTypes"] = "-1"
    data = http_json(VALUATION_API, params)
    rows = ((data.get("result") or {}).get("data")) or []
    seen, out = set(), []
    for r in rows:
        name = (r.get("BOARD_NAME") or "").strip()
        code = (r.get("BOARD_CODE") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append((code, name))
    return out


def build_valuation_lite(rows):
    """127 个板块用的精简版：只保留分位与结构指标，不建轨迹数组。

    完整版 build_valuation 会生成 pb_trail / pe_stat / pb_stat，
    127 个板块逐条算会明显拖慢，且全览表用不上。
    """
    if not rows:
        return None
    n = len(rows)
    as_of = rows[-1]["date"]
    window = [r for r in rows if r["date"] >= _cutoff(as_of)]
    pe_series = [r["pe"] for r in window]
    pb_series = [r["pb"] for r in window]
    cur_pe, cur_pb = rows[-1]["pe"], rows[-1]["pb"]

    def ago(k, key, lo, hi):
        i = n - 1 - k
        return pct_at(rows, i, key, lo, hi) if i >= 0 else None

    last = rows[-1]
    num, loss, cap = last.get("num"), last.get("loss"), last.get("cap")
    pe_lar = last.get("pe_lar")
    return {
        "as_of": as_of,
        "samples": len(window),
        "pe": round(cur_pe, 2) if cur_pe else None,
        "pb": round(cur_pb, 2) if cur_pb else None,
        "pe_lar": round(pe_lar, 2) if pe_lar else None,
        "earnings_trend": round((cur_pe / pe_lar - 1) * 100, 1) if cur_pe and pe_lar else None,
        "pe_pct": percentile_of(pe_series, cur_pe, PE_MIN, PE_MAX),
        "pb_pct": percentile_of(pb_series, cur_pb, PB_MIN, PB_MAX),
        "pe_pct_20d": ago(20, "pe", PE_MIN, PE_MAX),
        "pb_pct_20d": ago(20, "pb", PB_MIN, PB_MAX),
        "pe_pct_60d": ago(60, "pe", PE_MIN, PE_MAX),
        "pb_pct_60d": ago(60, "pb", PB_MIN, PB_MAX),
        "members": int(num) if num else None,
        "loss_count": int(loss) if loss is not None else None,
        "loss_ratio": round(loss / num * 100, 1) if num and loss is not None else None,
        "cap_yi": round(cap / 1e8, 0) if cap else None,
    }


def fetch_all_boards(as_of=None):
    """全量细分行业：127 个申万二级，逐个取 10 年日频估值并算分位。"""
    boards = fetch_board_list(as_of)
    if not boards:
        raise RuntimeError("板块清单为空")
    log("细分行业清单 %d 个，开始逐个取估值…" % len(boards))
    out, missing, failed = [], [], []
    for i, (code, name) in enumerate(boards, 1):
        sw1 = SW1_MAP.get(name)
        if not sw1:
            missing.append(name)
        try:
            val = build_valuation_lite(fetch_valuation(name))
            if not val:
                raise RuntimeError("空序列")
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            log("  估值 [%3d/%d] %-12s 失败: %s" % (i, len(boards), name, str(exc)[:70]))
            time.sleep(0.25)
            continue
        item = {
            "code": code, "name": name,
            "sw1": sw1 or "其他", "style": SW1_STYLE.get(sw1 or "", "defensive"),
        }
        item.update(val)
        out.append(item)
        if i % 20 == 0:
            log("  进度 %d/%d" % (i, len(boards)))
        time.sleep(0.2)
    if missing:
        log("⚠ 未映射申万一级的板块（请在 SW1_MAP 补齐）: %s" % "、".join(missing))
    if failed:
        log("⚠ 抓取失败的板块 %d 个: %s" % (len(failed), "、".join(failed[:12])))
    out.sort(key=lambda x: (x.get("cap_yi") or 0), reverse=True)
    return out


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
    ap.add_argument("--skip-boards", action="store_true",
                    help="跳过 127 个细分行业全量抓取（调试用，页面会缺全览表）")
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

    # 细分行业全量（127 个申万二级）——「机器人在汽车零部件里」的根因是粒度太粗
    boards = []
    if args.skip_boards:
        log("跳过细分行业全量抓取（--skip-boards）")
    else:
        try:
            boards = fetch_all_boards()
            log("细分行业全量完成：%d 个板块" % len(boards))
        except Exception as exc:  # noqa: BLE001
            log("细分行业全量抓取失败: %s" % str(exc)[:140])

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
        "boards": boards,
        "sw1_order": list(dict.fromkeys(SW1_MAP.values())),
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
