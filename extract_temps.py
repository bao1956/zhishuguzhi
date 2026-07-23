#!/usr/bin/env python3
"""扫描雪球大V估值图文件夹，新图用 claude -p 视觉提取指数温度 → temps.json → git push。

链路：archive_valuation.py(9:30 归档长图) → 本脚本(9:50/12:00 launchd) 提取新图温度
     → 提交 temps.json 到 GitHub → Actions 的 push_temps.py 用 Secret 写入 Google Sheet。
本地不持有 WEBHOOK_URL，GitHub Actions 是唯一写表入口。

幂等：temps_manifest.json（随仓库版本化）记录每张图的处理状态，重复跑不重提。
容错：claude 撞 session limit / 提取失败 → 该图保持未处理，下次运行自愈重试。
日期口径：一律用文件名日期（归档脚本已把望京博格帖标题里的数据日写进文件名）。

launchd: com.user.zhishuguzhi-temps，每天 09:50 与 12:00（二次兜底）。
部署: bash install_temps.sh（把仓库克隆到 $HOME/zhishuguzhi 跑，入口不能在 Downloads）。
"""

import json
import pathlib
import re
import shutil
import subprocess
import sys
import datetime as dt

HOME = pathlib.Path.home()
REPO = pathlib.Path(__file__).resolve().parent
BASE = HOME / "Library/Mobile Documents/com~apple~CloudDocs/季报和财报/4 信息源与观点/雪球大V"
TEMPS_FILE = REPO / "temps.json"
MANIFEST_FILE = REPO / "temps_manifest.json"
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_")

NAME_TO_CODE = {
    "沪深300": "SH000300", "中证500": "SH000905", "中证1000": "SH000852",
    "创业板指": "SZ399006", "创业板": "SZ399006",
    "上证红利": "SH000015", "中证红利": "SH000922", "红利低波": "CSIH30269",
}

PROMPT_61 = """Read 这张图片：{path}
这是雪球大V「六亿居士」的估值表长图，标题「"61"指数基金定投估值参考表(XXXX期)」，分区块（一、A股核心宽基 / 三、红利指数特色版块 / 四、板块指数 等），每行：指数名称 | 指数温度(℃) | PE | 百分位 | PB | 百分位 | 股息率 | ROE | 场内代码 | 场外代码。
提取以下 7 个行名的「指数温度」数值（℃前的数字）：
沪深300、中证500（不是中证A500/沪港深500/500低波动/500质量）、中证1000（都在区块一）；
创业板指（区块四，不是创业板50/创价值/创成长/科创创业50）；
上证红利、中证红利（不是300红利低波/深红利/央企红利）、红利低波（行名恰好「红利低波」，不是300红利低波/红利低波100/东证红利低波）（都在区块三）。
如果这张图不是该估值表，输出 {{"skip": true}}。
只输出一个 JSON 对象、不要任何其他文字：
{{"skip": false, "temps": {{"沪深300": 43.8, "中证500": 59.8, "中证1000": 46.5, "创业板指": 58.6, "上证红利": 61.3, "中证红利": 43.4, "红利低波": 44.4}}}}"""

PROMPT_WJBG = """Read 这张图片：{path}
这是雪球大V「望京博格」的「A股市场温度」长图：A股表约37行（每天按当日涨幅重排），列为 序号|行业名称|当日|"4*7"以来涨幅|"924"以来涨幅|PE|行业温度|相关标的，下方另有港股小表。
提取 A 股表中以下 4 个行名的「行业温度」列数值（形如 89.07% 的数，去掉%）——别取成 PE 或涨幅列：
沪深300（不是上证50/A500）、中证500、中证1000、创业板。
如果这张图不是「A股市场温度」表（如资金流向图/投票截图），输出 {{"skip": true}}。
只输出一个 JSON 对象、不要任何其他文字：
{{"skip": false, "temps": {{"沪深300": 89.07, "中证500": 88.51, "中证1000": 68.96, "创业板": 56.62}}}}"""

SOURCES = [
    {"name": "六亿居士", "folder": BASE / "六亿居士估值", "prompt": PROMPT_61,
     "expect": ["沪深300", "中证500", "中证1000", "创业板指", "上证红利", "中证红利", "红利低波"]},
    {"name": "望京博格", "folder": BASE / "望京博格估值", "prompt": PROMPT_WJBG,
     "expect": ["沪深300", "中证500", "中证1000", "创业板"]},
]


