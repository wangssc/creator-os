"""微博热搜采集器。

数据源：https://weibo.com/ajax/side/hotSearch
接口返回 data.realtime 数组，取 word 作标题、num 作热度。
"""

from urllib.parse import quote

import httpx

from config import USER_AGENT

PLATFORM = "weibo"
URL = "https://weibo.com/ajax/side/hotSearch"


def fetch() -> list[dict]:
    """抓取微博热搜榜，返回标准化条目列表。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
    }
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(URL, headers=headers)
        resp.raise_for_status()
        payload = resp.json()

    realtime = (payload.get("data") or {}).get("realtime") or []
    items = []
    for row in realtime:
        title = (row.get("word") or row.get("note") or "").strip()
        if not title:
            continue
        items.append(
            {
                "platform": PLATFORM,
                "title": title,
                "heat": int(row.get("num") or 0),
                "url": f"https://s.weibo.com/weibo?q=%23{quote(title)}%23",
            }
        )
    return items


if __name__ == "__main__":
    data = fetch()
    print(f"微博热搜共 {len(data)} 条，前 5 条：")
    for i, item in enumerate(data[:5], 1):
        print(f"{i}. 热度 {item['heat']}  {item['title']}")
