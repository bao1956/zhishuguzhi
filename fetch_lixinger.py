"""理杏仁「等权股息率」(dyr.ew) + 「5年分位点」(dyr.y5.ew.cvpos)
→ Google Sheet 各指数分表 + 总表「理杏仁」「理杏仁分位点」两列。

数据源：理杏仁开放平台 POST https://open.lixinger.com/api/cn/index/fundamental
  dyr.ew          = 股息率·等权口径，与网页图表「股息率 · 等权」完全同口径
  dyr.y5.ew.cvpos = 该股息率在过去 5 年窗口内的分位点（0~1，越高越贵）
  （已用网页接口 /api/ii/price-metrics/get-price-metrics-chart-info 逐日核对，
   分位点窗口固定 5 年，与理杏仁网页默认展示的窗口可能不同，注意甄别）。

覆盖指数（INDICES，2026-07-31 由中证红利扩展为三个）：
  中证红利(SH000922) / 红利低波(CSIH30269) —— 复用现有总表+分表机制；
  红利低波100(930955)             —— 蛋卷/有知有行均不追踪，只有理杏仁一列数据，
                                     只写独立分表，不进总表（分表需已手动建好表头）。

两种运行模式：
  1) 日常增量（默认）：需要 LIXINGER_TOKEN，拉最近 RECENT_DAYS 天幂等 upsert，
     漏跑自愈（窗口内的历史值每次都会重推，Apps Script 按「日期+代码」合并）。
  2) 文件回填：LXR_BACKFILL=1 时改读 lixinger_backfill.json（2026-07-30/31 用网页
     登录态一次性导出的历史数据，按指数代码分组），无需 token，workflow_dispatch 手动触发用。

写入约束：所有推送都带 appendMode="tailOnly"——只更新表内已有日期的行；
未命中的行仅当日期 >= 表内最大日期才允许追加（即只能在表尾累加），
历史缺失日（如 2026/5/8，当天整条流水线调度被丢）一律 skipped，
绝不会在表尾插出乱序的旧日期行。

环境变量：
  WEBHOOK_URL       Apps Script Web App 地址（必填）
  LIXINGER_TOKEN    开放平台 token（日常增量必填；缺失时打印警告退出 0，
                    避免 secret 未配置期间把整个 daily workflow 打红）
  LXR_BACKFILL      "1"/"true" 走文件回填模式
  LXR_START_DATE    增量起始日 YYYY-MM-DD（默认今天-14 天，区间修复用）
  LXR_DELETE_DATES  逗号分隔的 yyyy/M/d 列表：对每个指数按「日期+代码」删除总表/分表中的
                    整行（一次性清理误追加的乱序行用），设了它就只做删除不做推送
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone

import requests

OPEN_API = "https://open.lixinger.com/api/cn/index/fundamental"
RECENT_DAYS = 14
CST = timezone(timedelta(hours=8))

MAIN_TAB = "指数价格"
COL_DYR = "理杏仁"
COL_POS = "理杏仁分位点"
KEY_COLS = ["日期", "代码"]

# code: 表内「代码」列取值（与蛋卷/现有分表口径对齐，无先例的新指数直接用官方代码）
# stock_code: 理杏仁开放平台 stockCodes 参数
# tab: 目标分表名（须已存在，Apps Script 严格模式不会自动建 tab）
# write_main: 是否也写总表「指数价格」（无蛋卷/有知有行数据的新指数不写，避免总表出现
#             格式不一致、其余列全空的孤行）
INDICES = [
    {"code": "SH000922", "name": "中证红利", "stock_code": "000922", "tab": "中证红利", "write_main": True},
    {"code": "CSIH30269", "name": "红利低波", "stock_code": "H30269", "tab": "红利低波", "write_main": True},
    {"code": "930955", "name": "红利低波100", "stock_code": "930955", "tab": "红利低波100", "write_main": False},
]


def to_sheet_date(raw: str) -> str:
    """开放平台日期 → 表内 yyyy/M/d。兼容 '2026-07-29' 与 ISO 带时区两种形式；
    带时区的时间戳统一换算成北京时间取日期（网页接口的 T16:00Z 即北京次日零点）。"""
    if "T" in raw:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(CST).date()
    else:
        d = date.fromisoformat(raw)
    return f"{d.year}/{d.month}/{d.day}"


def fetch_open_api(token: str, stock_code: str, start: str, end: str) -> list[list]:
    r = requests.post(OPEN_API, json={
        "token": token,
        "startDate": start,
        "endDate": end,
        "stockCodes": [stock_code],
        "metricsList": ["dyr.ew", "dyr.y5.ew.cvpos"],
    }, timeout=30)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("data")
    if not isinstance(items, list):
        raise RuntimeError(f"开放平台返回异常: {str(payload)[:300]}")

    rows = []
    for it in items:
        v = it.get("dyr.ew")
        if v is None:
            continue
        pos = it.get("dyr.y5.ew.cvpos")
        rows.append([to_sheet_date(it["date"]), v, pos])
    return rows


def load_backfill(code: str) -> list[list]:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lixinger_backfill.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["indices"][code]["values"]


def post_webhook(webhook_url: str, sheet_name: str, rows: list,
                 delete_keys: list | None = None) -> dict:
    payload = {
        "sheetName": sheet_name,
        "headers": ["日期", "代码", "名称", COL_DYR, COL_POS],
        "keyCols": KEY_COLS,
        "rows": rows,
        "appendMode": "tailOnly",
    }
    if delete_keys:
        payload["deleteKeys"] = delete_keys
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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


def process_index(webhook_url: str, idx: dict, pairs: list[list]) -> int:
    if not pairs:
        print(f"  [{idx['name']}] 没有可推送的数据，跳过", file=sys.stderr)
        return 0

    pairs.sort(key=lambda p: tuple(int(x) for x in p[0].split("/")))
    rows = [
        [d, idx["code"], idx["name"], f"{v * 100:.2f}%", f"{pos * 100:.2f}%" if pos is not None else ""]
        for d, v, pos in pairs
    ]
    print(f"[{idx['name']}] {rows[0][0]} {rows[0][3]}/{rows[0][4]} ... {rows[-1][0]} {rows[-1][3]}/{rows[-1][4]}",
          file=sys.stderr)

    tabs = (MAIN_TAB, idx["tab"]) if idx["write_main"] else (idx["tab"],)
    failures = 0
    for tab in tabs:
        try:
            result = post_webhook(webhook_url, tab, rows)
            print(f"  「{tab}」: {result}", file=sys.stderr)
            if result.get("status") != "ok":
                failures += 1
        except Exception as e:
            print(f"[ERROR] 「{tab}」写入失败: {e}", file=sys.stderr)
            failures += 1
    return failures


def delete_dates(webhook_url: str, dates: list[str]) -> int:
    failures = 0
    for idx in INDICES:
        delete_keys = [[d, idx["code"]] for d in dates]
        print(f"[{idx['name']}] 删除模式：{delete_keys}", file=sys.stderr)
        tabs = (MAIN_TAB, idx["tab"]) if idx["write_main"] else (idx["tab"],)
        for tab in tabs:
            try:
                result = post_webhook(webhook_url, tab, [], delete_keys=delete_keys)
                print(f"  「{tab}」: {result}", file=sys.stderr)
                if result.get("status") != "ok":
                    failures += 1
            except Exception as e:
                print(f"[ERROR] 「{tab}」删除失败: {e}", file=sys.stderr)
                failures += 1
    return failures


def main() -> int:
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("[FATAL] 缺少 WEBHOOK_URL", file=sys.stderr)
        return 2

    dates = [d.strip() for d in os.environ.get("LXR_DELETE_DATES", "").split(",") if d.strip()]
    if dates:
        failures = delete_dates(webhook_url, dates)
        return 5 if failures else 0

    backfill = os.environ.get("LXR_BACKFILL", "").strip().lower() in ("1", "true", "yes")
    token = os.environ.get("LIXINGER_TOKEN", "").strip()
    if not backfill and not token:
        print("[WARN] 未配置 LIXINGER_TOKEN，跳过理杏仁增量（配好 secret 后自动恢复）",
              file=sys.stderr)
        return 0

    today = datetime.now(CST).date()
    start = os.environ.get("LXR_START_DATE", "").strip() or \
        (today - timedelta(days=RECENT_DAYS)).isoformat()

    total_failures = 0
    for idx in INDICES:
        if backfill:
            pairs = load_backfill(idx["code"])
            print(f"[{idx['name']}] 回填模式：lixinger_backfill.json 共 {len(pairs)} 天", file=sys.stderr)
        else:
            pairs = fetch_open_api(token, idx["stock_code"], start, today.isoformat())
            print(f"[{idx['name']}] 增量模式：{start} ~ {today.isoformat()} 共 {len(pairs)} 天", file=sys.stderr)
        total_failures += process_index(webhook_url, idx, pairs)

    if total_failures:
        print(f"[FATAL] 共 {total_failures} 个 tab 写入失败", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