def log(*a):
    print(f"[{dt.datetime.now():%m-%d %H:%M:%S}]", *a, flush=True)


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"⚠️ {path.name} 解析失败: {e}")
    return default


def git(*args, check=True):
    r = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r


def claude_extract(prompt: str) -> dict | None:
    """调 claude -p 提取，返回解析后的 dict；失败返回 None（下次自愈重试）。"""
    claude_bin = shutil.which("claude") or str(HOME / ".local/bin/claude")
    try:
        r = subprocess.run(
            [claude_bin, "-p", prompt, "--allowedTools", "Read"],
            capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log("  ⚠️ claude 超时")
        return None
    if r.returncode != 0:
        log(f"  ⚠️ claude 退出码 {r.returncode}: {r.stderr.strip()[:200]}")
        return None
    text = r.stdout.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        log(f"  ⚠️ 输出里没有 JSON: {text[:200]}")
        return None
    try:
        return json.loads(m.group(0))
    except ValueError as e:
        log(f"  ⚠️ JSON 解析失败: {e}")
        return None


def validate(out: dict, expect: list) -> dict | None:
    """校验并转成 {code: temp}；不合格返回 None。"""
    temps = out.get("temps") or {}
    by_code = {}
    for name in expect:
        v = temps.get(name)
        if v is None:
            log(f"  ⚠️ 缺指标 {name}")
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            log(f"  ⚠️ {name} 非数值: {v!r}")
            return None
        if not (0 <= v <= 120):
            log(f"  ⚠️ {name}={v} 超出合理范围")
            return None
        by_code[NAME_TO_CODE[name]] = round(v, 2)
    if not by_code:
        return None
    if len(by_code) < len(expect):
        log(f"  ⚠️ 只提取到 {len(by_code)}/{len(expect)} 个指标（部分入库）")
    return by_code


def push_pending():
    """把本地领先的提交推上去（含上一轮 push 失败的遗留）；无事可推时静默成功。"""
    try:
        git("push", "origin", "main")
    except Exception as e:
        log(f"⚠️ git push 失败（下次运行重试）: {e}")
        return False
    return True


def main() -> int:
    try:
        git("pull", "--rebase", "--autostash")
    except Exception as e:
        log(f"⚠️ git pull 失败（继续用本地版本）: {e}")

    temps = load_json(TEMPS_FILE, {})
    manifest = load_json(MANIFEST_FILE, {})
    todo = []
    for src in SOURCES:
        if not src["folder"].exists():
            log(f"⚠️ 目录不存在: {src['folder']}")
            continue
        for f in sorted(src["folder"].iterdir()):
            if f.suffix.lower() in IMG_EXTS and f.name not in manifest:
                todo.append((src, f))

    if not todo:
        log("没有新图片")
        push_pending()
        return 0

    log(f"待处理 {len(todo)} 张新图")
    changed = 0
    for src, f in todo:
        m = DATE_RE.match(f.name)
        if not m:
            manifest[f.name] = {"status": "bad_name"}
            continue
        iso = m.group(1)
        log(f"提取 {src['name']} {f.name} ...")
        out = claude_extract(src["prompt"].format(path=str(f)))
        if out is None:
            continue  # 不写 manifest，下次重试
        if out.get("skip"):
            manifest[f.name] = {"status": "skip", "date": iso}
            log("  跳过（非目标表）")
            changed += 1
            continue
        by_code = validate(out, src["expect"])
        if by_code is None:
            continue  # 下次重试
        temps.setdefault(src["name"], {}).setdefault(iso, {}).update(by_code)
        manifest[f.name] = {"status": "ok", "date": iso, "n": len(by_code)}
        log(f"  ✅ {iso} {len(by_code)} 个指标")
        changed += 1

    if not changed:
        log("本轮没有成功提取，不提交")
        push_pending()
        return 1

    TEMPS_FILE.write_text(
        json.dumps(temps, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    MANIFEST_FILE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    try:
        git("add", "temps.json", "temps_manifest.json")
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            git("commit", "-m", f"大V温度自动提取 {dt.date.today().isoformat()}")
    except Exception as e:
        log(f"⚠️ git 提交失败（数据已落盘，下次运行重试）: {e}")
        return 1
    if push_pending():
        log("✅ 已 push，Actions 将写入 Sheet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
