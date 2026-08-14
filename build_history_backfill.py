"""生成 history_backfill.json —— 3 年历史回填数据包（2026-08-14 一次性任务）。

在 GitHub Actions 里运行（backfill_export.yml，需 LIXINGER_TOKEN），生成后由
Actions 提交回仓库；再由 Apps Script 编辑器函数 backfillHistoryFromRepo() 从
仓库 raw 地址拉取并合并进 8 个指数分表：按「日期+代码」命中则填值，未命中则
插入并整块按日期重排（统计行保持钉底）。

打包内容（分列来源，与「说明」tab 口径一致）：
  1. 收盘价/近半年涨跌幅/涨跌幅百分位(3年)/涨跌幅档位（8 分表，日频，2023-08-14 起）
     —— 收盘价来源理杏仁开放平台 cn/index/fundamental 指标 cp（收盘点位，8 指数全覆盖），
     复用 fetch_price_percentile.compute_rows
  2. PE/PB（7 分表，红利低波100 蛋卷不覆盖）—— 蛋卷（雪球旗下）图表历史接口
     djapi/index_eva/{pe,pb}_history/{code}?day=all，周频（每周一个点），
     仅回填 2026-04-27（表内既有日频数据首日）之前，避免覆盖既有日频快照。
     蛋卷无 PE百分位/PB百分位/股息率/雪球档位 历史接口，这四列历史留空。
  3. 有知有行温度（5 分表：沪深300/中证500/中证1000/创业板/中证红利）——
     指数页 data-temp-history 内嵌历史，周频，同样只回填 2026-04-27 之前。
     有知有行股息率无历史数据，留空。
  4. notes —— 追加到「说明」tab 的统计规则与回填口径文本。

非交易日的周频点（如节假日周一）就近归到前一个交易日；若该日已有值则跳过。
理杏仁股息率 2 列与 10 年国债 2 列、两融 2 列不在本包内——历史行插入后用现有
workflow_dispatch 回填（lxr_start_date / ty_start_date / mg_start_date），upsert 命中即更新。

环境变量：LIXINGER_TOKEN（必填）
运行：python3 build_history_backfill.py   （输出 history_backfill.json）
"""

from __future__ import annotations

import html as ihtml
import json
import os
import re
import sys
from datetime import date, datetime

import requests

from fetch_price_percentile import (
    CST, INDICES, _retry, compute_rows, fetch_closes, sheet_date,
)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

BACKFILL_START = date(2023, 8, 14)   # 回跑 3 年
EXISTING_FIRST = date(2026, 4, 27)   # 表内既有日频数据首日，周频历史只补这之前
OUT = "history_backfill.json"

TAB_HEADERS = ["日期", "代码", "名称", "PE", "PB", "有知有行温度",
               "收盘价", "近半年涨跌幅", "涨跌幅百分位(3年)", "涨跌幅档位"]

DJ_HISTORY = "https://danjuanfunds.com/djapi/index_eva/{kind}_history/{code}?day=all"
YZYX_URL = "https://youzhiyouxing.cn/data/indices/{code}"
YZYX_CODE = {
    "SH000300": "000300.SH",
    "SH000905": "000905.SH",
    "SH000852": "000852.SH",
    "SZ399006": "399006.SZ",
    "SH000922": "000922.CSI",
}

NOTES = [
    "收盘价 | 近半年涨跌幅 | 涨跌幅百分位(3年) | 涨跌幅档位",
    "以上四列覆盖全部8个指数分表，2026/8/14 起随 daily workflow 每工作日自动更新，并已按 2023/8/14 起回填3年历史（fetch_price_percentile.py）。"
    "收盘价来源于理杏仁开放平台 open.lixinger.com/api/cn/index/fundamental 指标 cp（收盘点位，价格指数非全收益，"
    "8指数全覆盖含创业板指399006与红利低波100），与两融/理杏仁股息率列同源同token；"
    "雪球K线接口需登录态、自动化无法持续使用，故收盘价未取自雪球。",
    "统计规则（红利指数六个月反转效应框架）：近半年涨跌幅 = 当日收盘 ÷ 126个交易日前收盘 − 1；"
    "涨跌幅百分位(3年) = 截至当日最近756个交易日的「近半年涨跌幅」逐日样本中、严格小于当日值的样本占比；"
    "涨跌幅档位按百分位分五档：<10%极低 / 10~30%偏低 / 30~70%中性 / 70~80%中性偏高 / ≥80%偏高；样本不足500个交易日时百分位与档位留空。",
    "参考阈值：红利类资产反转效应显著（无趋势效应），近半年涨跌幅分位≈1%=历史极低、易吸引配置资金、可关注反转修复机会；"
    "≥80%=涨幅偏高、不建议买入甚至考虑止盈。实证校验：截至2026/6/30 红利低波/红利低波100 近半年约-11%/-10.6%，"
    "处近三年0.7%/0.8%分位，随后6周实际反弹约+7%。",
    "历史回填口径（2026/8/14 执行，backfillHistoryFromRepo）：全部8个分表按 2023/8/14 起回填3年。"
    "收盘价四列=日频全量（理杏仁cp）；PE/PB=蛋卷（雪球旗下）图表历史接口（djapi index_eva pe_history/pb_history?day=all，周频、每周仅一个点），"
    "红利低波100蛋卷不覆盖无PE/PB；PE百分位/PB百分位/股息率/雪球档位四列蛋卷无历史接口、2026/4/27之前留空；"
    "有知有行温度=指数页内嵌历史（周频，覆盖沪深300/中证500/中证1000/创业板/中证红利），有知有行股息率无历史留空；"
    "六亿居士/望京博格温度自2026年5月开始采集、无更早历史；"
    "理杏仁/理杏仁分位点/10年国债两列按 2023/8/14 起、融资余额两列按 2025/1/2 起（理杏仁接口数据起点）用开放平台回填，逐列来源与既有口径一致。",
]


