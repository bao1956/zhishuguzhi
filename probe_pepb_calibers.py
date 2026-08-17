"""临时探针：理杏仁开放平台各口径 vs 表内蛋卷 PE/PB/百分位/股息率 差异对比。

目的：8/14 历史回填后，2026-04-27 之前的 PE/PB 只有蛋卷周频点（每周一个）。
要用理杏仁日频历史补全缺口，先确认理杏仁哪个加权口径与蛋卷最接近。

在 Actions 里跑（需 LIXINGER_TOKEN），只打印对比报告，不写任何东西。用完删。

对比方法：从公开 gviz CSV 拉 7 个分表现有数据，按日期与理杏仁日频序列对齐：
  PE / PB / 股息率  → 平均|相对差|%（分历史区周频点 / 4/27 起日频区两段）
  PE百分位 / PB百分位 → 平均|差|个百分点（蛋卷整数%粒度，只有日频区有值）
"""

from __future__ import annotations

import csv
import io
import os
import sys
import time
import urllib.parse
from datetime import date, datetime

import requests

from fetch_price_percentile import CST, INDICES, LXR_API, _retry

SHEET_ID = "1InnFofF6Rw9PzwqMBcNVGyixTxyNLFcuAZZPOrrXTsA"
GVIZ = ("https://docs.google.com/spreadsheets/d/" + SHEET_ID +
        "/gviz/tq?tqx=out:csv&sheet={tab}")
EXISTING_FIRST = date(2026, 4, 27)
START = "2023-08-14"

# 表内列 → 候选理杏仁 metric（蛋卷口径未知，全加权方式都试）
CANDIDATES = {
    "PE":     ["pe_ttm.mcw", "pe_ttm.ew", "pe_ttm.ewpvo", "pe_ttm.avg", "pe_ttm.median"],
    "PB":     ["pb.mcw", "pb.ew", "pb.ewpvo", "pb.avg", "pb.median"],
    "PE 百分位": ["pe_ttm.y10.mcw.cvpos", "pe_ttm.y10.ew.cvpos", "pe_ttm.y10.ewpvo.cvpos",
               "pe_ttm.y10.avg.cvpos", "pe_ttm.y10.median.cvpos"],
    "PB 百分位": ["pb.y10.mcw.cvpos", "pb.y10.ew.cvpos", "pb.y10.avg.cvpos",
               "pb.y10.median.cvpos"],
    "股息率":    ["dyr.mcw", "dyr.ew"],
}
PCT_COLS = {"PE 百分位", "PB 百分位"}   # 蛋卷存整数%，理杏仁 cvpos 是 0~1
RATE_COLS = {"股息率"}                  # 蛋卷存 "4.42%"，理杏仁 dyr 是 0~1 小数


def fetch_sheet(tab: str) -> list[dict]:
    r = requests.get(GVIZ.format(tab=urllib.parse.quote(tab)), timeout=30)
    r.raise_for_status()
    rows = list(csv.reader(io.StringIO(r.text)))
    hdr = rows[0]
    out = []
    for raw in rows[1:]:
        rec = dict(zip(hdr, raw))
        d = rec.get("日期", "")
        try:
            y, m, dd = (int(x) for x in d.split("/"))
            rec["_date"] = date(y, m, dd)
        except Exception:
            continue
        out.append(rec)
    return out


def num(s: str) -> float | None:
    s = (s or "").strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s.rstrip("%"))
    except ValueError:
        return None


def fetch_lxr(token: str, stock_code: str, metrics: list[str],
              end: date) -> dict[str, dict[date, float]]:
    """返回 {metric: {日期: 值}}；整体 400 时逐个 metric 降级重试。"""
    def _post(mlist):
        r = requests.post(LXR_API, json={
            "token": token, "startDate": START, "endDate": end.isoformat(),
            "stockCodes": [stock_code], "metricsList": mlist,
        }, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        payload = r.json()
        items = payload.get("data")
        if not isinstance(items, list):
            raise RuntimeError(f"返回异常: {str(payload)[:200]}")
        return items

    series: dict[str, dict[date, float]] = {m: {} for m in metrics}
    try:
        items = _retry(lambda: _post(metrics))
        groups = [(metrics, items)]
    except Exception as e:
        print(f"    [整体请求失败，逐 metric 降级] {e}", file=sys.stderr)
        groups = []
        for m in metrics:
            time.sleep(1)
            try:
                groups.append(([m], _post([m])))
            except Exception as e2:
                print(f"    [metric 无效] {m}: {str(e2)[:120]}", file=sys.stderr)
    for mlist, items in groups:
        for it in items:
            d = datetime.fromisoformat(it["date"].replace("Z", "+00:00")) \
                .astimezone(CST).date()
            for m in mlist:
                v = it.get(m)
                if v is not None:
                    series[m][d] = float(v)
    return series


def main() -> int:
    token = os.environ.get("LIXINGER_TOKEN", "").strip()
    if not token:
        print("[FATAL] 缺少 LIXINGER_TOKEN", file=sys.stderr)
        return 2
    today = datetime.now(CST).date()
    all_metrics = sorted({m for v in CANDIDATES.values() for m in v})

    for idx in INDICES:
        if idx["code"] == "930955":
            continue  # 蛋卷不覆盖，无对照
        print(f"\n===== {idx['name']} ({idx['code']} / lxr {idx['stock_code']}) =====")
        sheet = fetch_sheet(idx["tab"])
        lxr = fetch_lxr(token, idx["stock_code"], all_metrics, today)
        time.sleep(1)

        for col, cands in CANDIDATES.items():
            targets = {r["_date"]: num(r.get(col, "")) for r in sheet
                       if num(r.get(col, "")) is not None}
            if not targets:
                print(f"  [{col}] 表内无值，跳过")
                continue
            print(f"  [{col}] 表内 {len(targets)} 点"
                  f"（历史区 {sum(1 for d in targets if d < EXISTING_FIRST)}"
                  f" / 日频区 {sum(1 for d in targets if d >= EXISTING_FIRST)}）")
            for m in cands:
                s = lxr.get(m, {})
                if not s:
                    print(f"    {m:<28} 无数据")
                    continue
                for label, cond in (("历史区", lambda d: d < EXISTING_FIRST),
                                    ("日频区", lambda d: d >= EXISTING_FIRST)):
                    common = [d for d in targets if cond(d) and d in s]
                    if not common:
                        continue
                    if col in PCT_COLS:
                        diffs = [abs(s[d] * 100 - targets[d]) for d in common]
                        unit = "个百分点"
                    elif col in RATE_COLS:
                        diffs = [abs(s[d] * 100 - targets[d]) / targets[d] * 100
                                 for d in common]
                        unit = "%相对差"
                    else:
                        diffs = [abs(s[d] - targets[d]) / targets[d] * 100
                                 for d in common]
                        unit = "%相对差"
                    mean = sum(diffs) / len(diffs)
                    mx = max(diffs)
                    print(f"    {m:<28} {label} n={len(common):<4} "
                          f"平均|Δ|={mean:6.2f}{unit}  最大={mx:6.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
