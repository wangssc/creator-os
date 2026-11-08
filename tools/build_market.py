# -*- coding: utf-8 -*-
"""可选采集脚本：用 GitHub 公开 search API 刷新 data/market.json 的 star 与仓库地址。

零第三方依赖（仅标准库 urllib / json）。本项目不运行它，也不把它的输出当验收依据：
market 是 curated 静态快照，正式数据以 data/market.json 为准，star_source 默认 "snapshot"。

约束（写死在流程里）：
- GitHub 未带 token 的 search API 限流为 60 次/小时；脚本只对少量 topic 各发 1 次请求。
- 任一请求失败 / 被限流 / 返回空：保留现有 data/market.json 原样，不写半成品、不删文件。
- 绝不编造数据：只写 API 真实返回的 full_name / stargazers_count；
  只更新快照里已存在的 en（按名称精确匹配），不新增仓库、不猜 star、不猜仓库名。
- 原型文件里的 9 条是真实开源项目；star 是原型快照值，只有拿到真实 API 值才标 "github"。
"""
import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "market.json"
# 只对这几个主题各发一次请求，远低于 60 次/小时的匿名限流
TOPICS = ["workflow-automation", "llm", "self-hosted", "analytics"]


def search(topic):
    q = urllib.parse.urlencode({"q": f"topic:{topic}", "sort": "stars", "per_page": 10})
    url = f"https://api.github.com/search/repositories?{q}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "creator-os-market-builder",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body.get("items", [])


def main():
    if not OUT.exists():
        print(f"[skip] 快照不存在，保留原状：{OUT}")
        return
    snapshots = json.loads(OUT.read_text(encoding="utf-8"))
    by_en = {str(it.get("en", "")).lower(): it for it in snapshots}

    updated = 0
    for topic in TOPICS:
        try:
            repos = search(topic)
        except Exception as e:  # 网络失败 / 403 限流 / 解析失败都保留原快照
            print(f"[skip] topic={topic} 抓取失败，保留原快照：{type(e).__name__}: {e}")
            continue
        for repo in repos:
            en = repo.get("name") or ""
            it = by_en.get(en.lower())
            if not it:
                continue  # 快照里没有的仓库一律不新增，避免把搜索噪声写进 curated 清单
            stars = repo.get("stargazers_count")
            if isinstance(stars, int):
                it["starNum"] = round(stars / 1000, 1)
                it["star"] = f"{it['starNum']}k"
                it["star_source"] = "github"
            full_name = repo.get("full_name")
            if full_name:
                it["url"] = f"https://github.com/{full_name}"
            updated += 1

    if not updated:
        print("本轮没有拿到可更新的真实条目，保留原快照，不写文件。")
        return
    OUT.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已用真实 API 数据更新 {updated} 条 → {OUT}")


if __name__ == "__main__":
    main()
