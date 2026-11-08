# -*- coding: utf-8 -*-
"""四模块独立验收脚本：9 项，零第三方依赖（仅 Python 标准库）。

产物 docs/verify/_modules.json -> {"items":[{no,name,result,detail}]}
退出码 0=全 PASS，1=有 FAIL；单项异常单独记 FAIL 且不中断其余项。
固定服务 http://127.0.0.1:8787；数据库 data/creator.db；所有请求 5 秒超时。
本机设置了 HTTP_PROXY，会把 127.0.0.1 送进代理并返回 502，
因此这里用 ProxyHandler({}) 显式禁用代理直连 8787。
"""
import base64
import json
import os
import re
import sqlite3
import sys
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BASE_URL = "http://127.0.0.1:8787"
DB_PATH = BASE_DIR / "data" / "creator.db"
INDEX = BASE_DIR / "static" / "index.html"
APP = BASE_DIR / "app.py"
OUT = BASE_DIR / "docs" / "verify" / "_modules.json"
TIMEOUT = 5

# 1x1 透明 PNG，脚本内生成（标准库 base64，不读任何外部文件）
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

# 禁用环境代理，否则 127.0.0.1 请求会被代理拦成 502
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ITEMS = []


def record(no, name, result, detail):
    ITEMS.append({"no": no, "name": name, "result": result, "detail": str(detail)})
    print("[%s] %s. %s" % (result, no, name))
    print("       %s" % detail)


def http(method, path, body=None, headers=None):
    """统一请求，返回 (status, text)。body 为 dict/list 时按 JSON 发送。"""
    data = None
    hdr = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdr.setdefault("Content-Type", "application/json")
        else:
            data = body
    req = urllib.request.Request(BASE_URL + path, data=data, headers=hdr, method=method)
    with OPENER.open(req, timeout=TIMEOUT) as r:
        return r.status, r.read().decode("utf-8", "replace")


def get_json(path):
    st, text = http("GET", path)
    return st, json.loads(text)


def check(no, name, fn):
    """单项隔离：任何异常都记 FAIL 并写明，绝不让整个脚本崩掉。"""
    try:
        ok, detail = fn()
        record(no, name, "PASS" if ok else "FAIL", detail)
    except Exception as e:
        record(no, name, "FAIL", "异常 %s：%s" % (type(e).__name__, e))


def sqlite_rows(table):
    con = sqlite3.connect(str(DB_PATH))
    try:
        return con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    finally:
        con.close()


def c1():
    st, _ = http("GET", "/")
    return st == 200, "GET / -> HTTP %s（期望 200）；根路径返回 HTML，不断言 JSON" % st


def c2():
    st, j = get_json("/api/market?limit=500")
    d = j.get("data") or {}
    items = d.get("items") or []
    has_url = all("url" in it for it in items)
    nums = [float(it.get("starNum") or 0) for it in items]
    desc_ok = all(nums[i] >= nums[i + 1] for i in range(len(nums) - 1))
    ok = st == 200 and j.get("ok") is True and len(items) > 0 and has_url and desc_ok
    return ok, ("HTTP %s ok=%s items=%d total=%s cats=%d；每条含 url=%s；"
                "starNum 序列=%s 降序=%s" % (
                    st, j.get("ok"), len(items), d.get("total"),
                    len(d.get("cats") or []), has_url, nums, desc_ok))


def c3():
    # q=自动化&cat=效率（中文已 URL 编码），过滤生效：返回项 cat 全为「效率」或空结果
    st, j = get_json("/api/market?q=%E8%87%AA%E5%8A%A8%E5%8C%96&cat=%E6%95%88%E7%8E%87")
    items = (j.get("data") or {}).get("items") or []
    cats = [it.get("cat") for it in items]
    ok = st == 200 and j.get("ok") is True and all(c == "效率" for c in cats)
    return ok, ("HTTP %s 返回 %d 条，cat 列表=%s；全部为「效率」或空结果=%s" % (
        st, len(items), cats, ok))


