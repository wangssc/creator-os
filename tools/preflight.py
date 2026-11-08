# -*- coding: utf-8 -*-
"""环境自检 — 验收第一关。

退出码：
  0  全部通过
  1  其他检查失败
  2  服务未启动
"""
import argparse
import json
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY_EXPECTED = r"C:\Users\32719\.workbuddy\binaries\python\versions\3.13.12\python.exe"
HOST = "127.0.0.1"
PORT = 8787
BASE_URL = f"http://{HOST}:{PORT}"
DB_PATH = BASE / "data" / "creator.db"

ITEMS = []


def add(name, result, detail):
    ITEMS.append({"name": name, "result": result, "detail": detail})


def port_open(timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_get(path, timeout=10):
    req = urllib.request.Request(BASE_URL + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    print("=" * 62)
    print("环境自检 preflight")
    print("=" * 62)

    # 1. 端口
    if not port_open():
        add("8787 端口监听", "FAIL", f"{HOST}:{PORT} 无监听")
        print("[FAIL] 服务未启动，请先双击 启动.cmd")
        print(f"       探测目标：{BASE_URL}")
        _finish(args, exit_code=2)
        return

    add("8787 端口监听", "PASS", f"{HOST}:{PORT} LISTENING")
    print(f"[PASS] 8787 端口在监听")

    # 2. /api/stats 可达
    try:
        status, body = http_get("/api/stats")
        ok = body.get("ok") is True and status == 200
        add("GET /api/stats 可达", "PASS" if ok else "FAIL",
            f"HTTP {status} {json.dumps(body, ensure_ascii=False)[:160]}")
        print(f"[{'PASS' if ok else 'FAIL'}] GET /api/stats → HTTP {status} "
              f"{json.dumps(body, ensure_ascii=False)[:120]}")
    except Exception as e:
        add("GET /api/stats 可达", "FAIL", f"{type(e).__name__}: {e}")
        print(f"[FAIL] GET /api/stats 不可达：{type(e).__name__}: {e}")

    # 3. 解释器路径
    actual = sys.executable
    same = actual.lower() == PY_EXPECTED.lower()
    add("Python 解释器路径", "PASS" if same else "FAIL", actual)
    print(f"[{'PASS' if same else 'FAIL'}] 解释器：{actual}")
    if not same:
        print(f"       期望：{PY_EXPECTED}")

    # 4. 数据库
    if not DB_PATH.exists():
        add("data/creator.db 存在且可读", "FAIL", f"不存在：{DB_PATH}")
        print(f"[FAIL] 数据库不存在：{DB_PATH}")
    else:
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            con.close()
            size = DB_PATH.stat().st_size
            add("data/creator.db 存在且可读", "PASS",
                f"{DB_PATH} {size} bytes，{len(tables)} 张表")
            print(f"[PASS] 数据库可读：{size} bytes，{len(tables)} 张表")
        except Exception as e:
            add("data/creator.db 存在且可读", "FAIL", f"{type(e).__name__}: {e}")
            print(f"[FAIL] 数据库不可读：{type(e).__name__}: {e}")

    failed = [i for i in ITEMS if i["result"] == "FAIL"]
    print("-" * 62)
    print(f"结果：{'PASS' if not failed else 'FAIL'}（{len(ITEMS) - len(failed)}/{len(ITEMS)} 通过）")
    _finish(args, exit_code=0 if not failed else 1)


def _finish(args, exit_code):
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"stage": "preflight", "items": ITEMS,
                        "exit_code": exit_code}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
