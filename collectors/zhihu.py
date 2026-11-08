"""知乎热榜采集器。

数据源：https://api.zhihu.com/topstory/hot-lists/total?limit=50
说明：www.zhihu.com/api/v3/feed/topstory/hot-lists/total 目前需要登录鉴权（返回 401），
     改用 api.zhihu.com 同路径接口，返回结构一致：
     data 数组，target.title 作标题，detail_text 解析热度，target.id 拼问答地址。
"""

import re

import httpx

from config import USER_AGENT

PLATFORM = "zhihu"
URL = "https://api.zhihu.com/topstory/hot-lists/total?limit=50"


def _parse_detail(text) -> int:
    """把「1234 万热度」解析成整数。"""
    if not text:
        return 0
    m = re.search(r"([\d.]+)\s*(万|亿)?", str(text))
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "万":
        num *= 10000
    elif unit == "亿":
        num *= 100000000
    return int(num)


def fetch() -> list[dict]:
    """抓取知乎热榜，返回标准化条目列表。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.zhihu.com/hot",
        "Accept": "application/json, text/plain, */*",
    }
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        # 先访问首页拿基础 Cookie，降低匿名请求被拦截的概率
        try:
            client.get("https://www.zhihu.com/", headers={"User-Agent": USER_AGENT})
        except Exception:
            pass  # 预热失败不阻断主请求
        resp = client.get(URL, headers=headers)
        resp.raise_for_status()
        payload = resp.json()

    items = []
    for row in payload.get("data") or []:
        target = row.get("target") or {}
        title = (target.get("title") or "").strip()
        if not title:
            continue
        qid = target.get("id")
        if not qid:
            m = re.search(r"/(?:question/)?(\d+)", str(target.get("url") or ""))
            qid = m.group(1) if m else None
        items.append(
            {
                "platform": PLATFORM,
                "title": title,
                "heat": _parse_detail(row.get("detail_text")),
                "url": f"https://www.zhihu.com/question/{qid}" if qid else "",
            }
        )
    return items


if __name__ == "__main__":
    data = fetch()
    print(f"知乎热榜共 {len(data)} 条，前 5 条：")
    for i, item in enumerate(data[:5], 1):
        print(f"{i}. 热度 {item['heat']}  {item['title']}")
