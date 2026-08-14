"""临时导出（回测用，用完删）：理杏仁 4 红利指数 cp/dyr.ew/dyr.y5.ew.cvpos 长历史。

两段请求（单次区间≤10年）：2014-01-01~2016-08-13 + 2016-08-14~今，拼接去重。
输出 dyr_backtest_data.json：{code: [[date, cp, dyr, cvpos], ...]}，由 Actions 提交回仓库，
本地做「涨跌幅分位 × 股息率分位」双维度反转信号回测。
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

CST = timezone(timedelta(hours=8))
API = "https://open.lixinger.com/api/cn/index/fundamental"
CODES = ["000015", "000922", "H30269", "930955"]
OUT = "dyr_backtest_data.json"

TOKEN = os.environ.get("LIXINGER_TOKEN", "").strip()
if not TOKEN:
    print("[FATAL] 缺少 LIXINGER_TOKEN", file=sys.stderr)
    sys.exit(2)

today = datetime.now(CST).date().isoformat()
WINDOWS = [("2014-01-01", "2016-08-13"), ("2016-08-14", today)]

out = {}
for code in CODES:
    merged = {}
    for start, end in WINDOWS:
        for attempt in range(3):
            try:
                r = requests.post(API, json={
                    "token": TOKEN, "startDate": start, "endDate": end,
                    "stockCodes": [code],
                    "metricsList": ["cp", "dyr.ew", "dyr.y5.ew.cvpos"],
                }, timeout=60)
                r.raise_for_status()
                items = r.json().get("data")
                if not isinstance(items, list):
                    raise RuntimeError(str(r.json())[:200])
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  retry {code} {start}: {e}", file=sys.stderr)
                time.sleep(2)
        for it in items:
            d = datetime.fromisoformat(it["date"].replace("Z", "+00:00")) \
                .astimezone(CST).date().isoformat()
            cp = it.get("cp")
            if cp is None:
                continue
            merged[d] = [d, cp, it.get("dyr.ew"), it.get("dyr.y5.ew.cvpos")]
    rows = [merged[k] for k in sorted(merged)]
    out[code] = rows
    n_dyr = sum(1 for r in rows if r[2] is not None)
    n_pos = sum(1 for r in rows if r[3] is not None)
    print(f"{code}: {len(rows)} 天 {rows[0][0]}~{rows[-1][0]} dyr非空{n_dyr} cvpos非空{n_pos}",
          file=sys.stderr)

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
print(f"已写 {OUT}", file=sys.stderr)
