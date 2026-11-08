"""creator-os 验收器。只读产品代码，只发请求，不改任何东西。

用法：
    python tools/verify.py            # 全跑
    python tools/verify.py --fast     # 跳过要调模型的项（V07 V08 V09 V10）
    python tools/verify.py --json-out docs/verify/_verify.json   # 另存一份机器可读结果

为什么要有这个文件：
    执行方说「做好了」不等于做好了。每一项都必须有一条能跑出数字的断言，
    数字不达标就是没做完，不许用「基本可用」「功能已通」代替。

退出码：全部 PASS 是 0，有任何 FAIL 是 1。
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8787"
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "creator.db"
PROMPTS = ROOT / "prompts"

RESULTS = []


def rec(code, name, ok, detail):
    RESULTS.append({"code": code, "name": name, "ok": bool(ok), "detail": detail})
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {code} {name} :: {detail}")


def dump_json(path, passed, total):
    """把 RESULTS 另存一份机器可读产物，供 run_all 汇总用。

    形状对齐 _redline.json 这一族：外层带 stage/ok，明细放 items；
    每条给 no / result / detail 三个字段（汇总方只消费这三个），
    额外带 name，因为 REPORT 的功能表要显示项目名，否则那列只能印编号。
    """
    if not path:
        return
    items = [
        {
            "no": r["code"],
            "name": r["name"],
            "result": "PASS" if r["ok"] else "FAIL",
            "detail": r["detail"],
        }
        for r in RESULTS
    ]
    Path(path).write_text(
        json.dumps(
            {
                "stage": "verify",
                "ok": total > 0 and passed == total,
                "passed": passed,
                "total": total,
                "items": items,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


def get(path, **kw):
    with httpx.Client(timeout=kw.pop("timeout", 20)) as c:
        return c.get(BASE + path, **kw)


def post(path, body, timeout=90):
    with httpx.Client(timeout=timeout) as c:
        r = c.post(BASE + path, json=body)
        return r.status_code, r.json()


# ---------------------------------------------------------------- 测试语料
# 每一段都刻意塞了一类硬痕迹。V03 要求逐类都能被体检抓出来。
AI_SMELL_TEXT = (
    "在当前数字化转型的大背景下，赋能一线团队、打通业务闭环、树立核心抓手，"
    "是一项至关重要、不可或缺的工作。研究表明，超过 80% 的企业都面临这一挑战。"
    "首先，我们要对齐目标；其次，要深耕底层链路；最后，要沉淀一套方法论。"
    "——这不仅是效率问题，更是认知升级。"
    "【核心观点】总而言之，让我们一起拥抱变化，赢得未来。"
)

# (痕迹名, 用来判断是否被抓到的正则)
TELLS = [
    ("破折号 ——", r"——"),
    ("无源权威 研究表明", r"研究表明"),
    ("段头【】", r"【"),
    ("首先其次最后", r"首先|其次|最后"),
    ("让我们 一起", r"让我们"),
    ("四字成语堆砌", r"至关重要|不可或缺|不可或缺"),
    ("段尾升华 未来", r"未来"),
]


def load_theses():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("SELECT id,title,evidence FROM theses ORDER BY id")]
    c.close()
    return rows


def ev_list(raw):
    try:
        v = json.loads(raw or "[]")
        return [x for x in v if isinstance(x, str) and x.strip()]
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--json-out", default="", help="把结果另存为 JSON（供 run_all 汇总）")
    args = ap.parse_args()

    # ---- V01 服务活着
    try:
        r = get("/api/stats", timeout=8)
        d = r.json()
        rec("V01", "服务存活 /api/stats", d.get("ok") is True,
            f"http {r.status_code} week_generated={d.get('data',{}).get('week_generated')}")
    except Exception as e:
        rec("V01", "服务存活 /api/stats", False, f"连不上：{type(e).__name__}: {e}")
        print("\n服务没起，后面的项没法验。先跑 启动.cmd。")
        # 服务没起也要落产物：否则汇总侧拿到 None，会把「没验」当成「没问题」
        dump_json(args.json_out, 0, len(RESULTS))
        return 1

    # ---- V02 风格样本接口
    try:
        d = get("/api/style").json()
        okd = d.get("data") or {}
        rec("V02", "GET /api/style", d.get("ok") is True and "sample" in okd,
            f"len={len(okd.get('sample') or '')} active={okd.get('active')}")
    except Exception as e:
        rec("V02", "GET /api/style", False, f"{type(e).__name__}: {e}")

    # ---- V03 体检对 7 类硬痕迹的检出率
    try:
        t0 = time.time()
        code, d = post("/api/check/express", {"text": AI_SMELL_TEXT}, timeout=70)
        el = time.time() - t0
        data = d.get("data") or {}
        blob = json.dumps(data.get("issues") or [], ensure_ascii=False)
        hit, miss = [], []
        for name, pat in TELLS:
            (hit if re.search(pat, blob) else miss).append(name)
        rec("V03", f"体检硬痕迹检出 {len(hit)}/{len(TELLS)}", len(hit) == len(TELLS),
            f"命中={hit} 漏={miss} total={data.get('total')} 耗时={el:.1f}s")
        rec("V04", "体检对 AI 味文本评分 < 60", (data.get("total") or 99) < 60,
            f"total={data.get('total')} issues={len(data.get('issues') or [])}")
    except Exception as e:
        rec("V03", "体检硬痕迹检出", False, f"{type(e).__name__}: {e}")
        rec("V04", "体检评分", False, f"{type(e).__name__}: {e}")

    # ---- V05 口播估时
    try:
        body = ("我改过十七版简历，最后一版只剩一行项目描述，面试邀约反而变多。"
                "室友把社团经历写了八条，投三十家只回了两个。这不是运气问题，"
                "是他在告诉对方他参加过什么，而不是他解决过什么。招聘的人一天看两百份，"
                "他只想确认你能不能把他手上的事接过去。所以每写一行，先问自己一句："
                "这行字能证明我解决过什么。写不出来的，删掉。") * 2
        code, d = post("/api/script/pacing", {"text": body, "wpm": 270}, timeout=20)
        data = d.get("data") or {}
        sec = data.get("estimated_seconds")
        chars = data.get("chars") or 1
        # 说话速度 4.5 字/秒，加上停顿，允许比裸算多 35%
        bare = chars / 4.5
        ok_sec = sec is not None and bare <= sec <= bare * 1.35
        rec("V05", "估时落在 裸算~裸算+35%", ok_sec,
            f"chars={chars} 裸算={bare:.1f}s 实际={sec}s")
    except Exception as e:
        rec("V05", "口播估时", False, f"{type(e).__name__}: {e}")

    # ---- V06 批量匹配速度
    try:
        t0 = time.time()
        code, d = post("/api/hot/match", {"limit": 20}, timeout=30)
        el = (time.time() - t0) * 1000
        data = d.get("data") or {}
        rec("V06", "批量匹配 <500ms 且无 model_error", el < 500 and "model_error" not in data,
            f"{el:.0f}ms count={data.get('count')} matched={data.get('matched')}")
    except Exception as e:
        rec("V06", "批量匹配", False, f"{type(e).__name__}: {e}")

    # ---- V07 单条 angle
    if not args.fast:
        try:
            c = sqlite3.connect(DB)
            tid = c.execute("SELECT id FROM topics ORDER BY id DESC LIMIT 1").fetchone()
            ths = load_theses()
            rich = next((t for t in ths if ev_list(t["evidence"])), None)
            c.close()
            if tid and rich:
                t0 = time.time()
                code, d = post(f"/api/hot/match/{tid[0]}/angle",
                               {"thesis_id": rich["id"]}, timeout=40)
                el = time.time() - t0
                ang = ((d.get("data") or {}).get("angle") or "").strip()
                rec("V07", "单条 angle <15s 且 8-60 字", el < 15 and 8 <= len(ang) <= 60,
                    f"{el:.1f}s len={len(ang)} angle={ang[:40]}")
            else:
                rec("V07", "单条 angle", False, "库里没有热点或有素材的母题")
        except Exception as e:
            rec("V07", "单条 angle", False, f"{type(e).__name__}: {e}")

    # ---- V08/V09 观点卡素材红线
    theses = load_theses()
    empty = next((t for t in theses if not ev_list(t["evidence"])), None)
    rich = next((t for t in theses if ev_list(t["evidence"])), None)
    card_empty = None
    if not args.fast:
        if empty:
            try:
                code, d = post("/api/gen/card", {"thesis_id": empty["id"]}, timeout=90)
                card_empty = (d.get("data") or {}).get("card") or d.get("data") or {}
                own = card_empty.get("own_detail")
                miss = card_empty.get("missing") or ""
                rec("V08", "空素材母题 own_detail 必须是空串", own == "",
                    f"thesis {empty['id']} own_detail={own!r} missing={miss!r}")
                rec("V12", "空素材时必须提示缺一手经历", "缺一手经历" in str(miss),
                    f"missing={miss!r}")
            except Exception as e:
                rec("V08", "空素材观点卡", False, f"{type(e).__name__}: {e}")
        else:
            rec("V08", "空素材观点卡", False, "库里没有空素材母题，无法验证")

        if rich:
            try:
                code, d = post("/api/gen/card", {"thesis_id": rich["id"]}, timeout=90)
                card = (d.get("data") or {}).get("card") or d.get("data") or {}
                own = (card.get("own_detail") or "").strip()
                evs = ev_list(rich["evidence"])
                verbatim = any(own and own in e for e in evs)
                rec("V09", "有素材母题 own_detail 逐字可回溯", bool(own) and verbatim,
                    f"thesis {rich['id']} own={own[:30]!r} 在素材原文里={verbatim}")
            except Exception as e:
                rec("V09", "有素材观点卡", False, f"{type(e).__name__}: {e}")

        # ---- V10 空素材派生不得编经历
        if card_empty and card_empty.get("id"):
            try:
                code, d = post("/api/gen/derive",
                               {"card_id": card_empty["id"], "forms": ["article"]}, timeout=120)
                blob = json.dumps(d.get("data") or {}, ensure_ascii=False)
                bad = [w for w in ("我见过", "我身边", "我一个朋友", "我以前", "我之前", "我认识")
                       if w in blob]
                rec("V10", "空素材派生文章不含编造经历", not bad, f"命中禁词={bad}")
            except Exception as e:
                rec("V10", "空素材派生", False, f"{type(e).__name__}: {e}")

    # ---- V11 假数据已清
    try:
        c = sqlite3.connect(DB)
        n_posts = c.execute("SELECT count(*) FROM posts").fetchone()[0]
        n_rev = c.execute("SELECT count(*) FROM reviews").fetchone()[0]
        c.close()
        rec("V11", "posts 表已清空", n_posts == 0, f"posts={n_posts} reviews={n_rev}")
    except Exception as e:
        rec("V11", "posts 表检查", False, f"{type(e).__name__}: {e}")

    # ---- V13 三个生成提示词都带反编造与具体性界线
    try:
        marks = []
        for f in ("article.md", "note.md", "script.md"):
            t = (PROMPTS / f).read_text(encoding="utf-8")
            okc = ("编造" in t or "不许编" in t) and "具体" in t
            marks.append((f, okc))
        rec("V13", "三个生成提示词都带反编造+具体性界线", all(x[1] for x in marks),
            str(dict(marks)))
    except Exception as e:
        rec("V13", "提示词检查", False, f"{type(e).__name__}: {e}")

    # ---- 汇总
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["ok"])
    print("\n" + "=" * 60)
    print(f"总计 {passed}/{total} PASS")
    fails = [r["code"] for r in RESULTS if not r["ok"]]
    if fails:
        print("未通过：" + " ".join(fails))
    print("=" * 60)

    out = ROOT / "tools" / "verify_report.json"
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已写入 {out}")

    dump_json(args.json_out, passed, total)
    if args.json_out:
        print(f"汇总产物已写入 {args.json_out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
