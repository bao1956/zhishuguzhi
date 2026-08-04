"""理杏仁「指数融资融券」→ 4 个红利分表两列：
  「融资余额(亿)」        financingBalance / 1e8，亿元，两位小数
  「融资余额/流通市值」   financingBalanceToMarketCap，百分比字符串三位小数（如 "0.580%"）

数据源：理杏仁开放平台 POST open.lixinger.com/api/cn/index/margin-trading-and-securities-lending
  按 stockCode 单指数查询，startDate 必填，单次区间 ≤10 年。
  已实测（2026-08-04，Actions run 30874184960）四个红利指数全覆盖、日频更新到前一交易日。

只写 4 个红利分表（上证红利/中证红利/红利低波/红利低波100），不写总表「指数价格」——
用户口径：两融只在红利分表单独展示。两列表头由 Apps Script 自动扩列追加在各分表表尾。

写入约束与 fetch_lixinger.py 相同：appendMode="tailOnly"，按「日期+代码」upsert，
早于表内首日的历史行 / 窗口内表里没有的旧日期一律 skipped，绝不在表尾插乱序旧行。

环境变量：
  WEBHOOK_URL     Apps Script Web App 地址（必填）
  LIXINGER_TOKEN  开放平台 token（缺失时打警告退出 0，不打红 workflow）
  MG_START_DATE   起始日 YYYY-MM-DD（默认今天-14 天；首次回填/区间修复直接设早期日期，
                  早于分表已有数据首日的行会被 tailOnly 自动 skipped，无副作用）
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

import requests

MARGIN_API = "https://open.lixinger.com/api/cn/index/margin-trading-and-securities-lending"
RECENT_DAYS = 14
CST = timezone(timedelta(hours=8))

KEY_COLS = ["日期", "代码"]
COL_BAL = "融资余额(亿)"
COL_RATIO = "融资余额/流通市值"
HEADERS = ["日期", "代码", "名称", COL_BAL, COL_RATIO]

# 与 fetch_lixinger.py INDICES 同口径（code=表内「代码」列取值，stock_code=开放平台参数）
INDICES = [
    {"code": "SH000015", "name": "上证红利", "stock_code": "000015", "tab": "上证红利"},
    {"code": "SH000922", "name": "中证红利", "stock_code": "000922", "tab": "中证红利"},
    {"code": "CSIH30269", "name": "红利低波", "stock_code": "H30269", "tab": "红利低波"},
    {"code": "930955", "name": "红利低波100", "stock_code": "930955", "tab": "红利低波100"},
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


def to_sheet_date(raw: str) -> str:
    """开放平台 ISO 带时区时间戳 → 表内 yyyy/M/d（北京时间取日期）。"""
    d = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(CST).date()
    return f"{d.year}/{d.month}/{d.day}"


def fetch_margin(token: str, stock_code: str, start: str, end: str) -> list[list]:
    """返回 [[yyyy/M/d, 融资余额(亿), 占比%], ...]，缺任一字段的行跳过。"""
    def _one():
        r = requests.post(MARGIN_API, json={
            "token": token,
            "stockCode": stock_code,
            "startDate": start,
            "endDate": end,
        }, timeout=30)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("data")
        if not isinstance(items, list):
            raise RuntimeError(f"开放平台返回异常: {str(payload)[:300]}")
        rows = []
        for it in items:
            bal = it.get("financingBalance")
            ratio = it.get("financingBalanceToMarketCap")
            if bal is None:  # 当日无数据的占位行（如查询日当天）
                continue
            rows.append([
                to_sheet_date(it["date"]),
                round(bal / 1e8, 2),
                f"{ratio * 100:.3f}%" if ratio is not None else "",
            ])
        return rows

    return _retry(_one)


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
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("[FATAL] 缺少 WEBHOOK_URL", file=sys.stderr)
        return 2

    token = os.environ.get("LIXINGER_TOKEN", "").strip()
    if not token:
        print("[WARN] 未配置 LIXINGER_TOKEN，跳过两融列（配好 secret 后自动恢复）",
              file=sys.stderr)
        return 0

    today = datetime.now(CST).date()
    start = os.environ.get("MG_START_DATE", "").strip() or \
        (today - timedelta(days=RECENT_DAYS)).isoformat()

    failures = 0
    for idx in INDICES:
        try:
            pairs = fetch_margin(token, idx["stock_code"], start, today.isoformat())
        except Exception as e:
            print(f"[ERROR] [{idx['name']}] 抓取失败: {e}", file=sys.stderr)
            failures += 1
            continue
        if not pairs:
            print(f"[WARN] [{idx['name']}] {start} ~ {today} 无数据，跳过", file=sys.stderr)
            continue

        pairs.sort(key=lambda p: tuple(int(x) for x in p[0].split("/")))
        rows = [[d, idx["code"], idx["name"], bal, ratio] for d, bal, ratio in pairs]
        print(f"[{idx['name']}] {rows[0][0]} {rows[0][3]}亿/{rows[0][4]} ... "
              f"{rows[-1][0]} {rows[-1][3]}亿/{rows[-1][4]}（共 {len(rows)} 天）", file=sys.stderr)

        try:
            result = post_webhook(webhook_url, idx["tab"], rows)
            print(f"  「{idx['tab']}」: {result}", file=sys.stderr)
            if result.get("status") != "ok":
                failures += 1
        except Exception as e:
            print(f"[ERROR] 「{idx['tab']}」写入失败: {e}", file=sys.stderr)
            failures += 1

    if failures:
        print(f"[FATAL] 共 {failures} 处失败", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