def fetch_danjuan_history(dj_code: str, kind: str) -> dict[date, float]:
    """蛋卷周频历史 {日期: 值}；kind = pe / pb。"""
    def _one():
        r = requests.get(DJ_HISTORY.format(kind=kind, code=dj_code), headers=UA, timeout=30)
        r.raise_for_status()
        payload = r.json()
        if payload.get("result_code") != 0:
            raise RuntimeError(f"{dj_code} {kind}_history: {payload}")
        arr = payload["data"][f"index_eva_{kind}_growths"]
        out = {}
        for it in arr:
            d = datetime.fromtimestamp(it["ts"] / 1000, tz=CST).date()
            out[d] = it[kind]
        return out
    return _retry(_one)


def fetch_yzyx_history(yzyx_code: str) -> dict[date, int]:
    """有知有行温度周频历史 {日期: 温度}。"""
    def _one():
        r = requests.get(YZYX_URL.format(code=yzyx_code), headers=UA, timeout=30)
        r.raise_for_status()
        m = re.search(r'data-temp-history="([^"]*)"', r.text)
        if not m:
            raise RuntimeError(f"{yzyx_code} 页面无 data-temp-history")
        out = {}
        for it in json.loads(ihtml.unescape(m.group(1))):
            if it.get("degree") is None:
                continue
            out[date.fromisoformat(it["date"])] = it["degree"]
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
        tab = idx["tab"]
        print(f"[{idx['name']}] 抓收盘价（理杏仁 cp）...", file=sys.stderr)
        closes = fetch_closes(token, idx["stock_code"], today)
        cal_set = {d for d, _ in closes}
        cal_sorted = sorted(cal_set)
        price_rows = compute_rows(closes, idx, BACKFILL_START)

        def snap(d: date) -> date | None:
            """非交易日就近归到前一个交易日；早于日历首日返回 None。"""
            if d in cal_set:
                return d
            import bisect
            i = bisect.bisect_left(cal_sorted, d)
            return cal_sorted[i - 1] if i > 0 else None

        # 日期 → 行（先装价格四列）
        merged: dict[str, list] = {}
        for r in price_rows:
            merged[r[0]] = [r[0], r[1], r[2], "", "", "", r[3], r[4], r[5], r[6]]

        def fill(col_i: int, series: dict[date, object], fmt) -> int:
            n = 0
            for d, v in sorted(series.items()):
                if not (BACKFILL_START <= d < EXISTING_FIRST):
                    continue
                sd = snap(d)
                if sd is None:
                    continue
                key = sheet_date(sd)
                if key not in merged:
                    merged[key] = [key, idx["code"], idx["name"], "", "", "", "", "", "", ""]
                if merged[key][col_i] == "":
                    merged[key][col_i] = fmt(v)
                    n += 1
            return n

        if idx["code"] != "930955":  # 蛋卷不覆盖红利低波100
            n_pe = fill(3, fetch_danjuan_history(idx["code"], "pe"), lambda v: f"{v:.4f}")
            n_pb = fill(4, fetch_danjuan_history(idx["code"], "pb"), lambda v: f"{v:.4f}")
            print(f"  蛋卷 PE {n_pe} 点 / PB {n_pb} 点（周频）", file=sys.stderr)

        yz = YZYX_CODE.get(idx["code"])
        if yz:
            n_t = fill(5, fetch_yzyx_history(yz), lambda v: v)
            print(f"  有知有行温度 {n_t} 点（周频）", file=sys.stderr)

        rows = [merged[k] for k in sorted(
            merged, key=lambda s: tuple(int(x) for x in s.split("/")))]
        tabs[tab] = {"headers": TAB_HEADERS, "rows": rows}
        print(f"  合计 {len(rows)} 行 {rows[0][0]} ~ {rows[-1][0]}", file=sys.stderr)

    out = {
        "generated": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "backfill_start": BACKFILL_START.isoformat(),
        "tabs": tabs,
        "notes": NOTES,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    size = len(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    print(f"\n已写 {OUT}（{size/1024:.0f} KB, {sum(len(t['rows']) for t in tabs.values())} 行）",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
