"""今日头条热榜采集器。

数据源：https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc
接口返回 data 数组，Title 作标题、HotValue 作热度、Url 作地址。
"""

import httpx

from config import USER_AGENT

PLATFORM = "toutiao"
URL = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"


def fetch() -> list[dict]:
    """抓取头条热榜，返回标准化条目列表。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.toutiao.com/",
        "Accept": "application/json, text/plain, */*",
    }
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(URL, headers=headers)
        resp.raise_for_status()
        payload = resp.json()

    items = []
    for row in payload.get("data") or []:
        title = (row.get("Title") or "").strip()
        if not title:
            continue
        try:
            heat = int(float(row.get("HotValue") or 0))
        except (TypeError, ValueError):
            heat = 0
        items.append(
            {
                "platform": PLATFORM,
                "title": title,
                "heat": heat,
                "url": row.get("Url") or "",
            }
        )
    return items


if __name__ == "__main__":
    data = fetch()
    print(f"头条热榜共 {len(data)} 条，前 5 条：")
    for i, item in enumerate(data[:5], 1):
        print(f"{i}. 热度 {item['heat']}  {item['title']}")
