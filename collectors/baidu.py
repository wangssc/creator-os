"""百度热搜采集器。

数据源：https://top.baidu.com/api/board?platform=wise&tab=realtime
返回结构：data.cards[0].content[0].content 才是榜单数组（多包了一层）。
当前接口已不返回 hotScore/hot 字段，仅有序号 index，因此 heat 统一记 0，
不伪造热度数字；排序时以入库顺序体现榜单位次。
"""

import httpx

from config import USER_AGENT

PLATFORM = "baidu"
URL = "https://top.baidu.com/api/board?platform=wise&tab=realtime"


def fetch() -> list[dict]:
    """抓取百度实时热搜，返回标准化条目列表。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://top.baidu.com/board?tab=realtime",
        "Accept": "application/json, text/plain, */*",
    }
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(URL, headers=headers)
        resp.raise_for_status()
        payload = resp.json()

    cards = (payload.get("data") or {}).get("cards") or []
    if not cards:
        return []

    # 榜单数组可能被包在 content[0].content 里，做一层兼容探测
    rows = []
    for block in cards[0].get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("content"), list):
            rows.extend(block["content"])
        elif isinstance(block, dict) and block.get("word"):
            rows.append(block)

    items = []
    for row in rows:
        title = (row.get("word") or row.get("query") or "").strip()
        if not title:
            continue
        items.append(
            {
                "platform": PLATFORM,
                "title": title,
                "heat": 0,  # 接口无热度字段，不填造数据
                "url": row.get("url") or row.get("rawUrl") or "",
            }
        )
    return items


if __name__ == "__main__":
    data = fetch()
    print(f"百度热搜共 {len(data)} 条，前 5 条：")
    for i, item in enumerate(data[:5], 1):
        print(f"{i}. 热度 {item['heat']}  {item['title']}")