def c4():
    st, text = http("GET", "/api/books")
    j = json.loads(text)
    d = j.get("data") or {}
    items = d.get("items") or []
    enabled_ok = all(it.get("enabled") is True for it in items)
    url_ok = all("url" in it for it in items)
    body_fields = ["content", "body", "chapters", "full_text", "fulltext", "paragraphs", "text"]
    hits = [k for k in body_fields if re.search(r'"%s"\s*:' % re.escape(k), text)]
    ok = (st == 200 and j.get("ok") is True and len(items) == 8
          and enabled_ok and url_ok and not hits)
    return ok, ("HTTP %s items=%d（期望 8）全部 enabled=true=%s 每条含 url=%s；"
                "url 实际值=%s；响应体正文字段命中=%s（期望 []）" % (
                    st, len(items), enabled_ok, url_ok,
                    [it.get("url") for it in items], hits))


def c5():
    st, j = get_json("/api/points")
    d = j.get("data") or {}
    bal = d.get("balance")
    recs = d.get("records") or []
    rules = d.get("rules") or []
    bal_num = isinstance(bal, (int, float)) and not isinstance(bal, bool)
    rec_ok = all("balance_after" in r for r in recs)
    ok = st == 200 and bal_num and rec_ok and len(rules) > 0
    return ok, ("HTTP %s balance=%s（数值=%s）delta=%s records=%d 每条含 balance_after=%s"
                " rules=%d（非空=%s）；record0=%s" % (
                    st, bal, bal_num, d.get("delta"), len(recs), rec_ok,
                    len(rules), len(rules) > 0,
                    json.dumps(recs[0], ensure_ascii=False) if recs else "无"))


def c6():
    _, before = get_json("/api/points")
    b0 = before["data"]["balance"]
    r0 = len(before["data"]["records"])
    db0 = sqlite_rows("point_records")
    st, text = http("POST", "/api/points/adjust", {"reason": "模块验收", "delta": -1})
    aj = json.loads(text)
    _, after = get_json("/api/points")
    b1 = after["data"]["balance"]
    r1 = len(after["data"]["records"])
    db1 = sqlite_rows("point_records")
    ok = (st == 200 and aj.get("ok") is True
          and b1 == b0 - 1 and r1 == r0 + 1 and db1 == db0 + 1)
    return ok, ("POST /api/points/adjust HTTP %s ok=%s；前 balance=%d records=%d sqlite行数=%d"
                " → 后 balance=%d records=%d sqlite行数=%d；"
                "期望 balance-1 / records+1 / sqlite+1；data=%s" % (
                    st, aj.get("ok"), b0, r0, db0, b1, r1, db1,
                    json.dumps(aj.get("data"), ensure_ascii=False)))


def c7():
    boundary = "----wb" + uuid.uuid4().hex
    parts = [
        ("--%s\r\n" % boundary).encode("ascii"),
        b'Content-Disposition: form-data; name="files"; filename="px.png"\r\n',
        b"Content-Type: image/png\r\n\r\n",
        PNG_1X1,
        b"\r\n",
        ("--%s--\r\n" % boundary).encode("ascii"),
    ]
    body = b"".join(parts)
    st, text = http("POST", "/api/replicate", body,
                    {"Content-Type": "multipart/form-data; boundary=" + boundary})
    j = json.loads(text)
    d = j.get("data") or {}
    keys = ("structure", "layers", "hooks", "directions")
    nonempty = all(d.get(k) for k in keys)
    con = sqlite3.connect(str(DB_PATH))
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        con.close()
    bad = [t for t in tables if re.search(r"replicate|upload|image", t, re.I)]
    ok = st == 200 and j.get("ok") is True and nonempty and not bad
    return ok, ("POST /api/replicate（multipart 字段 files，1x1 PNG %d 字节）HTTP %s ok=%s；"
                "structure/layers/hooks/directions 非空=%s file_count=%s；"
                "note=%s；sqlite 表=%s；replicate|upload|image 相关表=%s（期望 []）" % (
                    len(PNG_1X1), st, j.get("ok"), nonempty, d.get("file_count"),
                    json.dumps(d.get("note"), ensure_ascii=False), tables, bad))


