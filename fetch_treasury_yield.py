"""10 年期国债到期收益率（双源）→ 4 个红利分表两列：
  「10年国债(中债)」   官方一手：中债-中国债券信息网（中央结算公司）
                      「中债国债收益率曲线」10 年期，历史查询接口
                      yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery，
                      HTML 表格解析（与 gov_direct/equity_bond_official.py 同源同逻辑）。
  「10年国债(理杏仁)」 理杏仁开放平台 POST open.lixinger.com/api/macro/national-debt，
                      areaCode="cn"，指标 tcm_y10（十年期收益率），返回小数（0.0168=1.68%）。
                      理杏仁上游同为中债曲线，两列口径一致，可互为校验。

只写 4 个红利分表（上证红利/中证红利/红利低波/红利低波100），不写总表「指数价格」——
国债收益率是全市场共用序列，不属于任何单一指数，进总表会在 7 指数行间重复。
两列表头由 Apps Script 自动扩列追加在各分表表尾。

写入约束与 fetch_lixinger.py 相同：appendMode="tailOnly"，按「日期+代码」upsert，
历史缺行（如 2026/5/8）一律 skipped，绝不在表尾插乱序旧日期。
数值发 "1.6835%" 百分比字符串（与理杏仁两列同约定，Sheets 识别为百分比数值）。

环境变量：
  WEBHOOK_URL     Apps Script Web App 地址（必填）
  LIXINGER_TOKEN  开放平台 token（缺失时只推中债列，打警告不打红 workflow）
  TY_START_DATE   起始日 YYYY-MM-DD（默认今天-14 天；回填历史直接设早期日期，
                  如 2026-04-27，两个源都支持任意区间，无需单独回填文件）

失败语义：单源失败 → 打警告推另一源（列留空，次日 14 天窗口自愈）；
双源全失败或任一分表写入失败 → 非零退出打红 workflow。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

import requests
import urllib3

urllib3.disable_warnings()  # 中债证书链不全，与 curl -sk 同等处理

CST = timezone(timedelta(hours=8))
RECENT_DAYS = 14
KEY_COLS = ["日期", "代码"]
COL_CB = "10年国债(中债)"
COL_LXR = "10年国债(理杏仁)"
HEADERS = ["日期", "代码", "名称", COL_CB, COL_LXR]

CHINABOND_URL = ("https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/historyQuery"
                 "?startDate={start}&endDate={end}&gjqx=0&qxId=ycqx&locale=cn_ZH")
LXR_URL = "https://open.lixinger.com/api/macro/national-debt"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# 与 fetch_lixinger.py INDICES 的分表口径一致（code=表内「代码」列取值）
TABS = [
    {"code": "SH000015", "name": "上证红利", "tab": "上证红利"},
    {"code": "SH000922", "name": "中证红利", "tab": "中证红利"},
    {"code": "CSIH30269", "name": "红利低波", "tab": "红利低波"},
    {"code": "930955", "name": "红利低波100", "tab": "红利低波100"},
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


def fetch_chinabond(start: date, end: date) -> dict[str, float]:
    """中债 historyQuery 逐年抓（接口按年查询最稳），解析 HTML 表格取 10Y 列。
    返回 {yyyy/M/d: 1.6835}（百分数）。"""
    out: dict[str, float] = {}
    for y in range(start.year, end.year + 1):
        s = max(start, date(y, 1, 1)).isoformat()
        e = min(end, date(y, 12, 31)).isoformat()

        def _one(s=s, e=e):
            r = requests.get(CHINABOND_URL.format(start=s, end=e),
                             headers={"User-Agent": UA}, timeout=40, verify=False)
            r.raise_for_status()
            got = {}
            for tr in re.findall(r"<tr.*?</tr>", r.text, re.S):
                cells = [re.sub(r"<[^>]+>", "", c).strip()
                         for c in re.findall(r"<td.*?</td>", tr, re.S)]
                cells = [c for c in cells if c]
                # cells: 曲线名/日期/3M/6M/1Y/3Y/5Y/7Y/[10Y=idx8]/30Y
                if len(cells) >= 9 and cells[0] == "中债国债收益率曲线":
                    try:
                        d = date.fromisoformat(cells[1])
                        got[sheet_date(d)] = float(cells[8])
                    except ValueError:
                        pass
            return got

        out.update(_retry(_one))
    return out


def fetch_lixinger(token: str, start: date, end: date) -> dict[str, float]:
    """理杏仁开放平台国债接口，tcm_y10 小数 → 百分数。返回 {yyyy/M/d: 1.6835}。"""
    def _one():
        r = requests.post(LXR_URL, json={
            "token": token,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "areaCode": "cn",
            "metricsList": ["tcm_y10"],
        }, timeout=30)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("data")
        if not isinstance(items, list):
            raise RuntimeError(f"开放平台返回异常: {str(payload)[:300]}")
        got = {}
        for it in items:
            v = it.get("tcm_y10")
            if v is None:
                continue
            d = datetime.fromisoformat(it["date"].replace("Z", "+00:00")) \
                .astimezone(CST).date()
            got[sheet_date(d)] = v * 100
        return got

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

    today = datetime.now(CST).date()
    start_raw = os.environ.get("TY_START_DATE", "").strip()
    start = date.fromisoformat(start_raw) if start_raw else today - timedelta(days=RECENT_DAYS)

    cb: dict[str, float] = {}
    try:
        cb = fetch_chinabond(start, today)
        print(f"[中债] {start} ~ {today} 共 {len(cb)} 天", file=sys.stderr)
    except Exception as e:
        print(f"[WARN] 中债抓取失败（该列本次留空，窗口内次日自愈）: {e}", file=sys.stderr)

    lxr: dict[str, float] = {}
    token = os.environ.get("LIXINGER_TOKEN", "").strip()
    if not token:
        print("[WARN] 未配置 LIXINGER_TOKEN，理杏仁列本次留空", file=sys.stderr)
    else:
        try:
            lxr = fetch_lixinger(token, start, today)
            print(f"[理杏仁] {start} ~ {today} 共 {len(lxr)} 天", file=sys.stderr)
        except Exception as e:
            print(f"[WARN] 理杏仁抓取失败（该列本次留空，窗口内次日自愈）: {e}", file=sys.stderr)

    if not cb and not lxr:
        print("[FATAL] 中债与理杏仁均无数据", file=sys.stderr)
        return 4

    dates = sorted(set(cb) | set(lxr),
                   key=lambda s: tuple(int(x) for x in s.split("/")))
    sample = [f"{d} 中债={cb.get(d, '')} 理杏仁={round(lxr[d], 4) if d in lxr else ''}"
              for d in dates[-3:]]
    print("尾部样例: " + " | ".join(sample), file=sys.stderr)

    failures = 0
    for t in TABS:
        rows = [
            [d, t["code"], t["name"],
             f"{cb[d]:.4f}%" if d in cb else "",
             f"{lxr[d]:.4f}%" if d in lxr else ""]
            for d in dates
        ]
        try:
            result = post_webhook(webhook_url, t["tab"], rows)
            print(f"「{t['tab']}」: {result}", file=sys.stderr)
            if result.get("status") != "ok":
                failures += 1
        except Exception as e:
            print(f"[ERROR] 「{t['tab']}」写入失败: {e}", file=sys.stderr)
            failures += 1

    if failures:
        print(f"[FATAL] 共 {failures} 个分表写入失败", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
