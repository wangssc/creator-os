# -*- coding: utf-8 -*-
"""前端静态检查：不启动浏览器，只做能确定的部分。

结果集：PASS / FAIL / SKIP（SKIP 必须有理由，且不计入 PASS）
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
INDEX = BASE / "static" / "index.html"
TELE = BASE / "static" / "teleprompter.html"
BACKUP_ROOT = BASE.parent / "creator-os.backup"
BACKUP_INDEX = BACKUP_ROOT / "static" / "index.html"

ROWS = []


def item(no, name, result, detail):
    ROWS.append({"no": no, "name": name, "result": result, "detail": detail})
    print(f"[{result:5}] {no}. {name}")
    print(f"        {detail[:300]}")


def read(p):
    return p.read_text(encoding="utf-8", errors="replace")


def strip_comments(t):
    """剥离 /* */ 块注释、<!-- --> 与行首 // 注释，避免把文档说明当成代码判定。"""
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"(?m)^\s*//.*$", "", t)
    return t


def extract_root_css(text):
    m = re.search(r":root\s*\{([^}]*)\}", text, re.S)
    if not m:
        return None
    body = m.group(1)
    decls = []
    for line in body.split(";"):
        line = " ".join(line.split())
        if line:
            decls.append(line)
    return sorted(decls)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    print("=" * 70)
    print("前端静态检查 check_frontend")
    print("=" * 70)

    if not INDEX.exists():
        item(0, "static/index.html 存在", "FAIL", f"找不到 {INDEX}")
        _finish(args)
        return

    html = read(INDEX)
    code = strip_comments(html)

    # 1 pageCard
    hits = re.findall(r"function\s+pageCard\s*\(", code)
    item(1, "index.html 存在 pageCard 函数", "PASS" if hits else "FAIL",
         f"剥离注释后匹配 {len(hits)} 处；index.html {len(html)} 字符")

    # 2 MOCK_MODE 默认 false（只看真实赋值，忽略注释里的说明文字）
    assign = re.findall(r"(?:var|let|const)?\s*MOCK_MODE\s*=\s*(true|false)\s*[;\n]", code)
    on = re.findall(r"MOCK_MODE\s*=\s*true\s*[;\n]", code)
    commented_only = bool(re.search(r"MOCK_MODE\s*=\s*true", html)) and not on
    ok = bool(assign) and assign[0].lower() == "false" and not on
    note = "（注释里提到 MOCK_MODE = true，但不在代码中，不计为违规）" if commented_only else ""
    item(2, "MOCK_MODE 默认值为 false", "PASS" if ok else "FAIL",
         f"实际赋值={assign} 代码中 MOCK_MODE = true 次数={len(on)}{note}")

    # 3 teleprompter + 空格暂停
    if not TELE.exists():
        item(3, "teleprompter.html 存在且含空格暂停逻辑", "FAIL", f"找不到 {TELE}")
    else:
        t = read(TELE)
        has_space = ("Space" in t) or ('key === " "' in t) or ("key === ' '" in t)
        has_pause = ("pause" in t.lower()) or ("暂停" in t)
        ok3 = has_space and has_pause
        item(3, "teleprompter.html 存在且含空格暂停逻辑", "PASS" if ok3 else "FAIL",
             f"{len(t)} 字符；空格监听={has_space} 暂停逻辑={has_pause}")

    # 4 CSS :root 段存在且含 --green 变量（改为在 index.html 自身检查，不再依赖备份）
    cur_css = extract_root_css(html)
    green_hit = [d for d in (cur_css or []) if re.match(r"^--green\s*:", d)]
    ok4 = cur_css is not None and bool(green_hit)
    item(4, "CSS 变量段（:root）存在且含 --green: 变量", "PASS" if ok4 else "FAIL",
         f"index.html 自身 :root 解析={cur_css is not None} 声明数={len(cur_css or [])}；"
         f"精确命中 --green: 声明={green_hit[:3]}（原备份对比无备份即 SKIP，已收敛为自身检查）")

    # 5 .py 是否被前端改过（时间线推断）
    pys = sorted([p for p in BASE.rglob("*.py")
                  if "__pycache__" not in p.parts
                  and "tools" not in p.parts and "tests" not in p.parts],
                 key=lambda p: p.stat().st_mtime)
    fronts = [p for p in (INDEX, TELE) if p.exists()]
    front_times = [(p, datetime.fromtimestamp(p.stat().st_mtime)) for p in fronts]
    py_times = [(p, datetime.fromtimestamp(p.stat().st_mtime)) for p in pys]

    cross = []
    for p, t in py_times:
        for fp, ft in front_times:
            if abs((t - ft).total_seconds()) <= 120:
                cross.append(f"{p.name}@{t:%H:%M:%S} ≈ {fp.name}@{ft:%H:%M:%S}")

    timeline = "；".join(f"{p.name}@{t:%H:%M:%S}" for p, t in py_times)
    front_line = "；".join(f"{p.name}@{t:%H:%M:%S}" for p, t in front_times)
    detail = f"后端：{timeline} ｜ 前端：{front_line}"
    if cross:
        item(5, "没有 .py 文件被前端改动过", "FAIL",
             f"以下 .py 与前端文件修改时间相差 ≤120 秒，疑似交叉写入：{cross}；{detail}")
    else:
        item(5, "没有 .py 文件被前端改动过", "PASS",
             f"无 .py 与前端文件在 120 秒内同时修改（注意：时间线只能证否，不能证明作者）；{detail}")

    # 6 四模块不再读 MOCK 硬编码（防回退，挂在 _finish 之前）
    # 判定升级：引用为 0 且 MOCK 对象定义里也不再含这五个键（死数据一并禁掉）
    five = re.findall(r"MOCK\.(tools|books|bookCats|points|replicate)", code)
    m_later = re.search(r"var\s+LATER_MODULES\s*=\s*\{([^}]*)\}", code, re.S)
    later_body = m_later.group(1) if m_later else ""
    later_keys = re.findall(r"(market|books|replicate)\s*:", later_body)
    # 取 MOCK 对象定义体（var MOCK = { 到顶格 };），只认两空格缩进的顶层键
    m_mock = re.search(r"var\s+MOCK\s*=\s*\{", code)
    mock_body = ""
    if m_mock:
        rest = code[m_mock.end():]
        m_end = re.search(r"(?m)^\};", rest)
        if m_end:
            mock_body = rest[:m_end.start()]
    def_hits = re.findall(r"(?m)^\s{2}(tools|books|bookCats|points|replicate)\s*:", mock_body)
    ok6 = len(five) == 0 and len(later_keys) == 0 and len(def_hits) == 0
    item(6, "四模块不再读 MOCK 硬编码", "PASS" if ok6 else "FAIL",
         f"剥离注释后引用计数={len(five)} 命中={five}；"
         f"MOCK 对象定义命中={len(def_hits)} 命中={def_hits}（定义命中=0 才 PASS）；"
         f"LATER_MODULES 残留 market/books/replicate 键={later_keys}")

    _finish(args)


def _finish(args):
    fails = [r for r in ROWS if r["result"] == "FAIL"]
    skips = [r for r in ROWS if r["result"] == "SKIP"]
    print("-" * 70)
    print(f"前端检查：{len(ROWS) - len(fails) - len(skips)} PASS / {len(fails)} FAIL / {len(skips)} SKIP")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"stage": "frontend", "rows": ROWS,
             "fail_count": len(fails), "skip_count": len(skips),
             "ok": not fails}, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
