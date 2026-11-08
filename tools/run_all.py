# -*- coding: utf-8 -*-
"""一键验收编排：preflight → 红线 → 功能 → 前端 → 模块验收，汇总写 docs/verify/REPORT.md。

只写 tools/ tests/ docs/verify/ 三个目录，不碰任何产品代码。
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = r"C:\Users\32719\.workbuddy\binaries\python\versions\3.13.12\python.exe"
TOOLS = BASE / "tools"
TESTS = BASE / "tests"
OUT_DIR = BASE / "docs" / "verify"
REPORT = OUT_DIR / "REPORT.md"

MAX_EVIDENCE = 200

# 五个验收阶段，顺序即执行顺序
STAGES = ("preflight", "redline", "verify", "frontend", "modules")
STAGE_LABELS = {
    "preflight": "环境自检",
    "redline": "红线",
    "verify": "功能",
    "frontend": "前端",
    "modules": "模块验收",
}

# 失败项的人工归纳：严重程度、复现命令、期望
PROBLEM_META = {
    ("redline", 5): {
        "severity": "严重",
        "title": "空素材母题派生出的文章/口播里编造了第一人称经历",
        "repro": ('curl -s -X POST http://127.0.0.1:8787/api/gen/card '
                  '-H "Content-Type: application/json" -d \'{"thesis_id":1}\'  '
                  '# 取返回的 id，再：\n    curl -s -X POST http://127.0.0.1:8787/api/gen/derive '
                  '-H "Content-Type: application/json" -d \'{"card_id":<上一步id>,"forms":["article","script"]}\''),
        "expect": "派生文本中不得出现「我见过 / 我身边 / 我一个朋友 / 我以前 / 我之前 / 我认识 / 我遇到 / 我的经历」",
    },
    ("verify", "6"): {
        "severity": "严重",
        "title": "热点挂载接口的返回结构不符合契约，且 matches 数量不足 3 个",
        "repro": ('curl -s -X POST http://127.0.0.1:8787/api/hot/match '
                  '-H "Content-Type: application/json" -d \'{"limit":20}\''),
        "expect": '返回顶层数组 [{topic_id,title,matches:[3 个 {thesis_id,thesis_title,score,angle,has_evidence}]}]',
    },
    ("verify", "3.3"): {
        "severity": "中",
        "title": "POST /api/gen/script 生成口播失败，前端点「生成口播」会拿不到内容",
        "repro": ('curl -s -X POST http://127.0.0.1:8787/api/gen/script '
                  '-H "Content-Type: application/json" -d \'{"topic":"简历"}\''),
        "expect": '{"ok":true,"data":{... 含 hook 与 segments ...}}',
    },
    ("verify", "12"): {
        "severity": "严重",
        "title": "文章/笔记接口把编造的第一人称经历写进了生成内容（同一红线在老接口上没被拦）",
        "repro": ('curl -s -X POST http://127.0.0.1:8787/api/gen/article '
                  '-H "Content-Type: application/json" -d \'{"keyword":"简历"}\''),
        "expect": "生成文本不得出现「我见过 / 我身边 / 我以前」等第一人称经历搭配",
    },
    ("modules", "8"): {
        "severity": "严重",
        "title": "四模块仍在读 MOCK 硬编码，或 LATER_MODULES 未解禁",
        "repro": "python tools/verify_modules.py",
        "expect": "MOCK 五字段引用=0 且 MOCK 定义里五键=0；LATER_MODULES 不含 market/books/replicate",
    },
    ("modules", "9"): {
        "severity": "中",
        "title": "app.py 生成成功路径的扣分挂载静态断言未通过",
        "repro": "python tools/verify_modules.py",
        "expect": "db.points_change( 出现 ≥6 次且存在 POINT_COST 常量",
    },
}

GENERIC = {
    "severity": "中",
    "title": "",
    "repro": "",
    "expect": "",
}


def clip(s, n=MAX_EVIDENCE):
    s = str(s).replace("\n", " ").replace("|", "\\|")
    return s if len(s) <= n else s[:n] + " …（截断）"


def run_stage(name, cmd, extra):
    print()
    print("#" * 70)
    print(f"# 阶段：{name}")
    print("#" * 70)
    proc = subprocess.run(cmd + extra, cwd=str(BASE),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace")
    sys.stdout.write(proc.stdout or "")
    sys.stdout.flush()
    return proc.returncode


def load(name):
    """读阶段产物，并把明细行统一成 rows（no / name / result / evidence）。

    redline 写 asserts/evidence，preflight 与 verify 写 items/detail，frontend 写 rows。
    报告只认一种形状，所以在读盘这一处对齐一次，别在每个渲染点写兼容分支。
    """
    p = OUT_DIR / f"_{name}.json"
    if not p.exists():
        return None
    try:
        stage = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(stage, dict) or stage.get("rows"):
        return stage
    items = stage.get("items")
    # preflight 的 items 没有 no 字段，不做转换，保持原样
    if isinstance(items, list) and items and all(isinstance(i, dict) and "no" in i for i in items):
        stage["rows"] = [
            {
                "no": i.get("no"),
                "name": i.get("name") or str(i.get("no")),
                "result": i.get("result"),
                "evidence": i.get("evidence") or i.get("detail") or "",
                "detail": i.get("detail") or "",
            }
            for i in items
        ]
    return stage


def rows_of(stage):
    """取阶段产物的明细行，取不到就给空列表，不抛异常。"""
    return (stage or {}).get("rows") or []


def decide(results):
    """把四个阶段的产物收敛成唯一一份判定。

    报告里的总判定、阶段结论表、进程退出码，全部只读这个函数的返回值。
    以前是两套算法：报告按「有没有 FAIL 行」算，退出码按「有没有阶段 rc != 0」算，
    于是会出现报告写着 PASS、进程却退 1（或反过来）的自相矛盾。
    现在只有这里说了算：verdict 是 PASS 就退 0，是 FAIL 就退 1。
    """
    pre, red, ver, fe, mod = (results.get(k) for k in STAGES)

    fails, blocked = [], []
    for r in rows_of(ver):
        if r["result"] in ("FAIL", "NOT_IMPLEMENTED"):
            fails.append(("verify", str(r["no"]), r))
        elif r["result"] == "BLOCKED":
            blocked.append(("verify", str(r["no"]), r))
    for r in rows_of(fe):
        if r["result"] == "FAIL":
            fails.append(("frontend", str(r["no"]), r))
    # modules 是新增阶段，它的 FAIL 必须计入总判定，不能因为新增就被漏掉
    for r in rows_of(mod):
        if r["result"] in ("FAIL", "NOT_IMPLEMENTED"):
            fails.append(("modules", str(r["no"]), r))

    red_bad = [a for a in (red or {}).get("asserts", []) if a["result"] == "FAIL"]
    pre_bad = [i for i in (pre or {}).get("items", []) if i["result"] == "FAIL"]

    # 阶段跑了却没留下任何结果（脚本崩了、参数没传、JSON 写坏）：不能当成「没问题」。
    # 判定不再看 rc 之后，这个缺口必须显式记下来，否则崩溃会被读成通过。
    gaps = []
    for k in STAGES:
        rc = (results.get("_rc") or {}).get(k)
        if rc is None:
            continue  # --skip-long 跳过的阶段不算失败
        stage = results.get(k) or {}
        if not (stage.get("rows") or stage.get("asserts") or stage.get("items")):
            gaps.append({"stage": k, "rc": rc})

    total_fail = bool(
        red_bad or fails or pre_bad or blocked or (red or {}).get("failures") or gaps
    )

    # 每个阶段自己是否算「未通过」，阶段结论表也读这个，避免又长出一套算法
    stage_bad = {f[0] for f in fails} | {b[0] for b in blocked} | {g["stage"] for g in gaps}
    if red_bad or (red or {}).get("failures"):
        stage_bad.add("redline")
    if pre_bad:
        stage_bad.add("preflight")

    return {
        "verdict": "FAIL" if total_fail else "PASS",
        "fails": fails,
        "blocked": blocked,
        "red_bad": red_bad,
        "pre_bad": pre_bad,
        "gaps": gaps,
        "stage_bad": stage_bad,
    }


def build_report(results):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    pre, red, ver, fe, mod = (results.get(k) for k in STAGES)

    d = decide(results)
    verdict = d["verdict"]
    fails, blocked, red_bad = d["fails"], d["blocked"], d["red_bad"]
    gaps = d["gaps"]

    L = []
    L.append(f"# 验收报告 {now}")
    L.append("")
    L.append("## 总判定")
    L.append("")
    L.append(f"**{verdict}**")
    L.append("")

    # 红线
    L.append("## 红线")
    L.append("")
    L.append("| 断言 | 结果 | 证据 |")
    L.append("| --- | --- | --- |")
    asserts = (red or {}).get("asserts") or []
    if not asserts:
        hist = (results.get("_historical") or {}).get("redline")
        if hist:
            L.append(f"| — | 沿用历史产物（{hist}） | 本轮未重跑；证据来源 _redline.json |")
        else:
            L.append("| — | 历史产物缺失 | 本轮未重跑红线；docs/verify/_redline.json 不存在 |")
    else:
        for a in asserts:
            L.append(f"| 断言 {a['no']}：{a['assert']} | {a['result']} | {clip(a['evidence'])} |")
    errs = (red or {}).get("failures") or []
    for e in errs:
        if not any(f"断言" in str(a.get("no")) for a in asserts):
            L.append(f"| 用例异常 | FAIL | {clip(e.get('case'))}：{clip(e.get('msg'))} |")
    L.append("")

    # 功能
    L.append("## 功能验收")
    L.append("")
    L.append("| # | 项目 | 结果 | 证据 |")
    L.append("| --- | --- | --- | --- |")
    rows = (ver or {}).get("rows") or []
    if not rows:
        hist = (results.get("_historical") or {}).get("verify")
        if hist:
            L.append(f"| — | 沿用历史产物（{hist}） | — | 本轮未重跑；证据来源 _verify.json |")
        else:
            L.append("| — | 历史产物缺失 | — | 本轮未重跑功能验收；docs/verify/_verify.json 不存在 |")
    for r in rows:
        L.append(f"| {r['no']} | {r['name']} | {r['result']} | {clip(r['evidence'])} |")
    L.append("")

    # 前端
    L.append("## 前端静态检查")
    L.append("")
    L.append("| # | 项目 | 结果 | 说明 |")
    L.append("| --- | --- | --- | --- |")
    frows = (fe or {}).get("rows") or []
    if not frows:
        L.append("| — | 未执行 | — | 前端脚本未产出结果 |")
    for r in frows:
        L.append(f"| {r['no']} | {r['name']} | {r['result']} | {clip(r['detail'])} |")
    L.append("")

    # 模块验收（verify_modules.py 的 9 项）
    L.append("## 模块验收")
    L.append("")
    L.append("| # | 项目 | 结果 | 证据 |")
    L.append("| --- | --- | --- | --- |")
    mrows = (mod or {}).get("rows") or []
    if not mrows:
        L.append("| — | 无结果 | — | 模块验收脚本未产出结果 |")
    for r in mrows:
        L.append(f"| {r['no']} | {r['name']} | {r['result']} | {clip(r.get('evidence') or r.get('detail'))} |")
    L.append("")

    # 问题
    L.append("## 发现的问题（按严重程度排序）")
    L.append("")
    order = {"严重": 0, "中": 1, "轻": 2, "环境": 3}
    entries = []
    for a in red_bad:
        meta = PROBLEM_META.get(("redline", a["no"]), GENERIC)
        entries.append({
            "severity": meta.get("severity", "中"),
            "title": meta.get("title") or f"红线断言 {a['no']} 未通过：{a['assert']}",
            "repro": meta.get("repro", ""),
            "expect": meta.get("expect", ""),
            "actual": a["evidence"],
        })
    for stage, no, r in fails:
        meta = PROBLEM_META.get((stage, no), GENERIC)
        entries.append({
            "severity": meta.get("severity", "中"),
            "title": meta.get("title") or f"{r['name']} 未通过",
            "repro": meta.get("repro", ""),
            "expect": meta.get("expect", ""),
            "actual": r.get("evidence") or r.get("detail"),
        })
    for stage, no, r in blocked:
        entries.append({
            "severity": "环境",
            "title": f"{r['name']}：模型上游不可用，本项无法验证（不计入产品缺陷）",
            "repro": PROBLEM_META.get((stage, no), {}).get("repro", ""),
            "expect": "app 进程到智谱接口的连接正常（必要时重启服务，进程内连接池会僵死）",
            "actual": r.get("evidence"),
        })
    for g in gaps:
        label = STAGE_LABELS.get(g["stage"], g["stage"])
        entries.append({
            "severity": "环境",
            "title": f"{label}阶段跑了但没有留下可读结果，本阶段按未通过计",
            "repro": f'python tools/{g["stage"]}.py --json-out docs/verify/_{g["stage"]}.json',
            "expect": "阶段脚本正常退出并写出 _<stage>.json（含 asserts 或 rows/items）",
            "actual": f"退出码 {g['rc']}，产物缺失或为空",
        })
    entries.sort(key=lambda e: order.get(e["severity"], 3))

    if not entries:
        L.append("本轮没有 FAIL 项。")
    for i, e in enumerate(entries, 1):
        L.append(f"{i}. 【{e['severity']}】{e['title']}")
        if e["repro"]:
            L.append(f"   - 复现命令：`{e['repro']}`")
        if e["expect"]:
            L.append(f"   - 期望：{e['expect']}")
        L.append(f"   - 实际：{clip(e['actual'])}")
    L.append("")

    # 无法验证
    L.append("## 无法验证的部分")
    L.append("")
    L.append("1. supports 的因果性：脚本只核对条数（2-3），「因为」后面是否真的构成因果/对比，需要人读一遍。")
    L.append("2. 页面视觉合理性：index.html 各页面的布局、配色、留白、移动端断点，静态检查无法判断。")
    L.append("3. 热点真实性：只能判断标题不重复、无占位词、带 platform，无法确认抓的是当下真实榜位。")
    L.append("4. CSS :root 由 check_frontend.py 第 4 项在 index.html 自身检查（:root 段含 --green: 变量），不再依赖备份目录。")
    L.append("5. .py 的作者归属：文件修改时间只能证明「有没有在 120 秒内交叉写入」，不能证明是哪个工程师改的。")
    L.append("6. 提词器的实际滚动与空格暂停效果：需要真人对着屏幕按一次空格才能确认。")
    L.append("7. LLM 输出的稳定性：同一接口多次调用结果不同，本轮 PASS 不代表下一轮还 PASS（本轮红线断言 5 即为一次随机命中）。")
    L.append("")

    # 阶段执行情况：退出码是原始证据，结论按 decide() 的口径给，
    # 免得出现「总判定 PASS，某个阶段结论未通过」这种自己打自己的表。
    # 未重跑阶段必须写明证据来源与日期，让人一眼看出不是本轮跑的。
    L.append("## 阶段执行情况")
    L.append("")
    L.append("| 阶段 | 退出码 | 结论 |")
    L.append("| --- | --- | --- |")
    for k in STAGES:
        label = STAGE_LABELS[k]
        rc = (results.get("_rc") or {}).get(k)
        if rc is None:
            hist = (results.get("_historical") or {}).get(k)
            stage = results.get(k) or {}
            has_rows = bool(stage.get("rows") or stage.get("asserts") or stage.get("items"))
            if hist and has_rows:
                L.append(f"| {label} | 沿用历史产物（{hist}，来源 _{k}.json） | "
                         f"{'未通过' if k in d['stage_bad'] else '通过（历史）'} |")
            else:
                L.append(f"| {label} | 历史产物缺失（本轮未重跑） | 未验证 |")
        else:
            bad = k in d["stage_bad"]
            L.append(f"| {label} | {rc} | {'未通过' if bad else '通过'} |")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-long", action="store_true",
                    help="跳过红线与功能（跑自检、前端、模块验收；模块验收不依赖 LLM 仍会执行）")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 只删本次会重跑的阶段产物。--skip-long 未执行的 redline/verify 旧产物必须保留，
    # 报告里会明确标注「沿用历史产物（日期/来源）」或「历史产物缺失」。
    will_run = {"preflight", "frontend", "modules"}
    if not args.skip_long:
        will_run |= {"redline", "verify"}
    for k in will_run:
        p = OUT_DIR / f"_{k}.json"
        if p.exists():
            p.unlink()

    print("=" * 70)
    print(f"creator-os 独立验收  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"项目：{BASE}")
    print(f"解释器：{sys.executable}")
    print("=" * 70)

    results = {"_rc": {}, "_historical": {}}

    rc = run_stage("preflight", [PY, str(TOOLS / "preflight.py")],
                   ["--json-out", str(OUT_DIR / "_preflight.json")])
    results["_rc"]["preflight"] = rc
    results["preflight"] = load("preflight")

    if rc == 2:
        print()
        print("服务未启动，后续阶段无意义，直接出报告。")
        for k in ("redline", "verify", "frontend", "modules"):
            results["_rc"][k] = None
        md = build_report(results)
        REPORT.write_text(md, encoding="utf-8")
        print(f"\n报告已写入 {REPORT}")
        print(f"总判定：{decide(results)['verdict']}（退出码 2：环境不通，区别于产品缺陷的 1）")
        sys.exit(2)

    if not args.skip_long:
        results["_rc"]["redline"] = run_stage(
            "redline", [PY, str(TESTS / "test_redline.py")],
            ["--json-out", str(OUT_DIR / "_redline.json")])
        results["redline"] = load("redline")

        results["_rc"]["verify"] = run_stage(
            "verify", [PY, str(TOOLS / "verify.py")],
            ["--json-out", str(OUT_DIR / "_verify.json")])
        results["verify"] = load("verify")
    else:
        results["_rc"]["redline"] = None
        results["_rc"]["verify"] = None
        # 未重跑阶段沿用历史产物（若存在），报告里标注日期与来源；缺失就如实写缺失
        results["redline"] = load("redline")
        results["verify"] = load("verify")
        for k in ("redline", "verify"):
            p = OUT_DIR / f"_{k}.json"
            results["_historical"][k] = (
                datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                if p.exists() else None
            )

    results["_rc"]["frontend"] = run_stage(
        "frontend", [PY, str(TOOLS / "check_frontend.py")],
        ["--json-out", str(OUT_DIR / "_frontend.json")])
    results["frontend"] = load("frontend")

    # modules 阶段：verify_modules.py 固定写 docs/verify/_modules.json（不支持 --json-out），
    # 按固定产物路径调用；--skip-long 时仍执行（不依赖 LLM）。
    results["_rc"]["modules"] = run_stage(
        "modules", [PY, str(TOOLS / "verify_modules.py")], [])
    results["modules"] = load("modules")

    md = build_report(results)
    REPORT.write_text(md, encoding="utf-8")

    # 退出码直接复用报告里算好的 verdict，不再另算一遍。
    # 以前这里是 any(rc != 0)，和报告各算一套，才会出现「打印 PASS 却退 1」。
    d = decide(results)
    exit_code = 0 if d["verdict"] == "PASS" else 1

    print()
    print("=" * 70)
    print(f"报告已写入 {REPORT}")
    print(f"总判定：{d['verdict']}（退出码 {exit_code}，与报告同一份判定）")
    print("=" * 70)

    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
