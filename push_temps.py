"""把 temps.json（雪球大V估值图温度提取结果）推送到 Google Sheet 总表+7个分表。

在 GitHub Actions 里跑（本地不持有 WEBHOOK_URL，保持 Actions 唯一写表入口）。
写入沿用 Apps Script webhook 的「日期+代码」幂等合并：新列自动扩表头，
null/空值不会清掉已有数据，温度以 JSON number 发送避免字符串比较造成的重复重写。

环境变量：
  WEBHOOK_URL   Apps Script Web App 地址（必填）
  PUSH_ALL      "1"/"true" 时全量推送（回填用）；默认只推最近 RECENT_DAYS 天
"""

import json
import os
import sys
import urllib.request
from datetime import date, timedelta

RECENT_DAYS = 14

# code -> (sheet 名称列, 分表 tab 名)，与 fetch_index_valuation.py 的 INDEX_TABS 一致
CODE_INFO = {
    "SH000300": ("沪深300", "沪深300"),
    "SH000905": ("中证500", "中证500"),
    "SH000852": ("中证1000", "中证1000"),
    "SZ399006": ("创业板", "创业板"),
    "SH000015": ("上证红利", "上证红利"),
    "SH000922": ("中证红利", "中证红利"),
    "CSIH30269": ("红利低波", "红利低波"),
}

SOURCE_COL = {"六亿居士": "六亿居士温度", "望京博格": "望京博格温度"}
COLUMNS = ["日期", "代码", "名称", "六亿居士温度", "望京博格温度"]
KEY_COLS = ["日期", "代码"]
MAIN_TAB = "指数价格"


def iso_to_sheet_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(y)}/{int(m)}/{int(d)}"


def post_webhook(webhook_url: str, sheet_name: str, rows: list) -> dict:
    payload = {
        "sheetName": sheet_name,
        "headers": COLUMNS,
        "keyCols": KEY_COLS,
        "rows": rows,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("[FATAL] 缺少 WEBHOOK_URL", file=sys.stderr)
        return 2

    push_all = os.environ.get("PUSH_ALL", "").strip().lower() in ("1", "true", "yes")
    temps_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temps.json")
    with open(temps_file, encoding="utf-8") as f:
        temps = json.load(f)

    # 合并成 {(iso_date, code): {列名: 温度}}
    cells = {}
    for source, by_date in temps.items():
        col = SOURCE_COL.get(source)
        if not col:
            print(f"[WARN] 未知来源 {source}，跳过", file=sys.stderr)
            continue
        for iso, by_code in by_date.items():
            for code, v in by_code.items():
                if v is None or code not in CODE_INFO:
                    continue
                cells.setdefault((iso, code), {})[col] = v

    cutoff = None if push_all else (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    rows_by_code = {code: [] for code in CODE_INFO}
    for (iso, code) in sorted(cells):
        if cutoff and iso < cutoff:
            continue
        vals = cells[(iso, code)]
        name = CODE_INFO[code][0]
        row = [iso_to_sheet_date(iso), code, name,
               vals.get("六亿居士温度", ""), vals.get("望京博格温度", "")]
        rows_by_code[code].append(row)

    all_rows = [r for code in CODE_INFO for r in rows_by_code[code]]
    if not all_rows:
        print("窗口内没有可推送的温度数据", file=sys.stderr)
        return 0

    print(f"推送 {len(all_rows)} 行（PUSH_ALL={push_all}）", file=sys.stderr)
    failures = 0
    try:
        result = post_webhook(webhook_url, MAIN_TAB, all_rows)
        print(f"  总表「{MAIN_TAB}」: {result}", file=sys.stderr)
        if result.get("status") != "ok":
            failures += 1
    except Exception as e:
        print(f"[ERROR] 总表写入失败: {e}", file=sys.stderr)
        failures += 1

    for code, (_, tab) in CODE_INFO.items():
        if not rows_by_code[code]:
            continue
        try:
            result = post_webhook(webhook_url, tab, rows_by_code[code])
            print(f"  分表「{tab}」: {result}", file=sys.stderr)
            if result.get("status") != "ok":
                failures += 1
        except Exception as e:
            print(f"[ERROR] 分表「{tab}」写入失败: {e}", file=sys.stderr)
            failures += 1

    if failures:
        print(f"[FATAL] {failures} 个 tab 写入失败", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
