#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仪表盘数据更新脚本
==================
采集全球主要股市指数、全球要闻、今日五行数据，写入 data/dashboard.json，
供 index.html 前端渲染。

更新频率由 GitHub Actions 的 cron 控制（当前每日一次，workflow 中预留了
每小时更新的 cron 配置，改一行即可切换），本脚本本身与频率无关——每次运行
都拉取最新数据并在 JSON 中写入 generated_at / interval 字段。

数据来源（均为公开接口，仅供研究参考，不构成投资建议）：
  - A股指数   akshare.stock_zh_index_spot_sina（新浪）
  - 港股指数   akshare.stock_hk_index_spot_sina（新浪）
  - 美股指数   akshare.index_us_stock_sina（新浪，历史序列取末行）
  - 全球指数   akshare.index_global_spot_em（东财，失败自动跳过）
  - 全球要闻   akshare.stock_info_global_em（东财快讯，失败降级新浪/富途）
  - 今日五行   lunar-python（干支/纳音/宜忌）+ 本地五行生克题材推演

任一数据源失败均不会导致脚本失败：对应模块记录 error，前端显示"暂缺"。
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(REPO_ROOT, "data", "dashboard.json")

CST = timezone(timedelta(hours=8))  # 北京时间

# GitHub Actions 可通过环境变量切换更新频率标识（daily / hourly）
INTERVAL = os.environ.get("DASHBOARD_INTERVAL", "daily")
NEWS_LIMIT = int(os.environ.get("DASHBOARD_NEWS_LIMIT", "20"))


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
    """美股三大指数（新浪历史序列，取末行为最新）。"""
    import akshare as ak

    targets = [
        (".DJI", "道琼斯"),
        (".IXIC", "纳斯达克"),
        (".INX", "标普500"),
    ]
    result = []
    for symbol, name in targets:
        df = ak.index_us_stock_sina(symbol=symbol)
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
        result.append({
            "name": name,
            "code": symbol,
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "date": str(last.get("date", "")),
        })
        time.sleep(0.6)
    if not result:
        raise RuntimeError("新浪美股指数接口返回数据为空")
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


def fetch_news():
    """全球财经要闻：东财快讯为主，新浪/富途降级。"""
    import akshare as ak

    errors = []
    for fn, kw in (
        (ak.stock_info_global_em, {}),
        (ak.stock_info_global_sina, {}),
        (ak.stock_info_global_futu, {}),
    ):
        try:
            records = _df_to_records(fn(**kw))
            news = _norm_news(records)
            if news:
                return news[:NEWS_LIMIT]
            errors.append(f"{fn.__name__}: 空数据")
        except Exception as e:  # noqa: BLE001 - 单源失败降级
            errors.append(f"{fn.__name__}: {type(e).__name__} {e}")
        time.sleep(1)
    raise RuntimeError("；".join(errors) or "全部新闻源失败")


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
    news, errors["全球要闻"] = safe(fetch_news, "全球要闻")
    wuxing, errors["今日五行"] = safe(fetch_wuxing, "今日五行")

    now = datetime.now(CST)
    payload = {
        "schema_version": 2,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S CST"),
        "generated_at_iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "interval": INTERVAL,  # daily | hourly（预留接口，由环境变量/调度控制）
        "update_frequency_note": "当前每日更新一次；切换每小时更新仅需修改 GitHub Actions cron。",
        "markets": {
            "a_share": a_idx,
            "hk": hk_idx,
            "us": us_idx,
            "global": global_idx,
        },
        "news": news,
        "wuxing": wuxing,
        "errors": {k: v for k, v in errors.items() if v},
        "disclaimer": "行情与新闻数据来自公开来源，仅供研究参考，不构成投资建议；接口可能因上游网站变化而失效。",
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp_path = OUT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp_path, OUT_PATH)

    ok_modules = sum(1 for v in errors.values() if not v)
    log(f"完成：{ok_modules}/6 模块成功 -> {OUT_PATH}")
    # 数据部分失败（行情/新闻）时退出码 0 仍成立——页面可展示"暂缺"；
    # 仅当全部行情源都失败时返回非零，便于 Actions 报警。
    market_ok = any(v is None for v in
                    (errors["A股指数"], errors["美股指数"], errors["全球指数"]))
    sys.exit(0 if (market_ok and news is not None) else 1)


if __name__ == "__main__":
    main()
