"""生成 history_backfill.json —— PE/PB 与百分位历史补全数据包（2026-08-17 二次回填）。

背景：2026-08-14 首次回填（build_history_backfill.py）时蛋卷只有周频历史，
2026-04-27 之前的 PE/PB 每周只有一个点、PE百分位/PB百分位历史全空；
另有 2026/5/8（调度被丢、蛋卷快照永久缺失）一天的日频空洞。

本包用理杏仁开放平台 cn/index/fundamental 市值加权口径补全上述空缺：
  PE       = pe_ttm.mcw           （探针实测 vs 蛋卷 7 指数平均相对差 ≤0.41%，同口径）
  PB       = pb.mcw               （平均相对差 ≤0.39%）
  PE 百分位 = pe_ttm.y10.mcw.cvpos（10年窗口分位，vs 蛋卷平均差 0.35~3.46 个百分点）
  PB 百分位 = pb.y10.mcw.cvpos    （平均差 0.70~3.44 个百分点）
探针明细见一次性脚本 probe_pepb_calibers.py 的 Actions 日志（2026-08-17）。

安全约束：
  * 只补**表内已存在的行**（从公开 gviz CSV 读现有行做白名单）——不会插新行、不会重排；
  * 只补**空单元格**（蛋卷既有周频点与日频值原样保留，payload 对应位发空串，
    backfillHistoryFromRepo 对空串跳过不覆盖）；
  * 股息率/雪球档位/有知有行列不在本包内：蛋卷股息率与理杏仁 dyr 各口径实测差 2%~11%
    无法无缝衔接，雪球档位是蛋卷判定标签无法复原，有知有行为黑箱仅周频——均维持现状。
  * 红利低波100（930955）蛋卷不覆盖、PE/PB 列无日常更新，跳过不补。

在 Actions 里运行（backfill_export.yml，builder 输入填本文件名），生成后提交回仓库，
再跑 Apps Script 编辑器函数 backfillHistoryFromRepo() 合并进分表。

环境变量：LIXINGER_TOKEN（必填）
运行：python3 build_pepb_backfill.py   （输出 history_backfill.json）
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.parse
from datetime import date, datetime

import requests

from fetch_price_percentile import CST, INDICES, LXR_API, _retry, sheet_date

SHEET_ID = "1InnFofF6Rw9PzwqMBcNVGyixTxyNLFcuAZZPOrrXTsA"
GVIZ = ("https://docs.google.com/spreadsheets/d/" + SHEET_ID +
        "/gviz/tq?tqx=out:csv&sheet={tab}")
START = "2023-08-14"
OUT = "history_backfill.json"

# 表内列名 → (理杏仁 metric, 值格式化)
FILL_COLS = [
    ("PE", "pe_ttm.mcw", lambda v: f"{v:.4f}"),
    ("PB", "pb.mcw", lambda v: f"{v:.4f}"),
    ("PE 百分位", "pe_ttm.y10.mcw.cvpos", lambda v: f"{v * 100:.2f}%"),
    ("PB 百分位", "pb.y10.mcw.cvpos", lambda v: f"{v * 100:.2f}%"),
]
HEADERS = ["日期", "代码", "名称"] + [c for c, _, _ in FILL_COLS]

NOTES = [
    "PE/PB 与百分位历史补全（2026/8/17 执行，backfillHistoryFromRepo 二次回填）",
    "背景：2026/8/14 首次回填时蛋卷仅有周频历史，2026/4/27 之前 PE/PB 每周只有一个点、"
    "PE百分位/PB百分位历史全空，另有 2026/5/8（调度被丢）一天日频空洞。"
    "本次用理杏仁开放平台 cn/index/fundamental 市值加权口径**只补空单元格**，蛋卷既有值原样保留："
    "PE=pe_ttm.mcw、PB=pb.mcw（与蛋卷同为市值加权，7指数实测平均相对差≤0.41%，衔接无缝）；"
    "PE百分位=pe_ttm.y10.mcw.cvpos、PB百分位=pb.y10.mcw.cvpos（理杏仁10年窗口市值加权分位，"
    "与蛋卷百分位实测平均差0.35~3.46个百分点；历史区百分位整段来自理杏仁，2026/4/27起为蛋卷整数粒度）。",
    "未补列及原因：股息率（蛋卷口径与理杏仁 dyr.mcw/dyr.ew 实测相对差2%~11%，无法无缝衔接，历史维持留空）；"
    "雪球档位（蛋卷高低估判定标签，第三方数据无法复原）；有知有行温度/股息率（官方黑箱，仅周频历史，维持稀疏点）；"
    "红利低波100 蛋卷不覆盖且无日常更新，PE/PB 列维持留空。",
]


def fetch_sheet_rows(tab: str) -> list[dict]:
    """公开 gviz CSV 拉分表现有数据行（含表头映射与解析后的 _date）。"""
    def _one():
        r = requests.get(GVIZ.format(tab=urllib.parse.quote(tab)), timeout=30)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))
        hdr = rows[0]
        out = []
        for raw in rows[1:]:
            rec = dict(zip(hdr, raw))
            try:
                y, m, d = (int(x) for x in rec.get("日期", "").split("/"))
                rec["_date"] = date(y, m, d)
            except Exception:
                continue
            out.append(rec)
        if not out:
            raise RuntimeError(f"{tab}: gviz 无数据行")
        return out
    return _retry(_one)


def fetch_lxr(token: str, stock_code: str, end: date) -> dict[date, dict[str, float]]:
    metrics = [m for _, m, _ in FILL_COLS]
    def _one():
        r = requests.post(LXR_API, json={
            "token": token, "startDate": START, "endDate": end.isoformat(),
            "stockCodes": [stock_code], "metricsList": metrics,
        }, timeout=60)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("data")
        if not isinstance(items, list):
            raise RuntimeError(f"开放平台返回异常: {str(payload)[:300]}")
        out: dict[date, dict[str, float]] = {}
        for it in items:
            d = datetime.fromisoformat(it["date"].replace("Z", "+00:00")) \
                .astimezone(CST).date()
            vals = {m: float(it[m]) for m in metrics if it.get(m) is not None}
            if vals:
                out[d] = vals
        return out
    return _retry(_one)


def main() -> int:
    token = os.environ.get("LIXINGER_TOKEN", "").strip()
    if not token:
        print("[FATAL] 缺少 LIXINGER_TOKEN", file=sys.stderr)
        return 2

    today = datetime.now(CST).date()
    tabs: dict[str, dict] = {}
    for idx in INDICES:
        if idx["code"] == "930955":
            continue
        sheet = fetch_sheet_rows(idx["tab"])
        lxr = fetch_lxr(token, idx["stock_code"], today)
        time.sleep(1)

        rows, filled, miss = [], {c: 0 for c, _, _ in FILL_COLS}, 0
        for rec in sorted(sheet, key=lambda r: r["_date"]):
            vals = lxr.get(rec["_date"])
            row = [sheet_date(rec["_date"]), idx["code"], idx["name"]]
            any_fill = False
            for col, metric, fmt in FILL_COLS:
                if rec.get(col, "").strip():
                    row.append("")          # 已有值（蛋卷），不覆盖
                elif vals and metric in vals:
                    row.append(fmt(vals[metric]))
                    filled[col] += 1
                    any_fill = True
                else:
                    row.append("")
                    miss += 1
            if any_fill:
                rows.append(row)

        tabs[idx["tab"]] = {"headers": HEADERS, "rows": rows}
        print(f"[{idx['name']}] 现有 {len(sheet)} 行 → 补 {len(rows)} 行："
              + " ".join(f"{c}={n}" for c, n in filled.items())
              + (f"（理杏仁缺 {miss} 格）" if miss else ""), file=sys.stderr)

    out = {
        "generated": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "backfill_start": START,
        "tabs": tabs,
        "notes": NOTES,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    print(f"\n已写 {OUT}（{size/1024:.0f} KB, "
          f"{sum(len(t['rows']) for t in tabs.values())} 行）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
