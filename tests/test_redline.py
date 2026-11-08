# -*- coding: utf-8 -*-
"""红线测试：素材为空的母题，任何生成结果都不许出现第一人称经历。

只用标准库。运行为 unittest，输出明确 PASS / FAIL，不产生"部分通过"。
"""
import argparse
import json
import re
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:8787"
TIMEOUT_GEN = 90
TIMEOUT_DERIVE = 420

ASSERTS = []
CARD_EMPTY_ID = None

BANNED = ["我见过", "我身边", "我一个朋友", "我以前", "我之前", "我认识", "我遇到", "我的经历"]

PUNC = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()《》〈〉…—\-·~`|]+")


def norm(s):
    return PUNC.sub("", s or "")


def split_sentences(s):
    parts = re.split(r"[。！？；\n]+", s or "")
    return [p.strip() for p in parts if p.strip()]


def call(method, path, payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:400]}


def record(idx, claim, result, evidence):
    ASSERTS.append({"no": idx, "assert": claim, "result": result, "evidence": evidence})
    print(f"  [断言{idx}] {result} — {claim}")
    if evidence:
        print(f"           证据：{evidence[:400]}")


class RedlineTest(unittest.TestCase):
    empty_id = None
    rich_id = None
    rich_evidence = None
    health = None
    theses = None
    card_rich = None

    @classmethod
    def setUpClass(cls):
        status, body = call("GET", "/api/theses/health")
        assert status == 200 and body.get("ok"), f"health 接口异常 HTTP {status}"
        cls.health = body["data"]
        print(f"  health: {json.dumps(cls.health, ensure_ascii=False)}")

        status, body = call("GET", "/api/theses")
        assert status == 200 and body.get("ok"), f"theses 接口异常 HTTP {status}"
        cls.theses = body["data"]["items"]
        print(f"  母题总数：{len(cls.theses)}")

        empties = [t for t in cls.theses if not t.get("evidence")]
        riches = [t for t in cls.theses if t.get("evidence") and len(t["evidence"]) >= 1]
        assert empties, "找不到 evidence 为空的母题，红线测试无法建立基线"
        assert riches, "找不到 evidence >= 1 的母题，红线测试无法建立基线"

        cls.empty_id = empties[0]["id"]
        cls.rich_id = riches[0]["id"]
        cls.rich_evidence = riches[0]["evidence"]
        print(f"  EMPTY_ID = {cls.empty_id}「{empties[0]['title']}」")
        print(f"  RICH_ID  = {cls.rich_id}「{riches[0]['title']}」"
              f"（{len(cls.rich_evidence)} 条素材）")

    def test_01_health_matches_theses(self):
        """health 的 empty_evidence 必须与 theses 实际统计一致"""
        actual_empty = len([t for t in self.theses if not t.get("evidence")])
        expected = self.health.get("empty_evidence")
        self.assertEqual(actual_empty, expected,
                         f"health 报 empty_evidence={expected}，"
                         f"但 /api/theses 实际有 {actual_empty} 条空素材母题")

    def test_02_card_on_empty_thesis(self):
        """断言1、2：空素材母题生成的卡，own_detail 必须为空，missing 必须含「缺一手经历」"""
        global CARD_EMPTY_ID
        status, body = call("POST", "/api/gen/card", {"thesis_id": self.empty_id},
                            timeout=TIMEOUT_GEN)
        self.assertEqual(status, 200, f"HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
        self.assertTrue(body.get("ok"), f"ok=false：{body.get('error')}")
        card = body["data"]
        CARD_EMPTY_ID = card.get("id")
        snippet = json.dumps({k: card.get(k) for k in
                              ("id", "claim", "own_detail", "missing")},
                             ensure_ascii=False)

        own = card.get("own_detail")
        missing = card.get("missing") or []
        miss_joined = "".join(str(m) for m in missing)

        ok1 = own is not None and str(own).strip() == ""
        record(1, "空素材母题的 own_detail 必须是空字符串", "PASS" if ok1 else "FAIL",
               snippet)

        ok2 = "缺一手经历" in miss_joined
        record(2, "missing 里必须包含「缺一手经历」", "PASS" if ok2 else "FAIL",
               f"missing={json.dumps(missing, ensure_ascii=False)}")

        self.assertTrue(ok1, f"own_detail 不为空，实际值：{own!r}；原始返回 {snippet}")
        self.assertTrue(ok2, f"missing 缺「缺一手经历」，实际：{json.dumps(missing, ensure_ascii=False)}")

    def test_03_card_on_rich_thesis(self):
        """断言3、4：有素材母题的 own_detail 非空，且每句都能在 evidence 原文里逐字找到"""
        status, body = call("POST", "/api/gen/card", {"thesis_id": self.rich_id},
                            timeout=TIMEOUT_GEN)
        self.assertEqual(status, 200, f"HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
        self.assertTrue(body.get("ok"), f"ok=false：{body.get('error')}")
        card = body["data"]
        own = (card.get("own_detail") or "").strip()
        snippet = json.dumps({"id": card.get("id"), "own_detail": own},
                             ensure_ascii=False)

        ok3 = own != ""
        record(3, "有素材母题的 own_detail 必须非空", "PASS" if ok3 else "FAIL", snippet)
        self.assertTrue(ok3, f"own_detail 为空；原始返回 {snippet}")

        evidence_norm = [norm(e) for e in self.rich_evidence]
        sents = split_sentences(own)
        bad = []
        for s in sents:
            ns = norm(s)
            if not ns:
                continue
            if not any(ns in ev for ev in evidence_norm):
                bad.append(s)

        detail = f"own_detail={own}；素材={json.dumps(self.rich_evidence, ensure_ascii=False)}"
        ok4 = not bad
        record(4, "own_detail 每句都能在 evidence 原文里逐字找到", "PASS" if ok4 else "FAIL",
               detail if ok4 else f"找不到出处的句子：{json.dumps(bad, ensure_ascii=False)}；{detail}")

        self.assertFalse(bad,
                         "以下句子在素材原文里找不到逐字对应，属于模型凭空编造："
                         + json.dumps(bad, ensure_ascii=False)
                         + f"；素材原文：{json.dumps(self.rich_evidence, ensure_ascii=False)}")

    def test_04_derive_no_first_person(self):
        """断言5：空素材卡派生的任何文字，不许出现第一人称经历的组合词"""
        self.assertIsNotNone(CARD_EMPTY_ID,
                             "前置失败：没有得到空素材母题生成的卡 id，无法验证派生")

        status, body = call("POST", "/api/gen/derive",
                            {"card_id": CARD_EMPTY_ID, "forms": ["article", "script"]},
                            timeout=TIMEOUT_DERIVE)
        self.assertEqual(status, 200, f"HTTP {status} {json.dumps(body, ensure_ascii=False)[:300]}")
        self.assertTrue(body.get("ok"), f"ok=false：{body.get('error')}")

        texts = []

        def walk(o):
            if isinstance(o, str):
                texts.append(o)
            elif isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(body["data"].get("results"))
        blob = "\n".join(texts)

        hits = []
        for w in BANNED:
            start = 0
            while True:
                i = blob.find(w, start)
                if i < 0:
                    break
                hits.append({"word": w, "quote": blob[max(0, i - 20):i + 25]})
                start = i + len(w)

        ok5 = not hits
        record(5, "派生结果不含第一人称经历组合词", "PASS" if ok5 else "FAIL",
               f"命中 {json.dumps(hits, ensure_ascii=False)}" if hits
               else f"全文 {len(blob)} 字，无命中；开头：{blob[:120]}")

        self.assertFalse(hits,
                         "派生文本出现第一人称经历词："
                         + json.dumps(hits, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    print("=" * 62)
    print("红线测试 test_redline")
    print("=" * 62)

    suite = unittest.TestLoader().loadTestsFromTestCase(RedlineTest)
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(suite)

    problems = result.failures + result.errors
    ok = result.wasSuccessful() and not result.skipped and not problems

    print("-" * 62)
    print(f"红线总判定：{'PASS' if ok else 'FAIL'}")
    if problems:
        for case, tb in problems:
            tail = [ln for ln in tb.strip().splitlines() if ln.strip()]
            print(f"  失败用例 {case}: {tail[-1] if tail else ''}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "stage": "redline",
            "ok": ok,
            "asserts": ASSERTS,
            "skipped": len(result.skipped),
            "failures": [{"case": str(c), "msg": tb.strip().splitlines()[-1] if tb.strip() else ""}
                         for c, tb in problems],
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
