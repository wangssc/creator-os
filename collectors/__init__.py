"""热点采集器包。

两条路径：
  1) 主路径 hotapi.py —— 调 service/hotnode 这个 DailyHotApi 旁路服务（13 个平台）
  2) 兜底路径 fetch_all() —— 内置四源，DailyHotApi 挂掉时保证面板不空

每个子模块暴露 fetch() -> list[dict]，每项包含 platform / title / heat / url。
fetch_all() 统一调度，单源失败不影响其他源。

子模块按需动态导入，避免 `python -m collectors.weibo` 触发重复导入警告。
"""

import importlib
import re

from .hotapi import HotApiUnavailable, fetch_all as fetch_from_hotapi

# 采集器名称列表（顺序即入库顺序）
COLLECTOR_NAMES = ["weibo", "zhihu", "baidu", "toutiao"]


def parse_heat(text) -> int:
    """把「1234 万热度」「98.5万」这类中文热度描述解析成整数。解析不出返回 0。"""
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        return int(text)
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


def load(name: str):
    """按名称加载采集器模块。"""
    return importlib.import_module(f".{name}", __name__)


def fetch_all() -> dict:
    """跑全部采集器，返回 {平台: [条目...]}。单源异常只记录，不中断。"""
    result = {}
    for name in COLLECTOR_NAMES:
        try:
            mod = load(name)
            items = mod.fetch()
            result[name] = items
            print(f"[collector] {name} 采集 {len(items)} 条")
        except Exception as e:  # 单源失败不影响其他源
            result[name] = []
            print(f"[collector] {name} 失败: {type(e).__name__}: {e}")
    return result
