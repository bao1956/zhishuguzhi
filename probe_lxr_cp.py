"""临时探测（用完即删）：理杏仁开放平台是否提供指数收盘价。

候选：
  1) cn/index/fundamental 的 metricsList 加 "cp"（收盘点位）/"cpc"（涨跌幅）
  2) cn/index/candlestick K线接口（若存在）
对 8 个指数逐一验证覆盖与历史深度（2023-08-14 起 3 年）。
结果打到 Actions 日志（token 由 GitHub 自动掩码，不打印）。
"""

import json
import os
import sys

import requests

TOKEN = os.environ.get("LIXINGER_TOKEN", "").strip()
if not TOKEN:
    print("[FATAL] 无 LIXINGER_TOKEN")
    sys.exit(1)

CODES = ["000300", "000905", "000852", "399006", "000015", "000922", "H30269", "930955"]


def show(tag, payload):
    s = json.dumps(payload, ensure_ascii=False)
    print(f"  {tag}: {s[:400]}")


# 候选1: fundamental + cp
print("=== 候选1: cn/index/fundamental metricsList=['cp','cpc'] ===")
for code in CODES:
    try:
        r = requests.post("https://open.lixinger.com/api/cn/index/fundamental", json={
            "token": TOKEN,
            "startDate": "2026-08-05",
            "endDate": "2026-08-14",
            "stockCodes": [code],
            "metricsList": ["cp", "cpc"],
        }, timeout=30)
        j = r.json()
        data = j.get("data")
        if isinstance(data, list) and data:
            show(f"{code} OK n={len(data)}", data[-1])
        else:
            show(f"{code} EMPTY http={r.status_code}", j)
    except Exception as e:
        print(f"  {code} ERR {e}")

# 候选1b: 历史深度验证（只测 399006 与 930955）
print("=== 候选1b: 2023-08-14 起历史深度 ===")
for code in ["399006", "930955"]:
    try:
        r = requests.post("https://open.lixinger.com/api/cn/index/fundamental", json={
            "token": TOKEN,
            "startDate": "2023-08-14",
            "endDate": "2026-08-14",
            "stockCodes": [code],
            "metricsList": ["cp"],
        }, timeout=60)
        j = r.json()
        data = j.get("data")
        if isinstance(data, list) and data:
            ds = sorted(it["date"] for it in data)
            n_cp = sum(1 for it in data if it.get("cp") is not None)
            print(f"  {code}: n={len(data)} cp非空={n_cp} 首={ds[0][:10]} 末={ds[-1][:10]}")
            show(f"{code} 首样本", data[0])
        else:
            show(f"{code} EMPTY http={r.status_code}", j)
    except Exception as e:
        print(f"  {code} ERR {e}")

# 候选2: candlestick K线
print("=== 候选2: cn/index/candlestick ===")
for code in ["000300", "399006"]:
    for body in [
        {"token": TOKEN, "stockCode": code, "startDate": "2026-08-05",
         "endDate": "2026-08-14", "type": "normal"},
        {"token": TOKEN, "stockCodes": [code], "startDate": "2026-08-05",
         "endDate": "2026-08-14"},
    ]:
        try:
            r = requests.post("https://open.lixinger.com/api/cn/index/candlestick",
                              json=body, timeout=30)
            j = r.json()
            keys = "stockCode" if "stockCode" in body else "stockCodes"
            data = j.get("data")
            if isinstance(data, list) and data:
                show(f"{code}({keys}) OK n={len(data)}", data[-1])
            else:
                show(f"{code}({keys}) http={r.status_code}", j)
        except Exception as e:
            print(f"  {code} ERR {e}")
print("=== 探测结束 ===")
