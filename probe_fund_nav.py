#!/usr/bin/env python3
"""临时探测：用 LIXINGER_TOKEN 拉基金净值（014987 华安产业趋势混合A）。

跑在 GitHub Actions 里（token 只在 secret），结果落 fund_nav_result.json 走 artifact 带回。
用完连分支一起删。
"""
import datetime
import gzip
import json
import os
import urllib.request

TOKEN = os.environ["LIXINGER_TOKEN"]
BASE = "https://open.lixinger.com/api/"


def post(path, payload):
    payload = {"token": TOKEN, **payload}
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        return {"httpError": e.code, "body": e.read().decode()[:800]}
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)}


today = datetime.date.today().isoformat()
probes = {
    "netvalue_codes": ("cn/fund/net-value",
                       {"stockCodes": ["014987"], "startDate": "2022-08-01", "endDate": today}),
    "netvalue_code": ("cn/fund/net-value",
                      {"stockCode": "014987", "startDate": "2022-08-01", "endDate": today}),
    "candle": ("cn/fund/candlestick",
               {"stockCode": "014987", "startDate": "2022-08-01", "endDate": today, "type": "normal"}),
    "fund_basic": ("cn/fund", {"stockCodes": ["014987"]}),
}

out = {}
for key, (path, payload) in probes.items():
    out[key] = post(path, payload)

with open("fund_nav_result.json", "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

for key, v in out.items():
    data = v.get("data") if isinstance(v, dict) else None
    n = len(data) if isinstance(data, list) else data
    print(key, "->", v.get("code") if isinstance(v, dict) else "?", "rows:", n if isinstance(n, int) else str(n)[:100])
