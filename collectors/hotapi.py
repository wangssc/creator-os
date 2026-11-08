"""DailyHotApi 旁路采集器（主路径）。

数据来源：service/hotnode 这个独立 Node 进程，它把 D:\\aa\\refs\\DailyHotApi 的
平台路由收敛成 GET /hot/:platform，返回统一结构：

    {"ok": true, "data": {"platform": "weibo", "total": 51,
                          "items": [{"title": ..., "heat": ..., "url": ...}]}}

本模块只做三件事：并发拉取、转成内置四源同款结构、把「服务不可用」变成可捕获的异常。
它不做重试、不写库、不认识 DailyHotApi 的任何字段差异——那些都是 Node 侧的责任。

之所以要区分「不可用」和「某平台 0 条」：
    不可用要回退到内置四源（保面板不空），单一平台 0 条只是这个源挂了，
    回退会让整批数据变成两个来源混着，反而不好解释。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from config import HOT_API_TIMEOUT_SEC, HOT_API_URL, HOT_API_WORKERS, HOT_PLATFORM_SLUGS


class HotApiUnavailable(RuntimeError):
    """旁路服务整体不可用：连不上、全部平台失败、或者整批一条都没拿到。"""


def _client() -> httpx.Client:
    """共享一个 Client 连接池；httpx.Client 本身线程安全，可以被多线程并发调用。

    trust_env=False 是硬要求，不是洁癖：本机环境里设了 http_proxy（抓包/代理工具常见），
    httpx 默认会连 127.0.0.1 的请求也丢给代理，结果就是「Node 明明活着，Python 却报
    502 连不上」。旁路服务在本机回环，经代理没有意义，还可能被代理拒掉。
    """
    return httpx.Client(
        timeout=HOT_API_TIMEOUT_SEC,
        follow_redirects=True,
        trust_env=False,
    )


def _fetch_one(client: httpx.Client, slug: str) -> list:
    """拉单个平台。返回 [{title, heat, url}]，失败抛异常由调用方记录。"""
    resp = client.get(f"{HOT_API_URL}/hot/{slug}", params={"cache": "false"})
    payload = resp.json()
    if resp.status_code != 200 or not payload.get("ok"):
        raise RuntimeError(payload.get("error") or f"HTTP {resp.status_code}")

    items = []
    for it in (payload.get("data") or {}).get("items") or []:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "platform": slug,
                "title": title,
                "heat": int(it.get("heat") or 0),
                "url": it.get("url") or "",
            }
        )
    return items


def fetch_all(fresh: bool = True) -> dict:
    """并发拉全部白名单平台，返回 {平台: [条目...]}。

    fresh=True 时给每个请求带上 cache=false，绕过 DailyHotApi 自带的 60s 缓存，
    保证每次 refresh 拿到的是一手榜单而不是上一批的残影。

    全部失败时抛 HotApiUnavailable，由 app.refresh_hot 决定是否回退内置四源。
    """
    result = {}
    errors = {}
    with _client() as client:
        with ThreadPoolExecutor(max_workers=HOT_API_WORKERS) as pool:
            futures = {pool.submit(_fetch_one, client, slug): slug for slug in HOT_PLATFORM_SLUGS}
            for fut in as_completed(futures):
                slug = futures[fut]
                try:
                    items = fut.result()
                    result[slug] = items
                    print(f"[hotapi] {slug} 采集 {len(items)} 条")
                except Exception as e:  # 单平台失败不影响其他平台
                    result[slug] = []
                    errors[slug] = f"{type(e).__name__}: {e}"
                    print(f"[hotapi] {slug} 失败: {errors[slug]}")

    total = sum(len(v) for v in result.values())
    if total == 0:
        detail = "; ".join(f"{k}={v}" for k, v in list(errors.items())[:3]) or "全部平台返回 0 条"
        raise HotApiUnavailable(f"{HOT_API_URL} 未取到任何数据（{detail}）")
    return result


def ping() -> bool:
    """探活，只给排查用，刷新逻辑不依赖它（多一次请求就多一次超时机会）。"""
    try:
        with _client() as client:
            return client.get(f"{HOT_API_URL}/health").status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    data = fetch_all()
    print(f"\n共 {len(data)} 个平台、{sum(len(v) for v in data.values())} 条")
    for slug, items in data.items():
        print(f"{slug:10s} {len(items):3d} 条  {items[0]['title'] if items else ''}")
