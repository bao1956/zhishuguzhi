"""指数收盘价 + 近半年涨跌幅 + 涨跌幅百分位(3年) + 涨跌幅档位 → 8 个指数分表四列。

计算口径（红利指数六个月反转效应框架，2026-08 访谈整理）：
  近半年涨跌幅        = 当日收盘 / 126 个交易日前收盘 − 1
  涨跌幅百分位(3年)   = 截至当日最近 756 个交易日的「近半年涨跌幅」逐日样本中，
                        严格小于当日值的样本占比（0~100%）
  涨跌幅档位          = <10% 极低 / 10~30% 偏低 / 30~70% 中性 / 70~80% 中性偏高 / ≥80% 偏高
  参考阈值            = ≈1% 分位=极低(红利类反转效应显著,关注修复机会)；≥80%=偏高(考虑止盈)
  样本不足 500 个交易日时百分位/档位留空（避免短窗口失真）。

数据源（收盘价，价格指数非全收益）：
  理杏仁开放平台 POST open.lixinger.com/api/cn/index/fundamental，metricsList=["cp"]
  （收盘点位）。2026-08-14 探测实测 8 指数全覆盖（含深证创业板指 399006 与红利低波100
  930955），2019 年起历史完整。与两融/股息率列同源同 token。
  注：雪球 K 线接口需登录态（无 cookie 返回 400016），Actions 无法持续使用，故不取雪球。

只写 8 个指数分表，不写总表「指数价格」（与两融两列同口径）。
写入约束与其他脚本相同：appendMode="tailOnly"，按「日期+代码」upsert；
分表内已存在的历史行（backfillHistoryFromRepo 插入）会被正常更新，
早于表内首日且表内没有的日期行被 skipped。

环境变量：
  WEBHOOK_URL      Apps Script Web App 地址（必填，除非 PP_EXPORT_JSON 导出模式）
  LIXINGER_TOKEN   开放平台 token（缺失时打警告退出 0，不打红 workflow）
  PP_START_DATE    推送起始日 YYYY-MM-DD（默认今天-14 天；回填/区间修复用）
  PP_EXPORT_JSON   设为文件路径时只导出行到 JSON 不推 webhook（build_history_backfill.py 用）
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

import requests

CST = timezone(timedelta(hours=8))
RECENT_DAYS = 14
FETCH_START = "2019-06-01"  # 留足 126+756 交易日窗口（2023-08 起算百分位需 2019 年中起的数据）
WINDOW_RET = 126            # 近半年 = 126 个交易日
WINDOW_PCT = 756            # 分位窗口 = 756 个交易日（约 3 年）
MIN_SAMPLES = 500           # 分位样本下限

KEY_COLS = ["日期", "代码"]
COL_CLOSE = "收盘价"
COL_RET = "近半年涨跌幅"
COL_PCT = "涨跌幅百分位(3年)"
COL_BAND = "涨跌幅档位"
HEADERS = ["日期", "代码", "名称", COL_CLOSE, COL_RET, COL_PCT, COL_BAND]

LXR_API = "https://open.lixinger.com/api/cn/index/fundamental"

# code=表内「代码」列取值；stock_code=理杏仁开放平台参数（与 fetch_margin.py 同口径）
INDICES = [
    {"code": "SH000300", "name": "沪深300", "tab": "沪深300", "stock_code": "000300"},
    {"code": "SH000905", "name": "中证500", "tab": "中证500", "stock_code": "000905"},
    {"code": "SH000852", "name": "中证1000", "tab": "中证1000", "stock_code": "000852"},
    {"code": "SZ399006", "name": "创业板", "tab": "创业板", "stock_code": "399006"},
    {"code": "SH000015", "name": "上证红利", "tab": "上证红利", "stock_code": "000015"},
    {"code": "SH000922", "name": "中证红利", "tab": "中证红利", "stock_code": "000922"},
    {"code": "CSIH30269", "name": "红利低波", "tab": "红利低波", "stock_code": "H30269"},
    {"code": "930955", "name": "红利低波100", "tab": "红利低波100", "stock_code": "930955"},
]


def _retry(fn, n=3, wait=2):
    last = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < n - 1:
                time.sleep(wait * (i + 1))
    raise last


def sheet_date(d: date) -> str:
    return f"{d.year}/{d.month}/{d.day}"


def fetch_closes(token: str, stock_code: str, end: date) -> list[tuple[date, float]]:
    """理杏仁 cp 收盘点位，按日期升序返回 (交易日, 收盘价)。"""
    def _one():
        r = requests.post(LXR_API, json={
            "token": token,
            "startDate": FETCH_START,
            "endDate": end.isoformat(),
            "stockCodes": [stock_code],
            "metricsList": ["cp"],
        }, timeout=60)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("data")
        if not isinstance(items, list):
            raise RuntimeError(f"开放平台返回异常: {str(payload)[:300]}")
        out = []
        for it in items:
            v = it.get("cp")
            if v is None:
                continue
            d = datetime.fromisoformat(it["date"].replace("Z", "+00:00")) \
                .astimezone(CST).date()
            out.append((d, float(v)))
        return sorted(out)
    return _retry(_one)


def band_label(pct: float) -> str:
    if pct < 10:
        return "极低"
    if pct < 30:
        return "偏低"
    if pct < 70:
        return "中性"
    if pct < 80:
        return "中性偏高"
    return "偏高"


def compute_rows(closes: list[tuple[date, float]], idx: dict,
                 start: date) -> list[list]:
    """对 start 起的每个交易日产出一行：[日期,代码,名称,收盘,涨跌幅%,分位%,档位]。"""
    dates = [d for d, _ in closes]
    vals = [c for _, c in closes]
    rets: list[float | None] = [None] * len(vals)
    for i in range(WINDOW_RET, len(vals)):
        base = vals[i - WINDOW_RET]
        if base:
            rets[i] = vals[i] / base - 1

    rows = []
    for i, d in enumerate(dates):
        if d < start:
            continue
        r = rets[i]
        ret_s = f"{r * 100:.2f}%" if r is not None else ""
        pct_s = band_s = ""
        if r is not None:
            window = [x for x in rets[max(0, i - WINDOW_PCT + 1): i + 1] if x is not None]
            if len(window) >= MIN_SAMPLES:
                pct = sum(1 for x in window if x < r) / len(window) * 100
                pct_s = f"{pct:.1f}%"
                band_s = band_label(pct)
        rows.append([sheet_date(d), idx["code"], idx["name"],
                     round(vals[i], 2), ret_s, pct_s, band_s])
    return rows


def post_webhook(webhook_url: str, sheet_name: str, rows: list) -> dict:
    body = json.dumps({
        "sheetName": sheet_name,
        "headers": HEADERS,
        "keyCols": KEY_COLS,
        "rows": rows,
        "appendMode": "tailOnly",
    }, ensure_ascii=False).encode("utf-8")
    last_err = None
    for attempt in range(3):
        req = urllib.request.Request(
            webhook_url, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # 写入幂等，超时/瞬断直接重试
            last_err = e
            print(f"  [retry {attempt + 1}/3] {sheet_name}: {e}", file=sys.stderr)
    raise last_err


def main() -> int:
    export_path = os.environ.get("PP_EXPORT_JSON", "").strip()
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if not export_path and not webhook_url:
        print("[FATAL] 缺少 WEBHOOK_URL", file=sys.stderr)
        return 2

    token = os.environ.get("LIXINGER_TOKEN", "").strip()
    if not token:
        print("[WARN] 未配置 LIXINGER_TOKEN，跳过收盘价/百分位列（配好 secret 后自动恢复）",
              file=sys.stderr)
        return 0

    today = datetime.now(CST).date()
    start_raw = os.environ.get("PP_START_DATE", "").strip()
    start = date.fromisoformat(start_raw) if start_raw else today - timedelta(days=RECENT_DAYS)

    exported: dict[str, list[list]] = {}
    failures = 0
    for idx in INDICES:
        try:
            closes = fetch_closes(token, idx["stock_code"], today)
        except Exception as e:
            print(f"[ERROR] [{idx['name']}] 收盘价抓取失败: {e}", file=sys.stderr)
            failures += 1
            continue
        rows = compute_rows(closes, idx, start)
        if not rows:
            print(f"[WARN] [{idx['name']}] {start} 起无数据，跳过", file=sys.stderr)
            continue
        print(f"[{idx['name']}] {rows[0][0]} ... {rows[-1][0]} 共 {len(rows)} 行 "
              f"最新: 收盘{rows[-1][3]} 涨跌幅{rows[-1][4]} 分位{rows[-1][5]} {rows[-1][6]}",
              file=sys.stderr)

        if export_path:
            exported[idx["tab"]] = rows
            continue

        try:
            result = post_webhook(webhook_url, idx["tab"], rows)
            print(f"  「{idx['tab']}」: {result}", file=sys.stderr)
            if result.get("status") != "ok":
                failures += 1
        except Exception as e:
            print(f"[ERROR] 「{idx['tab']}」写入失败: {e}", file=sys.stderr)
            failures += 1

    if export_path:
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(exported, f, ensure_ascii=False)
        print(f"已导出 {sum(len(v) for v in exported.values())} 行到 {export_path}",
              file=sys.stderr)

    if failures:
        print(f"[FATAL] 共 {failures} 处失败", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
