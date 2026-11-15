#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行业板块实时行情 —— 小时级轻量快照生成器
==========================================

输出: data/sector_lab_live.json

职责边界：只更新「价格 / 涨跌幅 / 成交额 / 时间戳」，不重算估值分位。
估值分位是日频数据（数据源收盘后发布一次），盘中每小时重算得到的值完全相同，
因此重活交给 scripts/update_sector_lab.py 每天跑，本脚本交易时段每小时跑一次即可。

本文件是「兜底层」：页面优先用 JSONP 直连行情接口拿真·实时数据；
当用户网络拦截了跨域脚本、或页面离线打开时，自动回退读本快照。

用法：
  python scripts/update_sector_live.py
  python scripts/update_sector_live.py --out /path/to/sector_lab_live.json

依赖：仅 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "data", "sector_lab_live.json")
DEFAULT_MAP = os.path.join(ROOT, "data", "sector_lab.json")

QUOTE_API = "https://push2.eastmoney.com/api/qt/ulist.np/get"
FIELDS = "f1,f2,f3,f4,f6,f12,f13,f14,f124"
# f2 最新价 / f3 涨跌幅% / f4 涨跌额 / f6 成交额 / f12 代码 / f14 名称 / f124 行情时间戳

INDICES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000300", "沪深300"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def log(msg):
    print("[sector-live] " + msg, flush=True)


def bj_now():
    return datetime.now(timezone(timedelta(hours=8)))


def session_of(now, quote_date):
    """按北京时间与行情时间戳判断交易状态。"""
    if now.weekday() >= 5 or (quote_date and quote_date != now.strftime("%Y-%m-%d")):
        return "holiday"
    hm = now.hour * 60 + now.minute
    if 9 * 60 + 15 <= hm < 11 * 60 + 30 or 13 * 60 <= hm < 15 * 60 + 5:
        return "open"
    if 11 * 60 + 30 <= hm < 13 * 60:
        return "lunch"
    return "closed"


def fetch_quotes(secids):
    url = QUOTE_API + "?" + urllib.parse.urlencode({
        "fltt": "2", "invt": "2", "fields": FIELDS, "secids": ",".join(secids),
    })
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            diff = ((data.get("data") or {}).get("diff") or [])
            return {str(x.get("f12")): x for x in diff}
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    raise RuntimeError("行情请求失败: %s" % last)


def main():
    ap = argparse.ArgumentParser(description="生成行业板块实时行情兜底快照")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--map", default=DEFAULT_MAP, help="日频数据文件，用于读取板块代码映射")
    args = ap.parse_args()

    with open(args.map, "r", encoding="utf-8") as f:
        base = json.load(f)
    sectors = base.get("sectors") or []
    if not sectors:
        raise RuntimeError("日频数据中无板块定义，请先运行 update_sector_lab.py")

    def secid(code):
        # sh -> 1. / sz -> 0.
        return ("1." if code.startswith("sh") else "0.") + code[2:]

    secids = [secid(s["code"]) for s in sectors] + [x[0] for x in INDICES]
    raw = fetch_quotes(secids)
    now = bj_now()

    def pack(x):
        if not x:
            return None
        ts = x.get("f124")
        return {
            "price": x.get("f2"), "pct": x.get("f3"), "chg": x.get("f4"),
            "amount": x.get("f6"), "name": x.get("f14"), "ts": ts,
        }

    quotes = {}
    for s in sectors:
        q = pack(raw.get(s["code"][2:]))
        if q:
            quotes[s["id"]] = q
    indices = []
    for sid, name in INDICES:
        q = pack(raw.get(sid.split(".")[1]))
        if q:
            q["label"] = name
            indices.append(q)

    # 用行情时间戳判断是否为当日交易数据
    stamps = [q["ts"] for q in list(quotes.values()) + indices if isinstance(q.get("ts"), (int, float))]
    quote_date = None
    if stamps:
        quote_date = datetime.fromtimestamp(max(stamps), timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    payload = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S") + " (北京时间)",
        "trade_date": quote_date,
        "session": session_of(now, quote_date),
        "source": "东方财富行情接口 ulist.np（小时级兜底快照）",
        "sector_codes": {s["id"]: s["code"] for s in sectors},
        "quotes": quotes,
        "indices": indices,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    log("完成 -> %s (%d 板块 / %d 指数, 状态 %s, %s)" % (
        args.out, len(quotes), len(indices), payload["session"], quote_date))


if __name__ == "__main__":
    main()
