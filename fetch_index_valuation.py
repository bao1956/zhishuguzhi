"""每日抓取蛋卷/雪球 + 有知有行 指数估值，写入 Google Sheet（通过 Apps Script Webhook）。

环境变量：
  WEBHOOK_URL   Apps Script Web App 部署地址（必填）
  SHEET_NAME    目标 tab 名，默认 "指数"
"""

from datetime import datetime, timedelta, timezone
import html as ihtml
import json
import os
import re
import sys
import urllib.request
import urllib.error

import requests

INDEX_CODES = [
    "SH000300",   # 沪深300
    "SH000905",   # 中证500
    "SH000852",   # 中证1000
    "SZ399006",   # 创业板指
    "SH000015",   # 上证红利
    "SH000922",   # 中证红利
    "CSIH30269",  # 红利低波
]

YZYX_CODE = {
    "SH000300": "000300.SH",
    "SH000905": "000905.SH",
    "SH000852": "000852.SH",
    "SZ399006": "399006.SZ",
    "SH000015": None,
    "SH000922": "000922.CSI",
    "CSIH30269": None,
}

DJ_API = "https://danjuanfunds.com/djapi/index_eva/detail/{code}"
YZYX_URL = "https://youzhiyouxing.cn/data/indices/{code}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
}

EVA_LABEL = {"low": "低估", "normal": "正常", "high": "高估"}
CST = timezone(timedelta(hours=8))

COLUMNS = [
    "日期", "代码", "名称",
    "PE", "PB", "PE 百分位", "PB 百分位", "股息率", "雪球档位",
    "有知有行温度", "有知有行股息率",
]
KEY_COLS = ["日期", "代码"]


def fetch_danjuan(code: str) -> dict:
    r = requests.get(DJ_API.format(code=code), headers=HEADERS, timeout=10)
    r.raise_for_status()
    payload = r.json()
    if payload.get("result_code") != 0:
        raise RuntimeError(f"{code} 蛋卷接口异常: {payload}")
    d = payload["data"]
    snapshot_d = datetime.fromtimestamp(d["ts"] / 1000, tz=CST).date()
    return {
        "snapshot_date": f"{snapshot_d.year}/{snapshot_d.month}/{snapshot_d.day}",
        "code": d["index_code"],
        "name": d["name"],
        "pe": f"{d['pe']:.4f}",
        "pb": f"{d['pb']:.4f}",
        "pe_pct": f"{d['pe_percentile'] * 100:.2f}%",
        "pb_pct": f"{d['pb_percentile'] * 100:.2f}%",
        "yld": f"{d['yeild'] * 100:.2f}%",
        "label": EVA_LABEL.get(d["eva_type"], d["eva_type"]),
    }


def fetch_yzyx(yzyx_code: str) -> dict:
    r = requests.get(YZYX_URL.format(code=yzyx_code), headers=HEADERS, timeout=15)
    r.raise_for_status()
    html = r.text

    degree = ""
    m = re.search(r'data-temp-history="([^"]*)"', html)
    if m:
        try:
            arr = json.loads(ihtml.unescape(m.group(1)))
            if arr and arr[-1].get("degree") is not None:
                degree = str(arr[-1]["degree"])
        except (ValueError, KeyError):
            pass

    text_only = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S)
    text_only = re.sub(r"<[^>]+>", " ", text_only)
    m_dy = re.search(r"股息率\s*([\d.]+%)", text_only)
    yld = m_dy.group(1) if m_dy else ""

    return {"degree": degree, "yield": yld}


def build_row(snap: dict, yzyx: dict) -> list[str]:
    return [
        snap["snapshot_date"], snap["code"], snap["name"],
        snap["pe"], snap["pb"],
        snap["pe_pct"], snap["pb_pct"], snap["yld"], snap["label"],
        yzyx.get("degree", ""), yzyx.get("yield", ""),
    ]


def post_webhook(webhook_url: str, sheet_name: str, rows: list[list[str]]) -> dict:
    payload = {
        "sheetName": sheet_name,
        "headers": COLUMNS,
        "keyCols": KEY_COLS,
        "rows": rows,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("[FATAL] 缺少 WEBHOOK_URL 环境变量", file=sys.stderr)
        return 2

    sheet_name = os.environ.get("SHEET_NAME", "指数价格").strip() or "指数价格"

    rows = []
    for code in INDEX_CODES:
        try:
            snap = fetch_danjuan(code)
        except Exception as e:
            print(f"[ERROR] 蛋卷 {code}: {e}", file=sys.stderr)
            continue

        yzyx = {"degree": "", "yield": ""}
        yzyx_code = YZYX_CODE.get(code)
        if yzyx_code:
            try:
                yzyx = fetch_yzyx(yzyx_code)
            except Exception as e:
                print(f"[WARN] 有知有行 {yzyx_code}: {e}", file=sys.stderr)

        row = build_row(snap, yzyx)
        rows.append(row)
        print("  " + "\t".join(row))

    if not rows:
        print("[FATAL] 没有抓到任何数据，跳过 Webhook", file=sys.stderr)
        return 3

    print(f"\n抓到 {len(rows)} 行，推送到 Sheet tab=「{sheet_name}」 ...", file=sys.stderr)
    try:
        result = post_webhook(webhook_url, sheet_name, rows)
        print(f"Webhook 响应: {result}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        print(f"[FATAL] Webhook HTTP 错: {e.code} {e.read().decode('utf-8', errors='replace')[:300]}", file=sys.stderr)
        return 4
    except Exception as e:
        print(f"[FATAL] Webhook 异常: {e}", file=sys.stderr)
        return 4

    return 0


if __name__ == "__main__":
    sys.exit(main())
