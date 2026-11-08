"""配置加载：从 .env 读取环境变量，导出全局常量。

所有路径均基于本文件所在目录，保证在任何工作目录下启动都能正确找到资源。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent

# 加载 .env（文件不存在时静默跳过，不影响启动）
load_dotenv(BASE_DIR / ".env")

# 智谱开放平台配置
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY", "").strip()
ZHIPU_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

# 模型回退链：依次尝试，第一个成功的写入 contents.model
# 顺序按「实测延迟 + 质量」排：
#   glm-4.5-air   约 2.5s，指令遵循好（首选）
#   glm-4-flash   约 2.1s，快但内容偏模板化（兜底）
#   glm-4-plus    约 0.8s，质量可用（最后兜底）
# 不用 glm-4.5-flash：实测单次 40s+，会撞超时再回退，等于白等
MODEL_CHAIN = ["glm-4.5-air", "glm-4-flash", "glm-4-plus"]

# 数据库与静态资源
DB_PATH = BASE_DIR / "data" / "creator.db"
STATIC_DIR = BASE_DIR / "static"
PROMPT_DIR = BASE_DIR / "prompts"

# 用户自己填的风格样本。刻意放在 data/ 下而不是 prompts/：
# prompts/ 里的是代码资产，跟着版本走；这份是用户资产，跟着人走，且不入库。
# 模板在 prompts/style_sample.md，用户还没填过的时候读那个。
STYLE_SAMPLE_PATH = BASE_DIR / "data" / "style_sample.md"
STYLE_SAMPLE_TEMPLATE = PROMPT_DIR / "style_sample.md"

# 服务监听（只允许本机访问）
HOST = "127.0.0.1"
PORT = 8787

# 热点定时刷新间隔（秒）
REFRESH_INTERVAL_SEC = 30 * 60

# ---------------------------------------------------------------- 热点采集旁路服务
# DailyHotApi 是 TypeScript + Hono 的采集仓库，56 个平台的反爬与字段差异都在里面。
# 我们不在 Python 里重写它，而是让 service/hotnode 起一个独立 Node 进程，
# 对外只暴露 GET /hot/:platform，Python 侧是纯 HTTP 客户端（拿不到任何 Node 依赖）。
#
# 只绑回环地址：这是本机采集能力，不是对外服务。
HOT_API_URL = "http://127.0.0.1:6688"

# 单平台请求超时（秒）。实测值（2026-10-31，本机）：
#   ithome 7.3s、thepaper 11.9s、v2ex 21.8s 后仍 502（上游接口被拒，属环境问题）
# 上游 DailyHotApi 自身对慢请求会重试，所以单个平台可能耗到 20s 以上。
# 卡到 12s 时 thepaper 会随机超时，24s 以内 v2ex 永远走不到「失败」这条分支就先把整批拖死。
# 取 25s：足够让正常平台返回，异常平台也能干净地报错而不是拖垮整批。
HOT_API_TIMEOUT_SEC = 25.0

# 并发线程数。13 个平台、上游多为国内站点，8 路并发实测稳定；
# 开太大容易被上游按 IP 限流，反而整批失败。
HOT_API_WORKERS = 8

# 采集白名单：service/hotnode/platforms.json 是唯一事实来源，
# Node 侧用它做接口白名单与展示名，Python 侧用它决定刷新哪几个平台。
# 两边读同一个文件，避免「Node 加了平台但 Python 不采」这种漂移。
HOT_PLATFORMS_FILE = BASE_DIR / "service" / "hotnode" / "platforms.json"

# 文件被误删/改坏时的兜底，保证服务仍能起来（缺文件不该拖垮整个应用）
HOT_PLATFORM_FALLBACK = (
    "weibo",
    "zhihu",
    "baidu",
    "toutiao",
    "bilibili",
    "douyin",
    "36kr",
    "huxiu",
    "sspai",
    "juejin",
    "ithome",
    "thepaper",
    "v2ex",
)


def _load_hot_platforms() -> list:
    """读平台映射表，返回 [{"slug","name","type","note"}...]。读不到就用内置 13 个。"""
    try:
        import json

        raw = json.loads(HOT_PLATFORMS_FILE.read_text(encoding="utf-8"))
        table = [
            {"slug": str(r["slug"]), "name": str(r.get("name") or r["slug"])}
            for r in raw
            if isinstance(r, dict) and r.get("slug")
        ]
        if table:
            return table
    except Exception as e:  # 文件缺失、JSON 坏、权限问题都走兜底
        print(f"[config] 平台映射表读取失败，用内置列表：{type(e).__name__}: {e}")
    return [{"slug": s, "name": s} for s in HOT_PLATFORM_FALLBACK]


# HOT_PLATFORMS 是「我们采哪几个」的答案，顺序即刷新顺序
HOT_PLATFORMS = _load_hot_platforms()
HOT_PLATFORM_SLUGS = tuple(p["slug"] for p in HOT_PLATFORMS)
HOT_PLATFORM_NAMES = {p["slug"]: p["name"] for p in HOT_PLATFORMS}

# 采集器统一 User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 提示词版本号，写入 contents.prompt_ver
PROMPT_VER = "v1"