def detect_key():
    """运行时真实探测 ZHIPU_API_KEY 是否配置：先看环境变量，再读 .env。
    不写死任何环境结论；也不 import 依赖 dotenv 的 config 模块，保持零第三方依赖。
    """
    v = (os.environ.get("ZHIPU_API_KEY") or "").strip()
    if not v:
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            m = re.search(r"(?m)^\s*ZHIPU_API_KEY\s*=\s*(.*)$",
                          env_file.read_text(encoding="utf-8", errors="replace"))
            if m:
                v = m.group(1).strip().strip('"').strip("'")
    return bool(v)


def c8():
    html = INDEX.read_text(encoding="utf-8", errors="replace")
    code = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    code = re.sub(r"<!--.*?-->", "", code, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    five = re.findall(r"MOCK\.(tools|books|bookCats|points|replicate)", code)
    m = re.search(r"var\s+LATER_MODULES\s*=\s*\{([^}]*)\}", code, re.S)
    body = m.group(1) if m else ""
    keys = re.findall(r"(market|books|replicate|diagnose)\s*:", body)
    # 判定升级：引用为 0 且 MOCK 对象定义里也不再含这五个键
    m_mock = re.search(r"var\s+MOCK\s*=\s*\{", code)
    mock_body = ""
    if m_mock:
        rest = code[m_mock.end():]
        m_end = re.search(r"(?m)^\};", rest)
        if m_end:
            mock_body = rest[:m_end.start()]
    def_hits = re.findall(r"(?m)^\s{2}(tools|books|bookCats|points|replicate)\s*:", mock_body)
    ok = len(five) == 0 and len(keys) == 0 and len(def_hits) == 0
    return ok, ("剥离注释后引用计数=%d 命中=%s；MOCK 对象定义命中=%d 命中=%s（定义命中=0 才 PASS）；"
                "LATER_MODULES 段=%s；残留已上线模块键（market/books/replicate/diagnose）=%s" % (
                    len(five), five, len(def_hits), def_hits,
                    " ".join(body.split()), keys))


def c9():
    src = APP.read_text(encoding="utf-8", errors="replace")
    n = len(re.findall(r"db\.points_change\(", src))
    has_cost = bool(re.search(r"\bPOINT_COST\b", src))
    key_configured = detect_key()
    ok = n >= 6 and has_cost
    return ok, ("扣分挂载为静态直读：app.py 中 db.points_change( 出现 %d 次（≥6），"
                "POINT_COST 常量存在=%s；运行时回归由全量 run_all.py 覆盖，不在本脚本内断言；"
                "运行时探测 key_configured=%s" % (n, has_cost, key_configured))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=" * 70)
    print("四模块独立验收 verify_modules（服务 %s）" % BASE_URL)
    print("=" * 70)
    check(1, "GET / 返回 200", c1)
    check(2, "GET /api/market 基础结构与 starNum 降序", c2)
    check(3, "GET /api/market?q=自动化&cat=效率 过滤生效", c3)
    check(4, "GET /api/books 8 条且无正文字段", c4)
    check(5, "GET /api/points 结构与记录字段", c5)
    check(6, "POST /api/points/adjust 前后对比 + sqlite 行数", c6)
    check(7, "POST /api/replicate multipart 与无落库表", c7)
    check(8, "index.html 无四模块 MOCK 表达式且 LATER_MODULES 解禁", c8)
    check(9, "app.py 扣分挂载静态断言", c9)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"items": ITEMS}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    fails = [i for i in ITEMS if i["result"] == "FAIL"]
    print("-" * 70)
    print("模块验收：%d PASS / %d FAIL；产物 %s" % (
        len(ITEMS) - len(fails), len(fails), OUT))
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
