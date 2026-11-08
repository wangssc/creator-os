"""FastAPI 入口与路由。

统一响应格式：
  成功 {"ok": true, "data": ...}
  失败 {"ok": false, "error": "中文原因", "retryable": bool}
"""

import csv
import io
import json
import os
import re
import statistics
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import collectors
import config
import db
import llm
import rules


def ok(data):
    """成功响应包装。"""
    return {"ok": True, "data": data}


def fail(error: str, retryable: bool = False, status_code: int = 400):
    """失败响应包装，默认 HTTP 400 而不是 500。"""
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": error, "retryable": retryable},
    )


# ---------------------------------------------------------------- 四模块真实数据
# 生成成功后的积分扣分表；扣分失败只记日志，绝不拖垮生成返回。
POINT_COST = {"article": 10, "note": 10, "script": 10, "cards": 10, "card": 10, "derive": 10}

# 静态快照目录，与 DB_PATH 同源于 config.BASE_DIR
DATA_DIR = config.BASE_DIR / "data"

# 分类固定顺序（原型 index.html:5036 / index.html:2308），不用 set/sorted 打乱；
# JSON 里出现的新分类按出现顺序追加到末尾，去重且不丢数据。
MARKET_CATS = ["全部", "营销", "编程", "效率", "设计", "数据"]
BOOK_CATS = ["全部", "Agent", "AI编程", "自动化", "知识管理", "生活技能", "个人成长", "英语学习"]


def _ordered_cats(fixed, items):
    """固定顺序在前；JSON 里多出来的新分类按出现顺序追加到末尾，去重。"""
    cats = list(fixed)
    for it in items:
        c = str(it.get("cat") or "").strip()
        if c and c not in cats:
            cats.append(c)
    return cats

REPLICATE_MAX_FILES = 8
REPLICATE_MAX_BYTES = 10 * 1024 * 1024

# 结构报告模板：沿用原型 MOCK.replicate 的结构 + 页内二创方向，纯本地模板，不识别图片。
REPLICATE_TEMPLATE = {
    "structure": "三段式对比结构（旧认知 → 转折 → 新方案）",
    "layers": [
        "第 1 层 · 主视觉钩子：一句反常识大字标题，占画面 40% 高度，居中偏上",
        "第 2 层 · 支撑信息：两行副文案 + 一个数字锚点（如「3 个方法」），位于标题正下方",
        "第 3 层 · 落款区：底部 15% 留白，放品牌名 / 引导语 / 二维码占位",
    ],
    "hooks": [
        "数字钩子：标题中嵌入具体数字（3 个 / 5 天 / 90%），可信度显著提升",
        "冲突钩子：用「别再…」「你以为…其实…」制造认知反差",
        "身份钩子：直接点名人群（中年以后 / 退休后 / 新手爸妈），提升代入感",
    ],
    "colors": [
        {"hex": "#1F3A2E", "name": "主色"},
        {"hex": "#E8DCC8", "name": "底色"},
        {"hex": "#C0533C", "name": "强调"},
    ],
    "directions": [
        {"k": "换主题", "d": "保留结构与视觉钩子，换一个题材"},
        {"k": "换人群", "d": "结构不变，目标读者换成另一群人"},
        {"k": "换风格", "d": "结构不变，视觉风格整体改写"},
    ],
}


# ---------------------------------------------------------------- 热点采集


def refresh_hot() -> dict:
    """跑采集器并写库，返回各源条数与新增行数。

    采集主路径换成 DailyHotApi 旁路服务（service/hotnode，独立 Node 进程）：
    它把 13 个平台的反爬和字段差异吃下来，这里只当 HTTP 客户端用。
    旁路服务没起、或者整批一条都没拿到时，回退到 collectors 里的内置四源实现——
    「热点面板不空」这个底线不能依赖 Node 进程活着。

    source 字段记录这批数据的来路：hotnode（主路径）或 local（兜底）。
    混采没有意义，一批数据只有一个来源，所以 source 按整批写。
    """
    ts = db.now_str()
    try:
        sources = collectors.fetch_from_hotapi()
        source = "hotnode"
    except collectors.HotApiUnavailable as e:
        print(f"[refresh] 旁路服务不可用，回退内置四源：{e}")
        sources = collectors.fetch_all()
        source = "local"

    inserted = 0
    for platform, items in sources.items():
        rows = [
            (platform, it["title"], int(it.get("heat") or 0), it.get("url") or "", ts, source)
            for it in items
            if it.get("title")
        ]
        if not rows:
            continue
        # 同一批 fetched_at 相同，重复条目由 UNIQUE(platform,title,fetched_at) 忽略
        n = db.executemany(
            "INSERT OR IGNORE INTO topics(platform, title, heat, url, fetched_at, source)"
            " VALUES(?,?,?,?,?,?)",
            rows,
        )
        inserted += max(n, 0)
    return {
        "inserted": inserted,
        "source": source,
        "sources": {k: len(v) for k, v in sources.items()},
    }


def _refresh_loop():
    """启动即刷新一次，之后每隔 REFRESH_INTERVAL_SEC 秒刷新一次。"""

    def do_refresh(tag: str):
        try:
            result = refresh_hot()
            print(f"[refresh] {tag}: {result['sources']} 新增 {result['inserted']} 条")
        except Exception as e:
            print(f"[refresh] {tag} 失败: {type(e).__name__}: {e}")

    def tick():
        do_refresh("定时刷新")
        schedule()

    def schedule():
        timer = threading.Timer(config.REFRESH_INTERVAL_SEC, tick)
        timer.daemon = True
        timer.start()

    do_refresh("首次刷新")
    schedule()


# ---------------------------------------------------------------- 提示词渲染

# 风格样本段的三个锚点。空样本时要把整段摘掉，而不只是把占位符换成空串——
# 留着引导句（「以下是一段目标风格样本……」）等于让模型去模仿一段不存在的文字。
STYLE_BLOCK_START = "以下是一段目标风格样本"
STYLE_BLOCK_END = "如果样本为空，忽略这一段，按原有约束写。"
STYLE_PLACEHOLDER = "{{style_sample}}"


def _style_body(raw: str) -> str:
    """从 style_sample.md 里取出正文。

    文件里有 `# 风格样本` 标题和一行 `（由用户填写：……）` 的说明，
    这两样是给人看的，不能喂给模型。但它们只可能在开头，
    所以只从头部剥，不动正文内部的空行和分段——段落节奏本身就是风格的一部分。
    """
    text = (raw or "").replace("\r\n", "\n")

    def is_note(line: str) -> bool:
        s = line.strip()
        return (s.startswith("（") and s.endswith("）")) or (
            s.startswith("(") and s.endswith(")")
        )

    lines = text.split("\n")
    while lines and (
        not lines[0].strip() or lines[0].strip().startswith("#") or is_note(lines[0])
    ):
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def load_style_sample() -> str:
    """读用户填的风格样本正文，没有就返回空串。

    优先级：data/style_sample.md（用户在页面保存的）> prompts/style_sample.md（空模板）。
    每次生成都重新读盘，所以保存完下一次生成立即生效，不用重启服务。
    """
    for path in (config.STYLE_SAMPLE_PATH, config.STYLE_SAMPLE_TEMPLATE):
        try:
            if path.exists():
                body = _style_body(path.read_text(encoding="utf-8"))
                if body:
                    return body
        except OSError:
            continue
    return ""


def _strip_style_block(text: str) -> str:
    """把风格样本段整段摘掉，还原成加这个功能之前的提示词。"""
    i = text.find("\n" + STYLE_BLOCK_START)
    if i < 0:
        i = text.find(STYLE_BLOCK_START)
    if i < 0:
        return text
    j = text.find(STYLE_BLOCK_END, i)
    if j < 0:
        return text
    end = j + len(STYLE_BLOCK_END)
    if text[end : end + 1] == "\n":
        end += 1
    return text[:i] + text[end:]


def _style_targets(sample: str) -> str:
    """把风格样本翻译成模型能照着核对的数字。

    为什么要有这一段：只写「学它的句长节奏、口语程度」实测是无效的。
    拿一段平均句长 9.3 字的样本去生成，连跑三次，输出平均句长分别是
    25.3 / 24.9 / 26.7 字，一个 25 字以内的短句都没有，等于完全没听。
    换成可数的上限之后，同一条样本生成出来是 15.3 / 15.0 字，口语标记从 0 涨到 4-7。
    结论：抽象的「节奏」模型抓不住，能数的指标它才服。
    """
    sents = [s for s in re.split(r"[。！？；\n]", sample) if s.strip()]
    if len(sents) < 3:
        return ""
    lens = [len(s) for s in sents]
    avg = sum(lens) / len(lens)
    long_ratio = sum(1 for x in lens if x > 25) / len(lens) * 100

    # 上限留出余量再取整。这里有过一次教训：第一版卡在「平均句长不超过 14 字 /
    # 超 40 字就拆成两句」，数字是压下来了（12.0 字），但模型开始把一句话砍成两个残句，
    # 写出「我见过的一个同行。完全依赖AI写方案。」这种病句——那是断句，不是短句。
    # 所以放宽到「样本句长 + 8」，并把要求从「拆」改成「拆成两个完整句」。
    cap = max(16, int(round(avg)) + 8)
    long_cap = max(15, int(round(long_ratio)) + 15)

    return (
        "这篇的硬指标（照样本算出来的，必须达到）：\n"
        f"- 样本平均句长 {avg:.1f} 字，你写的正文平均句长控制在 {cap} 字以内。\n"
        f"- 超过 25 字的句子不超过全文句数的 {long_cap}%。\n"
        "- 每个小节至少出现 2 处口语转折（你看 / 其实 / 就 / 那），"
        "或一句真正独立成段的短句。\n"
        "- 长句该拆就拆，但必须拆成两个语法完整的句子。"
        "像「我见过的一个同行。完全依赖AI写方案。」这种是病句，不算短句。"
    )


CARD_BLOCK_START = "本内容基于以下观点卡"
CARD_BLOCK_END = "（若一手细节为空，禁止使用任何第一人称经历叙述）"


def _strip_card_block(text: str) -> str:
    """把观点卡段整段摘掉。

    没有 card 的时候必须摘掉而不是把占位符换成空串：留着「你只能围绕这个主张展开」
    却没有主张，模型会自己编一个主张出来，那比没有这段更糟。
    """
    i = text.find(CARD_BLOCK_START)
    if i < 0:
        return text
    j = text.find(CARD_BLOCK_END, i)
    if j < 0:
        return text
    end = j + len(CARD_BLOCK_END)
    if text[end : end + 1] == "\n":
        end += 1
    # 顺手吃掉段后多余空行，保证摘完和插入前的原文逐字一致
    while text[end : end + 1] == "\n":
        end += 1
    return text[:i] + text[end:]


def _card_values(card: dict) -> dict:
    """观点卡 → 提示词变量。own_detail 为空就真的是空，不补任何占位话术。"""
    supports = card.get("supports") or []
    return {
        "claim": card.get("claim") or "",
        "supports": "；".join(supports) if supports else "（无）",
        "counter": card.get("counter") or "（无）",
        "own_detail": card.get("own_detail") or "",
    }


def render_prompt(name: str, values: dict, card: dict | None = None) -> str:
    """读取 prompts/<name>.md 并替换 {{变量}}。

    两个横切的东西在这里统一注入，写这个函数的时候只想着「占位符替换」：
    - 观点卡：谁家 md 里有那段观点卡就吃 card 参数，没有就整段摘掉
    - 风格样本：同上
    cards.md 现在也有观点卡段，但它没有 {{style_sample}}，两套互不干扰。
    """
    text = (config.PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")

    if CARD_BLOCK_START in text:
        if card:
            values = {**_card_values(card), **dict(values)}
        else:
            text = _strip_card_block(text)

    has_style_slot = STYLE_PLACEHOLDER in text
    sample = ""
    if has_style_slot:
        sample = load_style_sample()
        if sample:
            values = dict(values)
            values["style_sample"] = sample
        else:
            # 没填样本：整段摘掉，提示词逐字回到加这个功能之前
            text = _strip_style_block(text)

    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))

    if has_style_slot and sample:
        text = text.rstrip("\n") + "\n\n" + _style_targets(sample) + "\n"

    return text



def _save_content(kind: str, title: str, result: dict, params: dict, meta: dict) -> int:
    """写一条 contents 记录，返回主键。"""
    return db.execute(
        "INSERT INTO contents(kind, topic_id, title, body, meta, model, prompt_ver, cost, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (
            kind,
            None,
            title,
            json.dumps(result, ensure_ascii=False),
            json.dumps(params, ensure_ascii=False),
            meta.get("model", ""),
            config.PROMPT_VER,
            float(meta.get("cost") or 0),
            db.now_str(),
        ),
    )


def _gen_error(e: Exception):
    """把模型层异常翻译成统一失败响应。"""
    if isinstance(e, llm.LLMNotConfigured):
        return fail("未配置 ZHIPU_API_KEY", retryable=False, status_code=400)
    if isinstance(e, llm.LLMJSONError):
        return fail("模型输出非 JSON", retryable=True)
    return fail(f"模型调用失败：{e}", retryable=True)


# ---------------------------------------------------------------- 应用与生命周期


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表并在后台线程里做热点刷新，不阻塞服务就绪。"""
    db.init_db()
    threading.Thread(target=_refresh_loop, daemon=True).start()
    yield


app = FastAPI(title="Creator OS", lifespan=lifespan)

# 静态资源：卡片渲染页 /static/render/card.html 需要能被 iframe 直接加载
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/")
def index():
    """返回前端单页。"""
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/api/stats")
def api_stats():
    """首页四个统计数字，全部实时读库。"""
    week = db.query_one(
        "SELECT COUNT(*) AS n FROM contents WHERE created_at >= datetime('now', 'localtime', '-7 days')"
    )
    hot = db.query_one(
        "SELECT COUNT(*) AS n FROM topics t WHERE t.fetched_at = "
        "(SELECT MAX(fetched_at) FROM topics x WHERE x.platform = t.platform)"
    )
    post = db.query_one("SELECT COUNT(*) AS n FROM posts")
    return ok(
        {
            "week_generated": int(week["n"]) if week else 0,
            "hot_count": int(hot["n"]) if hot else 0,
            "post_count": int(post["n"]) if post else 0,
            "streak_days": db.touch_streak(),
        }
    )


@app.post("/api/hot/refresh")
def api_hot_refresh():
    """手动触发四个源全量刷新。"""
    try:
        return ok(refresh_hot())
    except Exception as e:
        return fail(f"刷新失败：{type(e).__name__}: {e}", retryable=True, status_code=502)


@app.get("/api/hot")
def api_hot(
    platform: str = Query(default="", description="平台过滤，留空为全部"),
    q: str = Query(default="", description="标题关键词"),
    limit: int = Query(default=50, ge=1, le=500),
):
    """读取热点。每个平台只取各自最新一批，避免历史批次重复。"""
    sql = """
        SELECT platform, title, heat, url, fetched_at
        FROM topics t
        WHERE t.fetched_at = (
            SELECT MAX(fetched_at) FROM topics x WHERE x.platform = t.platform
        )
    """
    params: list = []
    if platform:
        sql += " AND t.platform = ?"
        params.append(platform)
    if q:
        sql += " AND t.title LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY t.fetched_at DESC, t.heat DESC LIMIT ?"
    params.append(limit)

    rows = db.query(sql, params)
    if not rows:
        # 库里没有数据时给出明确提示，而不是返回空数组装作正常
        return fail("暂无热点数据，请先调用 POST /api/hot/refresh", retryable=True)
    return ok(rows)


# ---------------------------------------------------------------- 生成接口


class ArticleReq(BaseModel):
    keyword: str
    reader: str = "普通读者"
    tone: str = "平实克制"
    length: str = "1200-1500字"
    structure: str = "总分总，3-4个小节"


class NoteReq(BaseModel):
    topic: str
    platform: str = "小红书"
    style: str = "真诚分享、口语化"


class ScriptReq(BaseModel):
    topic: str
    duration: int = 60
    hook_type: str = "反常识"
    reader: str = "普通观众"


@app.post("/api/gen/article")
def gen_article(req: ArticleReq):
    """生成公众号长文。"""
    prompt = render_prompt(
        "article",
        {
            "keyword": req.keyword,
            "reader": req.reader,
            "tone": req.tone,
            "length": req.length,
            "structure": req.structure,
        },
    )
    try:
        data, meta = llm.chat_json([{"role": "user", "content": prompt}], temperature=0.7)
    except Exception as e:
        return _gen_error(e)

    if not isinstance(data, dict) or not data.get("title") or not isinstance(data.get("sections"), list):
        return fail("模型返回结构不符合要求（缺 title 或 sections）", retryable=True)

    result = {
        "title": str(data.get("title") or ""),
        "lead": str(data.get("lead") or ""),
        "sections": [
            {"h": str(s.get("h") or ""), "p": str(s.get("p") or "")}
            for s in data["sections"]
            if isinstance(s, dict)
        ],
        "end": str(data.get("end") or ""),
    }
    _save_content("article", result["title"], result, req.model_dump(), meta)
    try:
        db.points_change("生成内容 · article", -POINT_COST["article"])
    except Exception as e:
        print(f"[points] article 扣分失败（不影响生成）：{type(e).__name__}: {e}")
    return ok(result)


@app.post("/api/gen/note")
def gen_note(req: NoteReq):
    """生成小红书笔记。"""
    prompt = render_prompt(
        "note",
        {"topic": req.topic, "style": f"{req.style}（发布平台：{req.platform}）"},
    )
    try:
        data, meta = llm.chat_json([{"role": "user", "content": prompt}], temperature=0.8)
    except Exception as e:
        return _gen_error(e)

    if not isinstance(data, dict) or not data.get("title") or not data.get("body"):
        return fail("模型返回结构不符合要求（缺 title 或 body）", retryable=True)

    result = {
        "title": str(data.get("title") or ""),
        "body": str(data.get("body") or ""),
        "tags": [str(t) for t in (data.get("tags") or [])],
        "image_hints": [str(t) for t in (data.get("image_hints") or [])],
    }
    _save_content("note", result["title"], result, req.model_dump(), meta)
    try:
        db.points_change("生成内容 · note", -POINT_COST["note"])
    except Exception as e:
        print(f"[points] note 扣分失败（不影响生成）：{type(e).__name__}: {e}")
    return ok(result)


@app.post("/api/gen/script")
def gen_script(req: ScriptReq):
    """生成短视频口播脚本。"""
    prompt = render_prompt(
        "script",
        {
            "topic": req.topic,
            "duration": req.duration,
            "hook_type": req.hook_type,
            "reader": req.reader,
        },
    )
    try:
        data, meta = llm.chat_json([{"role": "user", "content": prompt}], temperature=0.8)
    except Exception as e:
        return _gen_error(e)

    if not isinstance(data, dict) or not data.get("hook") or not isinstance(data.get("segments"), list):
        return fail("模型返回结构不符合要求（缺 hook 或 segments）", retryable=True)

    result = {
        "hook": str(data.get("hook") or ""),
        "segments": [
            {
                "t": str(s.get("t") or ""),
                "line": str(s.get("line") or ""),
                "shot": str(s.get("shot") or ""),
            }
            for s in data["segments"]
            if isinstance(s, dict)
        ],
        "cta": str(data.get("cta") or ""),
        "caption": str(data.get("caption") or ""),
        "hashtags": [str(t) for t in (data.get("hashtags") or [])],
    }
    _save_content("script", req.topic, result, req.model_dump(), meta)
    try:
        db.points_change("生成内容 · script", -POINT_COST["script"])
    except Exception as e:
        print(f"[points] script 扣分失败（不影响生成）：{type(e).__name__}: {e}")
    return ok(result)


# ---------------------------------------------------------------- 贴图卡片


# 允许的 8 种版式，与前端 Canvas 渲染器一一对应
LAYOUTS = [
    "big-center",
    "three-row",
    "quad-grid",
    "dark-reverse",
    "left-line",
    "quote-center",
    "timeline",
    "cover-photo",
]

LAYOUT_SET = set(LAYOUTS)


class CardsReq(BaseModel):
    theme: str
    count: int = 6
    style: str = "简约大字"
    person: str = "无人物"
    reader: str = ""
    type: str = "知识科普"


def _normalize_cards(data: dict, count: int) -> list[dict]:
    """把模型输出规范成固定结构：补默认值、兜底非法 layout、修正相邻重复版式。"""
    raw = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []

    out: list[dict] = []
    for i, item in enumerate(raw[:count]):
        if not isinstance(item, dict):
            continue
        layout = str(item.get("layout") or "").strip()
        if layout not in LAYOUT_SET:
            layout = "big-center"

        k = str(item.get("k") or "").strip()
        if not k:
            k = "cover" if i == 0 else ("end" if i == len(raw) - 1 else "list")

        items = item.get("items")
        if not isinstance(items, list):
            items = []

        out.append(
            {
                "k": k,
                "title": str(item.get("title") or "").strip(),
                "sub": str(item.get("sub") or "").strip(),
                "items": [str(x).strip() for x in items if str(x).strip()],
                "layout": layout,
            }
        )

    # 相邻两张版式相同就换一个，保证整组看起来不重复
    for i in range(1, len(out)):
        if out[i]["layout"] == out[i - 1]["layout"]:
            for cand in LAYOUTS:
                if cand != out[i - 1]["layout"]:
                    out[i]["layout"] = cand
                    break

    return out


@app.post("/api/gen/cards")
def gen_cards(req: CardsReq):
    """生成贴图卡片计划。AI 只出文案与版式，图片由前端 Canvas 渲染。"""
    count = max(3, min(9, int(req.count or 6)))
    prompt = render_prompt(
        "cards",
        {
            "theme": req.theme,
            "count": count,
            "style": req.style,
            "person": req.person,
            "reader": req.reader or "普通读者",
            "type": req.type,
        },
    )

    best_cards: list[dict] = []
    best_meta: dict = {}
    for temperature in (0.7, 0.85):
        try:
            data, meta = llm.chat_json([{"role": "user", "content": prompt}], temperature=temperature)
        except Exception as e:
            return _gen_error(e)

        cards = _normalize_cards(data, count)
        if len(cards) > len(best_cards):
            best_cards, best_meta = cards, meta
        if len(cards) >= count:
            best_cards, best_meta = cards, meta
            break

    if not best_cards:
        return fail("模型返回的卡片为空", retryable=True)

    result = {"cards": best_cards}
    _save_content("cards", req.theme, result, req.model_dump(), best_meta)
    try:
        db.points_change("生成内容 · cards", -POINT_COST["cards"])
    except Exception as e:
        print(f"[points] cards 扣分失败（不影响生成）：{type(e).__name__}: {e}")
    return ok(result)


# ---------------------------------------------------------------- 发布记录与复盘


# ===== 全站唯一口径 =====
# 互动率 rate = (like_n + share_n + comment_n) / read_n，read_n 为 0 时存 NULL。
# 任何写入 posts.rate 的地方都必须调用 calc_rate()，不许在别处重复实现这套算法，
# 否则前后端口径会分叉，复盘结论就不可信。
RATE_NUMERATOR = ("like_n", "share_n", "comment_n")
RATE_DENOMINATOR = "read_n"


def calc_rate(read_n, like_n, share_n, comment_n):
    """按全站唯一口径计算互动率。阅读数 <= 0 时返回 None（入库为 NULL）。"""
    try:
        reads = int(read_n or 0)
    except (TypeError, ValueError):
        reads = 0
    if reads <= 0:
        return None
    try:
        total = int(like_n or 0) + int(share_n or 0) + int(comment_n or 0)
    except (TypeError, ValueError):
        total = 0
    return round(total / reads, 6)


class PostReq(BaseModel):
    title: str = ""
    content_id: int | None = None
    platform: str
    url: str = ""
    published_at: str = ""
    read_n: int = 0
    like_n: int = 0
    share_n: int = 0
    comment_n: int = 0


@app.post("/api/posts")
def create_post(req: PostReq):
    """录入一条发布记录，rate 由服务端统一计算。"""
    if not (req.platform or "").strip():
        return fail("platform 不能为空", retryable=False)

    rate = calc_rate(req.read_n, req.like_n, req.share_n, req.comment_n)
    published_at = (req.published_at or "").strip() or db.now_str()

    new_id = db.execute(
        "INSERT INTO posts(title, content_id, platform, url, published_at,"
        " read_n, like_n, share_n, comment_n, rate)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            req.title.strip(),
            req.content_id,
            req.platform.strip(),
            req.url.strip(),
            published_at,
            int(req.read_n or 0),
            int(req.like_n or 0),
            int(req.share_n or 0),
            int(req.comment_n or 0),
            rate,
        ),
    )
    row = db.query_one("SELECT * FROM posts WHERE id = ?", (new_id,))
    return ok(row)


@app.get("/api/posts")
def list_posts(days: int = Query(default=14, ge=1, le=365)):
    """按最近 N 天返回发布记录（含关联内容的标题兜底）。"""
    rows = db.query(
        "SELECT p.*, COALESCE(NULLIF(p.title, ''), c.title, '') AS title_resolved"
        " FROM posts p LEFT JOIN contents c ON c.id = p.content_id"
        " WHERE date(COALESCE(NULLIF(p.published_at, ''), p.published_at)) >= date('now','localtime', ?)"
        " ORDER BY p.published_at DESC, p.id DESC",
        (f"-{days - 1} days",),
    )
    for r in rows:
        r["title"] = r.get("title_resolved") or r.get("title") or ""
        r.pop("title_resolved", None)
    return ok({"days": days, "count": len(rows), "items": rows})


class ReviewReq(BaseModel):
    period_start: str
    period_end: str


def _posts_table_markdown(rows: list[dict]) -> str:
    """把发布记录拼成 markdown 表格，喂给模型。"""
    lines = ["| 标题 | 平台 | 发布日期 | 阅读 | 点赞 | 转发 | 评论 | 互动率 |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        rate = r.get("rate")
        rate_txt = "—" if rate is None else f"{float(rate) * 100:.2f}%"
        title = str(r.get("title") or "").replace("|", "/").replace("\n", " ")
        published = str(r.get("published_at") or "")[:16]
        lines.append(
            f"| {title} | {r.get('platform') or ''} | {published} | {r.get('read_n') or 0} |"
            f" {r.get('like_n') or 0} | {r.get('share_n') or 0} | {r.get('comment_n') or 0} | {rate_txt} |"
        )
    return "\n".join(lines)


@app.post("/api/review")
def create_review(req: ReviewReq):
    """取区间内发布记录，让模型做归因分析，结果写入 reviews 表。"""
    start = (req.period_start or "").strip()
    end = (req.period_end or "").strip()
    if not start or not end:
        return fail("period_start 与 period_end 都不能为空", retryable=False)

    rows = db.query(
        "SELECT p.*, COALESCE(NULLIF(p.title, ''), c.title, '') AS title_resolved"
        " FROM posts p LEFT JOIN contents c ON c.id = p.content_id"
        " WHERE date(p.published_at) BETWEEN date(?) AND date(?)"
        " ORDER BY p.published_at ASC, p.id ASC",
        (start, end),
    )
    if not rows:
        return fail(f"{start} 至 {end} 之间没有发布记录，先录入数据再复盘", retryable=False)

    for r in rows:
        r["title"] = r.get("title_resolved") or r.get("title") or ""

    table = _posts_table_markdown(rows)
    prompt = render_prompt(
        "review",
        {
            "period_start": start,
            "period_end": end,
            "count": len(rows),
            "table": table,
        },
    )

    try:
        data, meta = llm.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.4,
            timeout=60,
        )
    except Exception as e:
        return _gen_error(e)

    parts = [str(data.get(k) or "").strip() for k in ("part1", "part2", "part3")]
    if not any(parts):
        return fail("模型返回结构不符合要求（缺 part1/part2/part3）", retryable=True)

    report = "\n\n".join([p for p in parts if p])
    review_id = db.execute(
        "INSERT INTO reviews(period_start, period_end, report, model, created_at) VALUES(?,?,?,?,?)",
        (start, end, report, meta.get("model", ""), db.now_str()),
    )

    return ok(
        {
            "id": review_id,
            "period_start": start,
            "period_end": end,
            "count": len(rows),
            "sections": [p for p in parts if p],
            "report": report,
            "model": meta.get("model", ""),
        }
    )


@app.get("/api/review")
def list_reviews(limit: int = Query(default=10, ge=1, le=50)):
    """历史复盘列表。"""
    return ok(db.query("SELECT * FROM reviews ORDER BY id DESC LIMIT ?", (limit,)))


# ---------------------------------------------------------------- 母题库
# 母题是用户的立场资产，不是内容草稿。后面所有生成都得挂到某条母题上，
# 所以这里的校验比别的表严：title 8 字内、stance 40 字内，超了直接拒。
# 放任写成长段落，派生观点卡时 claim 就压不出来。

THESIS_TITLE_MAX = 8
THESIS_STANCE_MAX = 40
WEIGHT_MIN, WEIGHT_MAX = 0.3, 3.0


def _parse_evidence(raw) -> list[str]:
    """evidence 在库里存 JSON 数组字符串，对外统一成 list[str]。"""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if str(x).strip()]


def _thesis_out(row: dict) -> dict:
    """库行 → 对外结构：evidence 反序列化，并标出 incomplete。

    incomplete 不是错误状态，是「这条母题还没接到地上」的提示。
    它能被选中，但生成时必须走降级路径，不许拿它编一手经历。
    """
    if not row:
        return {}
    ev = _parse_evidence(row.get("evidence"))
    out = dict(row)
    out["evidence"] = ev
    out["evidence_count"] = len(ev)
    out["incomplete"] = len(ev) == 0
    return out


def _validate_thesis(title: str, stance: str) -> str | None:
    """返回中文错误信息，None 表示通过。"""
    if not title:
        return "母题名不能为空"
    if len(title) > THESIS_TITLE_MAX:
        return f"母题名不超过 {THESIS_TITLE_MAX} 个字，现在是 {len(title)} 个"
    if not stance:
        return "主张不能为空"
    if len(stance) > THESIS_STANCE_MAX:
        return f"主张不超过 {THESIS_STANCE_MAX} 个字，现在是 {len(stance)} 个"
    return None


class ThesisReq(BaseModel):
    title: str = ""
    stance: str = ""
    against: str = ""
    evidence: list[str] = []
    keywords: str = ""


class ThesisPatch(BaseModel):
    """修改用：只传要改的字段，没传的保持原样。"""
    title: str | None = None
    stance: str | None = None
    against: str | None = None
    evidence: list[str] | None = None
    keywords: str | None = None


@app.get("/api/theses/health")
def theses_health():
    """母题库健康度：总数、有一手素材的、待补的、平均权重。

    health 必须声明在 /api/theses/{id} 之前，否则会被路径参数吃掉。
    """
    rows = db.query("SELECT evidence, weight FROM theses")
    total = len(rows)
    with_ev = sum(1 for r in rows if _parse_evidence(r.get("evidence")))
    avg = round(sum(float(r.get("weight") or 0) for r in rows) / total, 3) if total else 0.0
    return ok(
        {
            "total": total,
            "with_evidence": with_ev,
            "empty_evidence": total - with_ev,
            "avg_weight": avg,
        }
    )


@app.get("/api/theses")
def list_theses():
    """母题列表，按权重倒序（最懂你的排前面）。"""
    rows = db.query("SELECT * FROM theses ORDER BY weight DESC, id ASC")
    items = [_thesis_out(r) for r in rows]
    return ok({"count": len(items), "items": items})


@app.post("/api/theses")
def create_thesis(req: ThesisReq):
    """新建母题。"""
    title = (req.title or "").strip()
    stance = (req.stance or "").strip()
    err = _validate_thesis(title, stance)
    if err:
        return fail(err)

    ev = [str(x).strip() for x in (req.evidence or []) if str(x).strip()]
    new_id = db.execute(
        "INSERT INTO theses(title, stance, against, evidence, keywords, weight, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            title,
            stance,
            (req.against or "").strip(),
            json.dumps(ev, ensure_ascii=False),
            (req.keywords or "").strip(),
            1.0,
            db.now_str(),
        ),
    )
    return ok(_thesis_out(db.query_one("SELECT * FROM theses WHERE id = ?", (new_id,))))


@app.put("/api/theses/{thesis_id}")
def update_thesis(thesis_id: int, req: ThesisPatch):
    """修改母题，只改传了的字段。"""
    old = db.query_one("SELECT * FROM theses WHERE id = ?", (thesis_id,))
    if not old:
        return fail(f"母题 {thesis_id} 不存在")

    title = old["title"] if req.title is None else (req.title or "").strip()
    stance = old["stance"] if req.stance is None else (req.stance or "").strip()
    err = _validate_thesis(title, stance)
    if err:
        return fail(err)

    against = old.get("against") or "" if req.against is None else (req.against or "").strip()
    keywords = old.get("keywords") or "" if req.keywords is None else (req.keywords or "").strip()
    if req.evidence is None:
        evidence = old.get("evidence") or "[]"
    else:
        ev = [str(x).strip() for x in req.evidence if str(x).strip()]
        evidence = json.dumps(ev, ensure_ascii=False)

    db.execute(
        "UPDATE theses SET title=?, stance=?, against=?, evidence=?, keywords=? WHERE id=?",
        (title, stance, against, evidence, keywords, thesis_id),
    )
    return ok(_thesis_out(db.query_one("SELECT * FROM theses WHERE id = ?", (thesis_id,))))


@app.delete("/api/theses/{thesis_id}")
def delete_thesis(thesis_id: int):
    """删除母题。已经派生的观点卡一并删掉，不留孤儿卡。"""
    old = db.query_one("SELECT id FROM theses WHERE id = ?", (thesis_id,))
    if not old:
        return fail(f"母题 {thesis_id} 不存在")

    cards = 0
    has_cards = db.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='thesis_cards'"
    )
    if has_cards:
        row = db.query_one("SELECT COUNT(*) AS n FROM thesis_cards WHERE thesis_id = ?", (thesis_id,))
        cards = int(row["n"]) if row else 0
        db.execute("DELETE FROM thesis_cards WHERE thesis_id = ?", (thesis_id,))

    db.execute("DELETE FROM theses WHERE id = ?", (thesis_id,))
    return ok({"deleted": thesis_id, "cards_deleted": cards})



# ---------------------------------------------------------------- 风格样本


class StyleReq(BaseModel):
    sample: str = ""


@app.get("/api/style")
def get_style():
    """读当前风格样本。active=False 表示还没填，生成行为和加这个功能之前一致。"""
    body = load_style_sample()
    return ok(
        {
            "sample": body,
            "length": len(body),
            "active": bool(body),
            "path": str(config.STYLE_SAMPLE_PATH),
        }
    )


@app.put("/api/style")
def put_style(req: StyleReq):
    """保存风格样本。

    刻意不截断也不压缩：句长和分段本身就是风格的一部分，截一刀就把节奏截没了。
    代价是样本写太长会吃 token，这由用户在页面上自己控制。
    写完立刻落盘，下次生成直接读到，不需要重启服务。
    """
    body = _style_body(req.sample)
    path = config.STYLE_SAMPLE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    if body:
        content = "# 风格样本\n\n" + body + "\n"
    else:
        # 清空后回写成模板形态，而不是留一个空文件——空文件下次打开看不出该填什么
        content = (
            "# 风格样本\n\n"
            "（由用户填写：一段他自己满意、愿意反复模仿的原创文字，200-400 字）\n"
        )
    path.write_text(content, encoding="utf-8")

    return ok(
        {
            "sample": body,
            "length": len(body),
            "active": bool(body),
            "path": str(path),
        }
    )


# ---------------------------------------------------------------- 历史与设置


@app.get("/api/contents")
def api_contents(
    kind: str = Query(default="", description="article | note | script，留空为全部"),
    limit: int = Query(default=20, ge=1, le=200),
):
    """历史列表，不含正文全文。"""
    sql = "SELECT id, kind, title, created_at FROM contents"
    params: list = []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return ok(db.query(sql, params))


@app.get("/api/contents/{content_id}")
def api_content_detail(content_id: int):
    """单条完整内容。"""
    row = db.query_one("SELECT * FROM contents WHERE id = ?", (content_id,))
    if not row:
        return fail("内容不存在", retryable=False, status_code=404)
    # body 存的是 JSON 字符串，返回时还原成对象
    try:
        row["body"] = json.loads(row["body"]) if row["body"] else None
    except json.JSONDecodeError:
        pass
    try:
        row["meta"] = json.loads(row["meta"]) if row["meta"] else None
    except json.JSONDecodeError:
        pass
    return ok(row)


# 密钥类键名一律不出现在读接口里。就算以后有人把 key 存进 settings，
# 这个接口也不会把它吐给浏览器——泄露一次就够完蛋了。
SENSITIVE_KEY_WORDS = ("key", "secret", "token", "password", "passwd", "credential")


def _is_sensitive(key: str) -> bool:
    k = (key or "").lower()
    return any(w in k for w in SENSITIVE_KEY_WORDS)


@app.get("/api/settings")
def api_settings_get():
    """读取设置键值（密钥类键名会被过滤掉）。"""
    rows = db.query("SELECT key, value FROM settings ORDER BY key")
    return ok({r["key"]: r["value"] for r in rows if not _is_sensitive(r["key"])})


class SettingReq(BaseModel):
    key: str
    value: str


@app.put("/api/settings")
def api_settings_put(req: SettingReq):
    """写入单个设置项（存在则覆盖）。"""
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (req.key, req.value),
    )
    return ok({"key": req.key, "value": req.value})


# ================================================================
# 观点卡
#
# 命名纪律：贴图卡片占的是 templates 表 + /api/gen/cards（复数），
# 观点卡一律单数 thesis_cards + /api/gen/card + /api/cards。
# 两套东西长得很像但不是一回事，改名字会直接把前端打挂。
# ================================================================


def _json_list(raw) -> list[str]:
    """把 JSON 数组字符串读成 list[str]，脏数据一律当空列表。"""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if str(x).strip()]


def _card_out(row: dict) -> dict:
    """thesis_cards 行 → 对外结构。supports / missing 都反序列化成数组。"""
    if not row:
        return {}
    out = dict(row)
    out["supports"] = _json_list(row.get("supports"))
    out["missing"] = _json_list(row.get("missing"))
    out["own_detail"] = row.get("own_detail") or ""
    return out


class CardReq(BaseModel):
    thesis_id: int
    topic_id: int | None = None


@app.post("/api/gen/card")
def gen_card(req: CardReq):
    """从一条母题压出一张观点卡。

    这里最要紧的不是 prompt，是下面的兜底：模型很爱把 own_detail「润色」一下，
    或者干脆编一段。所以入库前一律做逐字校验，对不上就换回素材原文。
    红线不能靠提示词守，得靠代码守。
    """
    th = db.query_one("SELECT * FROM theses WHERE id = ?", (req.thesis_id,))
    if not th:
        return fail(f"母题 {req.thesis_id} 不存在")

    evidence = _parse_evidence(th.get("evidence"))
    ev_text = "\n".join("- " + e for e in evidence) if evidence else "（空，这条母题还没有一手素材）"

    if req.topic_id:
        topic = db.query_one("SELECT id FROM topics WHERE id = ?", (req.topic_id,))
        if not topic:
            req.topic_id = None

    prompt = render_prompt(
        "card",
        {
            "thesis_title": th["title"],
            "stance": th["stance"],
            "against": (th.get("against") or "").strip() or "（未填）",
            "evidence": ev_text,
        },
    )
    try:
        data, meta = llm.chat_json([{"role": "user", "content": prompt}], temperature=0.5, timeout=60)
    except Exception as e:
        return _gen_error(e)

    claim = str(data.get("claim") or "").strip()
    if not claim:
        return fail("模型返回结构不符合要求（缺 claim）", retryable=True)

    supports = [str(x).strip() for x in (data.get("supports") or []) if str(x).strip()][:3]
    counter = str(data.get("counter") or "").strip()
    missing = [str(x).strip() for x in (data.get("missing") or []) if str(x).strip()][:3]

    # ---- 一手细节红线：只认逐字命中
    own = str(data.get("own_detail") or "").strip()
    if not evidence:
        own = ""
        if not any("一手经历" in m for m in missing):
            missing = (["缺一手经历，请补一条亲历"] + missing)[:3]
    elif own not in evidence:
        # 模型改写了、或者编了。不接受「相似」，直接换回第一条素材原文，
        # 这样至少保证用户看到的是他自己写的话。
        own = evidence[0]

    new_id = db.execute(
        "INSERT INTO thesis_cards(thesis_id, topic_id, claim, supports, counter,"
        " own_detail, missing, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            req.thesis_id,
            req.topic_id,
            claim,
            json.dumps(supports, ensure_ascii=False),
            counter,
            own,
            json.dumps(missing, ensure_ascii=False),
            db.now_str(),
        ),
    )
    row = db.query_one("SELECT * FROM thesis_cards WHERE id = ?", (new_id,))
    out = _card_out(row)
    out["thesis_title"] = th["title"]
    out["model"] = meta.get("model", "")
    try:
        db.points_change("生成内容 · card", -POINT_COST["card"])
    except Exception as e:
        print(f"[points] card 扣分失败（不影响生成）：{type(e).__name__}: {e}")
    return ok(out)


@app.get("/api/cards")
def list_cards(thesis_id: int = Query(default=0)):
    """观点卡列表。带 thesis_id 就只看这条母题的。"""
    sql = (
        "SELECT c.*, t.title AS thesis_title FROM thesis_cards c"
        " LEFT JOIN theses t ON t.id = c.thesis_id"
    )
    params: list = []
    if thesis_id:
        sql += " WHERE c.thesis_id = ?"
        params.append(thesis_id)
    sql += " ORDER BY c.id DESC"
    rows = db.query(sql, params)
    return ok({"count": len(rows), "items": [_card_out(r) for r in rows]})


@app.get("/api/cards/{card_id}")
def get_card(card_id: int):
    """单张观点卡。"""
    row = db.query_one(
        "SELECT c.*, t.title AS thesis_title FROM thesis_cards c"
        " LEFT JOIN theses t ON t.id = c.thesis_id WHERE c.id = ?",
        (card_id,),
    )
    if not row:
        return fail(f"观点卡 {card_id} 不存在")
    return ok(_card_out(row))


@app.delete("/api/cards/{card_id}")
def delete_card(card_id: int):
    """删一张观点卡。"""
    row = db.query_one("SELECT id FROM thesis_cards WHERE id = ?", (card_id,))
    if not row:
        return fail(f"观点卡 {card_id} 不存在")
    db.execute("DELETE FROM thesis_cards WHERE id = ?", (card_id,))
    return ok({"deleted": card_id})


# ================================================================
# 热点挂载
#
# 这里分两条路，故意的：
#
# 1) 批量打分（POST /api/hot/match）不调模型，纯关键词算分。
#    以前是「关键词 60 + 模型 40」，一次 20 条要 237 秒 —— 因为
#    MODEL_CHAIN 三个模型 × 每个重试 2 次 × timeout 90 串起来跑。
#    页面一进就卡死，这个功能等于不能用。批量只做「哪几个母题可能相关」，
#    这件事用可复现的关键词分就够了，先出结果比出得准重要。
#
# 2) 单条角度（POST /api/hot/match/{topic_id}/angle）才调一次模型。
#    用户点开某条热点、真的在看那 3 个母题的时候，才值得花 3 秒换一句
#    具体的切入角度。前端先显示模板 angle，模型回来了就地替换。
#
# 关键词分单独看的毛病是「大部分热点标题根本不含母题关键词」——
# 实测 20 条热点 × 9 个母题，命中数 0。所以批量必须有权重兜底，
# 否则前端会显示「这条和你的母题都不搭」，而真相只是没撞上关键词。
# 兜底条 matched_by="weight"、score=0，前端据此换文案，不假装是匹配。
# ================================================================

MATCH_KEYWORD_MAX = 60.0
MATCH_MODEL_MAX = 40.0
MATCH_MIN_SCORE = 20  # 低于这个分还算「匹配」就是自欺欺人，直接不返回
MATCH_ANGLE_MAX = 30  # 角度是「一句话」，超过这个长度就不是一句话了
ANGLE_TIMEOUT = 12  # glm-4.5-air 在这类任务上实测 6-10s，卡 10s 会把正常调用打断成重试


def _latest_topics(limit: int) -> list[dict]:
    """每个平台只取自己最新一批，和 /api/hot 同一口径。

    这里原来是关联子查询（WHERE fetched_at = (SELECT MAX(...) WHERE platform=t.platform)），
    topics 攒到 4000 行之后这条 SQL 要 300ms —— 每行都跑一次子查询，ORDER BY 之前
    一行都少不了。改成 GROUP BY 出一张「平台→最新批次」的小表再 JOIN，5ms。
    结果集逐字相同，只是不再把 LIMIT 之前的工作全做一遍。
    """
    return db.query(
        """
        SELECT t.id, t.platform, t.title, t.heat, t.fetched_at
        FROM topics t
        JOIN (
            SELECT platform, MAX(fetched_at) AS mf FROM topics GROUP BY platform
        ) g ON g.platform = t.platform AND g.mf = t.fetched_at
        ORDER BY t.heat DESC, t.id DESC LIMIT ?
        """,
        (limit,),
    )


def _split_keywords(keywords: str) -> list[str]:
    return [k.strip() for k in (keywords or "").replace("，", ",").split(",") if k.strip()]


def _keyword_score(title: str, keywords: str) -> float:
    """标题命中关键词的比例，0-60 分。命中 3 个就算满。"""
    kws = _split_keywords(keywords)
    if not kws:
        return 0.0
    t = (title or "").lower()
    hit = [k for k in kws if k.lower() in t]
    if not hit:
        return 0.0
    full = min(3, len(kws))
    return min(1.0, len(hit) / full) * MATCH_KEYWORD_MAX


def _ask_model_matches(topics: list[dict], theses: list[dict]) -> dict:
    """一次调用给一批热点打分。返回 {topic_id: [match,...]}。"""
    topic_lines = "\n".join(f"{t['id']}|{t['title']}" for t in topics)
    thesis_lines = "\n".join(f"{x['id']}|{x['title']}|{x['stance']}" for x in theses)
    prompt = render_prompt("match", {"topics": topic_lines, "theses": thesis_lines})

    data, _meta = llm.chat_json(
        [{"role": "user", "content": prompt}], temperature=0.3, timeout=90
    )
    out: dict[int, list[dict]] = {}
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            tid = int(item.get("topic_id"))
        except (TypeError, ValueError):
            continue
        rows = []
        for m in item.get("matches") or []:
            if not isinstance(m, dict):
                continue
            try:
                mid = int(m.get("thesis_id"))
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "thesis_id": mid,
                    "relevance": max(0, min(MATCH_MODEL_MAX, int(m.get("relevance") or 0))),
                    "angle": str(m.get("angle") or "").strip(),
                }
            )
        out[tid] = rows[:3]
    return out


def _assemble_match(topic: dict, theses_map: dict, model_rows: list[dict]) -> dict:
    """把关键词分和模型分加起来，过滤、排序、截到 3 条。"""
    matches = []
    for m in model_rows:
        th = theses_map.get(m["thesis_id"])
        if not th:
            continue
        kw = _keyword_score(topic["title"], th.get("keywords") or "")
        score = int(round(min(100.0, kw + float(m["relevance"]))))
        if score < MATCH_MIN_SCORE:
            continue
        angle = m["angle"][:20] if m["angle"] else f"借这条说「{th['title']}」"[:20]
        matches.append(
            {
                "thesis_id": th["id"],
                "thesis_title": th["title"],
                "score": score,
                "angle": angle,
                "has_evidence": bool(_parse_evidence(th.get("evidence"))),
            }
        )
    matches.sort(key=lambda x: x["score"], reverse=True)
    return {
        "topic_id": topic["id"],
        "title": topic["title"],
        "platform": topic.get("platform") or "",
        "matches": matches[:3],
    }


def _template_angle(thesis: dict) -> str:
    """模板版切入角度：把母题主张摊成一句话，不调模型。

    这不是「假 angle」，是占位。它保证点开任何一条热点都有东西可看，
    真 angle 由 /api/hot/match/{topic_id}/angle 回来后就地替换。
    """
    stance = " ".join(str(thesis.get("stance") or thesis.get("title") or "").split())
    if len(stance) > 24:
        # 硬截到 24 字会把「把二十年经验压成一个能」这种半句话留在页面上，
        # 看着像 bug。优先退到 24 字以内最后一个标点收口，实在没有才硬截。
        head = stance[:24]
        cut = max(head.rfind(p) for p in "。！？；，、：")
        stance = head[:cut] if cut >= 8 else head
    stance = stance.rstrip("。！？，、；：,.!?;:")
    return f"从「{stance}」切入"


def _keyword_matches(topic: dict, theses: list[dict]) -> list[dict]:
    """一条热点的母题候选，纯关键词，不调模型。"""
    scored = []
    for th in theses:
        kw = _keyword_score(topic["title"], th.get("keywords") or "")
        if kw >= MATCH_MIN_SCORE:
            scored.append((kw, th))
    scored.sort(key=lambda x: (-x[0], -float(x[1].get("weight") or 0), x[1]["id"]))

    rows = []
    for kw, th in scored[:3]:
        rows.append(
            {
                "thesis_id": th["id"],
                "thesis_title": th["title"],
                "score": int(round(min(100.0, kw))),
                "angle": _template_angle(th),
                "angle_source": "template",
                "matched_by": "keyword",
                "has_evidence": bool(_parse_evidence(th.get("evidence"))),
            }
        )

    if not rows and theses:
        # 一个关键词都没撞上：按母题权重（最懂你的那条）兜底一条。
        # 不出一条的话前端只能显示「这条和你的母题都不搭」，而那不是事实——
        # 事实是关键词没撞上，模型可能挂得上。真角度交给单条接口去问。
        th = theses[0]  # 已按 weight DESC, id ASC 排好
        rows.append(
            {
                "thesis_id": th["id"],
                "thesis_title": th["title"],
                "score": 0,
                "angle": _template_angle(th),
                "angle_source": "template",
                "matched_by": "weight",
                "has_evidence": bool(_parse_evidence(th.get("evidence"))),
            }
        )
    return rows


def _match_topics(limit: int) -> list[dict]:
    """批量匹配：纯关键词，不调模型，20 条目标 100ms 内返回。"""
    topics = _latest_topics(limit)
    if not topics:
        return []
    theses = db.query("SELECT * FROM theses ORDER BY weight DESC, id ASC")
    results = []
    for t in topics:
        rows = _keyword_matches(t, theses) if theses else []
        results.append(
            {
                "topic_id": t["id"],
                "title": t["title"],
                "platform": t.get("platform") or "",
                "matched_by": rows[0]["matched_by"] if rows else "none",
                "matches": rows,
            }
        )
    return results


class MatchReq(BaseModel):
    limit: int = 20


@app.post("/api/hot/match")
def hot_match(req: MatchReq):
    """一批热点的 top3 母题。纯关键词，不调模型，所以没有 model_error。"""
    limit = max(1, min(50, int(req.limit or 20)))
    items = _match_topics(limit)
    if not items:
        return fail("暂无热点数据，请先调用 POST /api/hot/refresh", retryable=True)
    hit = sum(1 for x in items if x["matches"])
    return ok({"count": len(items), "matched": hit, "items": items})


@app.post("/api/hot/match/{topic_id}")
def hot_match_one(topic_id: int):
    """单条热点的 top3 母题。"""
    topic = db.query_one(
        "SELECT id, platform, title, heat FROM topics WHERE id = ?", (topic_id,)
    )
    if not topic:
        return fail(f"热点 {topic_id} 不存在")

    theses = db.query("SELECT * FROM theses ORDER BY weight DESC, id ASC")
    if not theses:
        return ok({"topic_id": topic_id, "title": topic["title"], "matches": []})

    theses_map = {x["id"]: x for x in theses}
    model_error = ""
    try:
        model_out = _ask_model_matches([topic], theses)
    except Exception as e:
        model_out = {}
        model_error = f"{type(e).__name__}: {e}"

    rows = model_out.get(topic_id, [])
    if not rows:
        scored = sorted(
            ((_keyword_score(topic["title"], x.get("keywords") or ""), x["id"]) for x in theses),
            reverse=True,
        )
        rows = [
            {"thesis_id": tid, "relevance": 0, "angle": ""}
            for s, tid in scored[:3]
            if s >= MATCH_MIN_SCORE
        ]
    out = _assemble_match(topic, theses_map, rows)
    if model_error:
        out["model_error"] = model_error
    return ok(out)


class AngleReq(BaseModel):
    thesis_id: int


@app.post("/api/hot/match/{topic_id}/angle")
def hot_match_angle(topic_id: int, req: AngleReq):
    """单条热点 × 单个母题 → 一句具体切入角度。

    这是整个热点链路里唯一调模型的地方，一次只调一次，
    所以前端可以放心地先显示模板 angle、模型回来了再替换。
    """
    t0 = time.perf_counter()

    topic = db.query_one("SELECT id, platform, title FROM topics WHERE id = ?", (topic_id,))
    if not topic:
        return fail(f"热点 {topic_id} 不存在")
    thesis = db.query_one("SELECT * FROM theses WHERE id = ?", (int(req.thesis_id),))
    if not thesis:
        return fail(f"母题 {req.thesis_id} 不存在")

    prompt = render_prompt(
        "angle",
        {
            "topic": topic["title"],
            "thesis_title": thesis["title"],
            "stance": thesis.get("stance") or "",
        },
    )

    try:
        data, meta = llm.chat_json(
            [{"role": "user", "content": prompt}],
            temperature=0.6,
            timeout=ANGLE_TIMEOUT,
        )
    except llm.LLMNotConfigured as e:
        return fail(f"未配置模型 Key，生成不了角度：{e}", retryable=False)
    except llm.LLMError as e:
        return fail(f"生成角度失败：{e}", retryable=True)

    # 模型偶尔会把整句包在引号或书名号里，剥掉；长度兜到 30 字以内。
    angle = str(data.get("angle") or "").strip().strip("\"'“”‘’「」《》")
    if angle:
        angle = re.sub(r"^(切入角度|角度)\s*[:：]\s*", "", angle).strip()
    angle = angle[:MATCH_ANGLE_MAX]

    return ok(
        {
            "topic_id": topic_id,
            "thesis_id": int(req.thesis_id),
            # 空串 = 模型认为这条热点和这个母题确实挂不上，前端保留模板角度就好
            "angle": angle,
            "model": meta.get("model") or "",
            "elapsed_ms": int(round((time.perf_counter() - t0) * 1000)),
        }
    )


# ================================================================
# 四形态派生
#
# 一张卡是唯一论点来源。这里不重新组织论点，只是把同一组 claim/supports 换四种说法。
# claim 由后端统一回填进每个形态的结果里，不依赖模型抄对——
# 模型要换论点会表现在正文里，那只能靠提示词压，但字段层面必须是一致的。
# ================================================================

DERIVE_FORMS = ("article", "script", "note", "cards")


def _derive_values(form: str, card: dict) -> dict:
    """每个形态仍然需要自己的主题词，用 card.claim 顶上，保证同源。"""
    claim = card["claim"]
    if form == "article":
        return {
            "keyword": claim,
            "reader": "普通读者",
            "tone": "平实克制",
            "length": "1200-1500字",
            "structure": "总分总，3-4个小节",
        }
    if form == "script":
        return {"topic": claim, "duration": 60, "hook_type": "反常识", "reader": "普通观众"}
    if form == "note":
        return {"topic": claim, "platform": "小红书", "style": "真诚分享、口语化"}
    return {"theme": claim, "count": 6, "style": "简约大字", "person": "无人物",
            "reader": "普通读者", "type": "观点输出"}


def _run_form(form: str, card: dict) -> tuple[dict, dict]:
    """跑一个形态。结构校验和单接口保持一致，失败抛 ValueError。"""
    prompt = render_prompt(form, _derive_values(form, card), card=card)

    if form == "article":
        data, meta = llm.chat_json([{"role": "user", "content": prompt}], temperature=0.7)
        if not isinstance(data, dict) or not data.get("title") or not isinstance(data.get("sections"), list):
            raise ValueError("模型返回结构不符合要求（缺 title 或 sections）")
        return {
            "title": str(data.get("title") or ""),
            "lead": str(data.get("lead") or ""),
            "sections": [
                {"h": str(s.get("h") or ""), "p": str(s.get("p") or "")}
                for s in data["sections"]
                if isinstance(s, dict)
            ],
            "end": str(data.get("end") or ""),
        }, meta

    if form == "script":
        data, meta = llm.chat_json([{"role": "user", "content": prompt}], temperature=0.8)
        if not isinstance(data, dict) or not data.get("hook") or not isinstance(data.get("segments"), list):
            raise ValueError("模型返回结构不符合要求（缺 hook 或 segments）")
        return {
            "hook": str(data.get("hook") or ""),
            "segments": [
                {"t": str(s.get("t") or ""), "line": str(s.get("line") or ""), "shot": str(s.get("shot") or "")}
                for s in data["segments"]
                if isinstance(s, dict)
            ],
            "cta": str(data.get("cta") or ""),
            "caption": str(data.get("caption") or ""),
            "hashtags": [str(t) for t in (data.get("hashtags") or [])],
        }, meta

    if form == "note":
        data, meta = llm.chat_json([{"role": "user", "content": prompt}], temperature=0.8)
        if not isinstance(data, dict) or not data.get("title") or not data.get("body"):
            raise ValueError("模型返回结构不符合要求（缺 title 或 body）")
        return {
            "title": str(data.get("title") or ""),
            "body": str(data.get("body") or ""),
            "tags": [str(t) for t in (data.get("tags") or [])],
            "image_hints": [str(t) for t in (data.get("image_hints") or [])],
        }, meta

    data, meta = llm.chat_json([{"role": "user", "content": prompt}], temperature=0.7)
    cards = _normalize_cards(data, 6)
    if not cards:
        raise ValueError("模型返回的卡片为空")
    return {"cards": cards}, meta


class DeriveReq(BaseModel):
    card_id: int
    forms: list[str] = []


# ================================================================
# 派生后置守卫：素材为空时，正文不许出现编造的第一人称经历
# ================================================================
# 提示词里写了「一手细节为空时禁止第一人称」，但那只是概率约束，
# 实测模型照样会写「我认识一个人」「我见过的一个同行」。
# 这里做确定性兜底：命中就把那句话整句删掉——宁可短，不许假。

FAKE_EXPERIENCE_WORDS = (
    "我见过", "我身边", "我一个朋友", "我以前",
    "我之前", "我认识", "我遇到", "我的经历",
)

_SPLIT_KEEP_RE = re.compile(r"[^。！？；\n]*[。！？；\n]?")


def _strip_fake_experience(text: str) -> tuple[str, int]:
    """删掉含第一人称编造的整句，返回 (清洗后文本, 删掉的句数)。"""
    if not isinstance(text, str) or not text:
        return text, 0
    if not any(w in text for w in FAKE_EXPERIENCE_WORDS):
        return text, 0

    kept: list[str] = []
    removed = 0
    for part in _SPLIT_KEEP_RE.findall(text):
        if not part:
            continue
        if any(w in part for w in FAKE_EXPERIENCE_WORDS):
            removed += 1
            continue
        kept.append(part)
    return "".join(kept).strip(), removed


def _sanitize_forms(node, counter: list[int]):
    """递归清洗整棵结果树里的所有字符串字段。"""
    if isinstance(node, str):
        out, n = _strip_fake_experience(node)
        counter[0] += n
        return out
    if isinstance(node, dict):
        return {k: _sanitize_forms(v, counter) for k, v in node.items()}
    if isinstance(node, list):
        return [_sanitize_forms(v, counter) for v in node]
    return node


@app.post("/api/gen/derive")
def gen_derive(req: DeriveReq):
    """以一张观点卡为唯一输入，一次产出多种形态。"""
    row = db.query_one("SELECT * FROM thesis_cards WHERE id = ?", (req.card_id,))
    if not row:
        return fail(f"观点卡 {req.card_id} 不存在")
    card = _card_out(row)

    forms = [f for f in (req.forms or list(DERIVE_FORMS)) if f in DERIVE_FORMS]
    if not forms:
        return fail("forms 至少要有一个：" + " / ".join(DERIVE_FORMS))

    results: dict = {}
    errors: dict = {}
    for form in forms:
        try:
            payload, meta = _run_form(form, card)
            payload["claim"] = card["claim"]
            payload["form"] = form
            payload["model"] = meta.get("model", "")

            # 素材为空时兜底：正文里冒出的第一人称经历，整句删掉再落库/返回。
            if not (card.get("own_detail") or "").strip():
                counter = [0]
                payload = _sanitize_forms(payload, counter)
                if counter[0]:
                    payload["stripped_sentences"] = counter[0]

            results[form] = payload
            _save_content(
                f"derive-{form}",
                payload.get("title") or card["claim"],
                payload,
                {"card_id": req.card_id, "form": form},
                meta,
            )
        except Exception as e:
            errors[form] = str(e)

    if not results:
        return fail("全部形态生成失败：" + "；".join(f"{k}:{v}" for k, v in errors.items()), retryable=True)

    out = {"card_id": req.card_id, "claim": card["claim"], "results": results}
    if errors:
        out["errors"] = errors
    try:
        db.points_change("生成内容 · derive", -POINT_COST["derive"])
    except Exception as e:
        print(f"[points] derive 扣分失败（不影响生成）：{type(e).__name__}: {e}")
    return ok(out)


# ================================================================
# 表达体检
# ================================================================

MAX_ISSUES = 12


class ExpressReq(BaseModel):
    text: str


@app.post("/api/check/express")
def check_express(req: ExpressReq):
    """规则先算分，再让模型找具体病句，合并返回。

    顺序是有意的：规则结果一定能引到原文，模型那条不一定。
    所以先把规则的问题占住位置，模型的问题往里补，凑不满 5 条也没关系。
    """
    text = (req.text or "").strip()
    if not text:
        return fail("text 不能为空")

    rule = rules.analyze_text(text)
    m = rule["metrics"]
    # 规则问题里前若干条是硬痕迹（破折号、无源权威、脚手架、口号、段尾升华等），
    # 每一条都能定位到原文，必须占住名额，不能被后面的短句风格问题挤掉。
    issues = list(rule["rule_issues"][:10])
    model_checked = False

    try:
        prompt = render_prompt(
            "express",
            {
                "text": text[:2000],
                "avg": m["avg_sentence_len"],
                "written_ratio": f"{m['written_word_ratio'] * 100:.0f}%",
                "concrete": m["concrete_hits"],
            },
        )
        data, _meta = llm.chat_json(
            [{"role": "user", "content": prompt}], temperature=0.4, timeout=60
        )
        model_checked = True
        for it in (data.get("issues") or []):
            if not isinstance(it, dict):
                continue
            quote = str(it.get("quote") or "").strip()
            problem = str(it.get("problem") or "").strip()
            fix = str(it.get("fix") or "").strip()
            if not quote or not fix:
                continue
            # 引文必须是原文里真实存在的一段，模型自己编的句子一律丢掉
            if quote not in text:
                continue
            issues.append({"quote": quote, "problem": problem, "fix": fix})
    except Exception:
        # 模型不可用也要把规则结果交出去，不能整个接口挂掉
        pass

    seen = set()
    merged = []
    for it in issues:
        key = (it.get("quote"), it.get("fix"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(it)

    return ok(
        {
            "metrics": m,
            "total": rule["total"],
            "issues": merged[:MAX_ISSUES],
            "model_checked": model_checked,
        }
    )


# ================================================================
# 口播节奏
# ================================================================


class PaceReq(BaseModel):
    text: str
    wpm: int = 270


@app.post("/api/script/pacing")
def script_pacing(req: PaceReq):
    """纯规则估时，不调模型。"""
    text = (req.text or "").strip()
    if not text:
        return fail("text 不能为空")
    return ok(rules.pace(text, req.wpm))


# ================================================================
# 四模块真实数据 API（market / books / points / replicate）
# 契约：成功 ok(data)；失败 fail(中文原因, retryable)，HTTP 400
# ================================================================


class PointsAdjustReq(BaseModel):
    reason: str
    delta: int


@app.get("/api/market")
def api_market(
    q: str = Query(default=""),
    cat: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
):
    """AI 超市：读 data/market.json 静态快照，不建表。"""
    path = DATA_DIR / "market.json"
    if not path.exists():
        return fail("技能快照未生成，请检查 data/market.json", retryable=True)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return fail(f"技能快照读取失败：{e}", retryable=True)
    kw = (q or "").strip().lower()
    rows = []
    for it in items:
        if cat and cat != "全部" and str(it.get("cat") or "") != cat:
            continue
        if kw:
            blob = (
                str(it.get("zh") or "") + str(it.get("en") or "")
                + str(it.get("desc") or "") + str(it.get("cat") or "")
            ).lower()
            if kw not in blob:
                continue
        rows.append(it)
    rows.sort(key=lambda x: float(x.get("starNum") or 0), reverse=True)
    total = len(rows)
    rows = rows[:limit]
    cats = _ordered_cats(MARKET_CATS, items)
    return ok({"items": rows, "total": total, "cats": cats})


@app.get("/api/books")
def api_books(cat: str = Query(default="")):
    """AI 书架：读 data/books.json 静态快照，只回元信息、不含正文。"""
    path = DATA_DIR / "books.json"
    if not path.exists():
        return fail("书架快照未生成，请检查 data/books.json", retryable=True)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return fail(f"书架快照读取失败：{e}", retryable=True)
    rows = []
    for it in items:
        if it.get("enabled") is False:
            continue
        if cat and cat != "全部" and str(it.get("cat") or "") != cat:
            continue
        rows.append({
            "id": it.get("id", ""),
            "name": it.get("name", ""),
            "cat": it.get("cat", ""),
            "color": it.get("color", ""),
            "ink": it.get("ink", ""),
            "desc": it.get("desc", ""),
            "tip": it.get("tip", ""),
            "url": it.get("url", ""),
            "author": it.get("author", ""),
            "source": it.get("source", ""),
            "enabled": it.get("enabled", True),
        })
    cats = _ordered_cats(BOOK_CATS, items)
    return ok({"items": rows, "cats": cats})


@app.get("/api/points")
def api_points():
    """积分：余额复用 settings、流水读 point_records、规则读 JSON。"""
    balance = db.points_balance()
    records = db.points_list(50)
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    week = db.query_one(
        "SELECT COALESCE(SUM(delta), 0) AS s FROM point_records WHERE ts >= ?",
        (cutoff,),
    )
    week_sum = int(week["s"]) if week else 0
    delta = f"本周 +{week_sum}" if week_sum >= 0 else f"本周 {week_sum}"
    try:
        rules_list = json.loads((DATA_DIR / "points_rules.json").read_text(encoding="utf-8"))
    except Exception:
        rules_list = []
    return ok({"balance": balance, "delta": delta, "records": records, "rules": rules_list})


@app.post("/api/points/adjust")
def api_points_adjust(req: PointsAdjustReq):
    """积分变动的唯一入口。"""
    reason = (req.reason or "").strip()
    if not reason:
        return fail("reason 不能为空", retryable=False)
    try:
        out = db.points_change(reason, int(req.delta))
    except Exception as e:
        return fail(f"积分变动失败：{e}", retryable=True)
    return ok(out)


@app.post("/api/replicate")
async def api_replicate(files: list[UploadFile] = File(...)):
    """爆款贴图复刻：只产结构报告，不调 LLM、不写库、不留存。"""
    if not files or len(files) < 1 or len(files) > REPLICATE_MAX_FILES:
        return fail(f"请上传 1–{REPLICATE_MAX_FILES} 张图片", retryable=False)
    with tempfile.TemporaryDirectory(prefix="creator-os-rep-") as td:
        sizes = []
        for i, f in enumerate(files):
            raw = await f.read()
            if len(raw) > REPLICATE_MAX_BYTES:
                return fail(f"单张图片超过 10MB：{f.filename or '未命名'}", retryable=False)
            # 临时文件随 TemporaryDirectory 退出即删，sqlite 里不留任何表
            with open(os.path.join(td, f"upload_{i}"), "wb") as w:
                w.write(raw)
            sizes.append(len(raw))
        size_text = "、".join(f"{n} 字节" for n in sizes)
        data = {
            "structure": REPLICATE_TEMPLATE["structure"],
            "layers": list(REPLICATE_TEMPLATE["layers"]),
            "hooks": list(REPLICATE_TEMPLATE["hooks"]),
            "colors": [dict(c) for c in REPLICATE_TEMPLATE["colors"]],
            "directions": [dict(d) for d in REPLICATE_TEMPLATE["directions"]],
            "file_count": len(sizes),
            "note": (
                "结构报告为本地模板生成，未调用视觉模型；本次收到 "
                f"{len(sizes)} 个文件，各文件字节数：{size_text}。"
            ),
        }
    return ok(data)


# ================================================================
# 账号诊断（R-D01 ~ R-D04）
#
# 数据只来自用户手动导入的那一批，不与任何外部基准比较（我们没有别人的数据）。
# 算不出来的指标一律 None，前端显示「—」，不填 0、不填示例值。
# 互动率一律走 calc_rate()，这里不许再写一份 (like+share+comment)/read。
# ================================================================

# 表头同义写法：标准名 -> 用户后台可能导出的其他叫法
DIAG_COLUMNS = {
    "标题": ("标题", "title", "内容", "作品"),
    "平台": ("平台", "platform", "渠道"),
    "阅读": ("阅读", "阅读量", "播放", "曝光", "views"),
    "发布时间": ("发布时间", "时间", "发布日期", "date"),
    "点赞": ("点赞", "赞", "likes"),
    "转发": ("转发", "分享", "分享数", "shares"),
    "评论": ("评论", "评论数", "comments"),
    "粉丝数": ("粉丝数", "粉丝", "fans"),
}
DIAG_REQUIRED = ("标题", "平台", "阅读")
DIAG_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
DIAG_SUGGEST_MAX = 150
DIAG_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日",
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M",
)


def _diag_header_key(raw) -> str:
    """表头归一：去 BOM、去所有空白、转小写，容忍「阅读 量」「Views」。"""
    return re.sub(r"\s+", "", str(raw or "")).replace("\ufeff", "").lower()


def _diag_column_index(headers) -> dict:
    """把表头行映射成 {标准列名: 列下标}，同名列只认第一列。"""
    alias = {}
    for canon, names in DIAG_COLUMNS.items():
        for n in names:
            alias[_diag_header_key(n)] = canon
    idx = {}
    for i, h in enumerate(headers):
        canon = alias.get(_diag_header_key(h))
        if canon and canon not in idx:
            idx[canon] = i
    return idx


def _diag_int(raw):
    """把「18,420」「 1200 」「1.0」这类单元格转成 int，转不动返回 None。"""
    s = str(raw if raw is not None else "").strip()
    for ch in (",", "，", " ", "\u00a0"):
        s = s.replace(ch, "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _diag_date(raw):
    """解析发布时间，返回 date；解析不了返回 None（该行不进周内走势）。"""
    s = str(raw or "").strip().replace("T", " ")
    if not s:
        return None
    for fmt in DIAG_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _diag_decode(raw: bytes) -> str:
    """CSV 解码：Excel 导出的中文 CSV 默认 GBK，先按 UTF-8 试，再按 GB18030（GBK 超集）。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _diag_parse(text: str):
    """解析 CSV / 粘贴的表格。

    返回 (error, rows, skipped, columns)：
      error    非空表示整批不可用（缺必需列 / 没内容），rows 为 None
      rows     保留下来的行，每行带 title/platform/read/like/share/comment/fans/date
      skipped  被丢掉的行数（缺标题或缺阅读）
      columns  {标准列名: 是否存在}，前端用它告诉用户读到了哪几列
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return "没有收到数据，请粘贴表格内容或上传 CSV", None, 0, {}

    # 从 Excel 直接复制是 Tab 分隔，导出文件是逗号分隔，按表头里哪种多就用哪种
    head = text.split("\n", 1)[0]
    delim = "\t" if head.count("\t") > head.count(",") else ","
    try:
        grid = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(str(c).strip() for c in r)]
    except csv.Error as e:
        return f"CSV 解析失败：{e}", None, 0, {}
    if not grid:
        return "没有收到数据，请粘贴表格内容或上传 CSV", None, 0, {}

    headers = grid[0]
    idx = _diag_column_index(headers)
    columns = {c: (c in idx) for c in DIAG_COLUMNS}
    missing = [c for c in DIAG_REQUIRED if c not in idx]
    if missing:
        got = "、".join(str(h).strip() for h in headers if str(h).strip()) or "（空表头）"
        return f"缺少必需列：{'、'.join(missing)}；读到的表头是：{got}", None, 0, columns

    rows = []
    skipped = 0
    for raw in grid[1:]:
        def cell(name, _raw=raw):
            i = idx.get(name)
            if i is None or i >= len(_raw):
                return ""
            return str(_raw[i]).strip()

        title = cell("标题")
        read = _diag_int(cell("阅读"))
        if not title or read is None:
            skipped += 1
            continue
        d = _diag_date(cell("发布时间"))
        rows.append({
            "title": title,
            "platform": cell("平台"),
            "read": read,
            "like": _diag_int(cell("点赞")) or 0,
            "share": _diag_int(cell("转发")) or 0,
            "comment": _diag_int(cell("评论")) or 0,
            "fans": _diag_int(cell("粉丝数")),
            "date": d.isoformat() if d else None,
        })
    return "", rows, skipped, columns


def _diag_topic_score(title: str) -> int:
    """选题力：标题里有没有「具体的谁」和「具体的事」。纯规则，不调模型。

    基准 50 = 中性标题。加分的加、减分的减，最后夹到 0-100。
    基准必须是 50 而不是 0：一个没数字没疑问词、但长度正常的标题是「普通」，
    不是「极差」。从 0 起算会把所有正常标题压到 10-20 分，整个质量分系统性偏低。
    """
    t = str(title or "")
    score = 50
    if re.search(r"[0-9０-９]", t):
        score += 25
    if any(w in t for w in ("为什么", "怎么", "如何", "吗", "呢", "哪", "谁")):
        score += 20
    if any(w in t for w in ("我", "你", "普通人", "学生", "宝妈", "程序员", "上班族", "退休", "50岁")):
        score += 25
    n = len(t)
    if 8 <= n <= 28:
        score += 20
    elif 8 <= n <= 40:
        score += 10
    if any(w in t for w in ("格局", "认知", "思维", "底层逻辑", "干货", "必看")):
        score -= 15
    return max(0, min(100, score))


def _diag_quality(rows: list[dict]):
    """内容质量四项子分。返回 (总分|None, 子分 dict, 阅读变异系数|None)。

    某项算不出来（样本不足 / 全 0 阅读）就是 None，权重按剩下的项重新归一，
    不拿 0 分顶上去——0 分会把「不知道」说成「很差」。
    """
    subs = {}
    reads = [r["read"] for r in rows]
    titles = [r["title"] for r in rows]

    subs["选题力"] = round(sum(_diag_topic_score(t) for t in titles) / len(titles)) if titles else None

    if len(reads) >= 3 and max(reads) > 0:
        subs["标题力"] = round((max(reads) - min(reads)) / max(reads) * 100)
    else:
        subs["标题力"] = None

    mean = sum(reads) / len(reads) if reads else 0.0
    cv = None
    if mean > 0:
        subs["信息密度"] = round(min(100.0, statistics.median(reads) / mean * 100))
        cv = statistics.pstdev(reads) / mean
        subs["稳定性"] = round(max(0.0, 100 - cv * 100))
    else:
        subs["信息密度"] = None
        subs["稳定性"] = None

    weights = {"选题力": 0.3, "标题力": 0.3, "信息密度": 0.25, "稳定性": 0.15}
    total_w = sum(w for k, w in weights.items() if subs.get(k) is not None)
    if total_w <= 0:
        return None, subs, cv
    score = sum(subs[k] * w for k, w in weights.items() if subs.get(k) is not None) / total_w
    return int(round(score)), subs, cv


def _diag_bars(rows: list[dict]) -> list[dict]:
    """一周走势：按发布时间的星期几聚合，取该星期几的阅读中位数（不用均值）。"""
    buckets = {}
    for r in rows:
        if not r.get("date"):
            continue
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        buckets.setdefault(d.weekday(), []).append(r["read"])
    if not buckets:
        return []

    med = {wd: statistics.median(v) for wd, v in buckets.items()}
    top = max(med.values())
    bars = []
    for wd in range(7):
        if wd not in med:
            bars.append({"l": DIAG_WEEKDAYS[wd], "v": 0, "empty": True})
        else:
            v = int(round(med[wd] / top * 96)) if top > 0 else 0
            bars.append({"l": DIAG_WEEKDAYS[wd], "v": v})
    if top > 0:
        hot = max(range(7), key=lambda i: bars[i]["v"])
        bars[hot]["hot"] = True
    return bars


def _diag_anomalies(rows: list[dict]):
    """异常点：|rate - 中位数| > 3 × 1.4826 × MAD 的行。MAD 为 0 时一律不标。"""
    pairs = []
    for r in rows:
        rate = calc_rate(r["read"], r["like"], r["share"], r["comment"])
        if rate is not None:
            pairs.append((r["title"], rate))
    if len(pairs) < 3:
        return [], None
    rates = [p[1] for p in pairs]
    med = statistics.median(rates)
    mad = statistics.median([abs(x - med) for x in rates])
    if mad <= 0:
        return [], med
    limit = 3 * 1.4826 * mad
    hits = [
        {"title": t, "rate": round(x, 6), "median": round(med, 6)}
        for t, x in pairs
        if abs(x - med) > limit
    ]
    hits.sort(key=lambda h: abs(h["rate"] - h["median"]), reverse=True)
    return hits, med


def _diag_suggestions(n, weeks, freq, rate, quality, subs, cv, anomalies) -> list[str]:
    """规则建议器：纯 Python，必定有输出。最多 3 条，按影响面 × 可执行性排序。"""
    cands = []  # (优先级, 文案)；异常点 > 互动率 > 更新频率 > 质量分 > 稳定性

    if anomalies:
        a = anomalies[0]
        cands.append((1, (
            f"《{a['title']}》的互动率 {a['rate'] * 100:.1f}%，是你这批数据里明显偏离的一条"
            f"（中位数 {a['median'] * 100:.1f}%）。"
        )))

    if rate is not None:
        p = rate * 100
        if p < 1:
            cands.append((2, (
                f"互动率 {p:.1f}%，低于 1%。检查结尾有没有具体提问——"
                "不是「你怎么看」，而是「你上次遇到这种情况是怎么处理的」。"
            )))
        elif p >= 5:
            cands.append((2, (
                f"互动率 {p:.1f}%，这个数字说明你的读者愿意动手。"
                "把这批里互动率最高的那篇的结尾句式抠出来，当成模板固定。"
            )))

    if freq < 2:
        cands.append((3, (
            f"近 {weeks} 周只发了 {n} 篇，平均 {freq:.1f} 篇/周。"
            "平台对更新频率的权重通常高于单篇质量，先做到每周 2 篇再谈优化。"
        )))
    elif freq > 7:
        cands.append((3, (
            f"{freq:.1f} 篇/周属于高频。这个节奏下质量通常会被稀释——"
            "把这批里互动率最低的三篇找出来，看它们是不是赶工出来的。"
        )))

    if quality is not None:
        if quality < 60:
            scored = [(k, v) for k, v in subs.items() if v is not None]
            worst = min(scored, key=lambda kv: kv[1])[0] if scored else "选题力"
            cands.append((4, (
                f"内容质量 {quality} 分，主要失分在{worst}。"
                "这一项是可以靠模板化的方式改的，优先动它。"
            )))
        elif quality >= 85:
            cands.append((4, (
                f"内容质量 {quality} 分。这个水平下继续优化文字的收益已经很小，"
                "把精力挪到分发——同样的内容换一个平台、换一个标题重发一次。"
            )))

    if cv is not None and cv > 1.5:
        cands.append((5, (
            f"阅读量忽高忽低（变异系数 {cv:.2f}）。这说明你的选题还没形成可复用的模式，"
            "靠碰运气。先把跑赢的三篇放在一起找共同点。"
        )))

    cands.sort(key=lambda x: x[0])
    out = [t if len(t) <= DIAG_SUGGEST_MAX else t[: DIAG_SUGGEST_MAX - 1] + "…" for _, t in cands[:3]]
    if not out:
        out = [f"这批 {n} 条数据没有明显异常。想看出问题，需要至少 10 条或跨两周的数据。"]
    return out


def _diag_compute(rows: list[dict]) -> dict:
    """四项指标 + 一周走势 + 异常点 + 规则建议。只读导入的那批数据。"""
    n = len(rows)

    # 互动率：加权平均 = 总互动 / 总阅读，口径复用 calc_rate()
    ok_rows = [r for r in rows if r["read"] > 0]
    rate = calc_rate(
        sum(r["read"] for r in ok_rows),
        sum(r["like"] for r in ok_rows),
        sum(r["share"] for r in ok_rows),
        sum(r["comment"] for r in ok_rows),
    )

    # 覆盖周数：按发布时间跨度算，不足一周按一周
    dates = sorted(r["date"] for r in rows if r.get("date"))
    weeks = 1
    if len(dates) >= 2:
        try:
            span = (datetime.strptime(dates[-1], "%Y-%m-%d").date()
                    - datetime.strptime(dates[0], "%Y-%m-%d").date()).days
            weeks = max(1, (span + 6) // 7)
        except ValueError:
            weeks = 1
    freq = n / weeks

    quality, subs, cv = _diag_quality(rows)

    # 粉丝增长：数据里真有粉丝数才算，末值/首值 - 1
    fans = [(r["date"] or "", r["fans"]) for r in rows if r.get("fans") is not None]
    growth = None
    if len(fans) >= 2:
        if all(r.get("date") for r in rows if r.get("fans") is not None):
            fans.sort(key=lambda x: x[0])
        first, last = fans[0][1], fans[-1][1]
        if first and first > 0:
            growth = (last / first - 1) * 100

    metrics = [
        {
            "label": "内容质量",
            "value": "—" if quality is None else str(quality),
            "unit": "分",
            "pct": 0 if quality is None else int(quality),
        },
        {
            "label": "粉丝增长",
            "value": "—" if growth is None else f"{growth:.1f}",
            "unit": "%",
            "pct": 0 if growth is None else int(round(max(0.0, min(100.0, growth * 10)))),
        },
        {
            # 没有一行有阅读时算不出互动率：给 None，不给 0
            "label": "互动率",
            "value": None if rate is None else f"{rate * 100:.1f}",
            "unit": "%",
            "pct": 0 if rate is None else int(round(min(100.0, rate * 100 * 20))),
        },
        {
            "label": "更新频率",
            "value": f"{freq:.1f}",
            "unit": "篇/周",
            "pct": int(round(min(100.0, freq * 15))),
        },
    ]

    anomalies, _ = _diag_anomalies(rows)
    return {
        "metrics": metrics,
        "bars": _diag_bars(rows),
        "suggestions": _diag_suggestions(n, weeks, freq, rate, quality, subs, cv, anomalies),
        "anomalies": anomalies,
        "score": quality,
        "weeks": weeks,
    }


def _diag_note(metrics, suggestions, n, weeks, anomalies):
    """可选增强：配了 key 才调模型，失败一律返回 None，不影响整条链路。"""
    if not config.ZHIPU_API_KEY:
        return None
    mtext = "；".join(
        f"{m['label']} {m['value'] if m['value'] is not None else '—'}{m['unit']}" for m in metrics
    )
    stext = "；".join(f"{i + 1}. {s}" for i, s in enumerate(suggestions))
    atext = f"{len(anomalies)} 条异常点" if anomalies else "无异常点"
    try:
        prompt = render_prompt("diagnose", {
            "metrics": mtext,
            "suggestions": stext,
            "row_count": n,
            "weeks": weeks,
            "anomalies": atext,
        })
        data, _ = llm.chat_json([{"role": "user", "content": prompt}], temperature=0.3, timeout=30)
    except Exception as e:
        print(f"[diagnose] 一句话总评生成失败，按无评语返回：{type(e).__name__}: {e}")
        return None
    note = str(data.get("note") or "").strip()
    if not note:
        return None
    # 红线：模型不许拿别人的数据做比较，出现这类说法整条评语丢掉
    for bad in ("同类账号", "行业平均", "行业均值", "大盘", "基准值"):
        if bad in note:
            return None
    return note


class DiagnoseRunReq(BaseModel):
    run_id: int


@app.post("/api/diagnose/import")
async def api_diagnose_import(request: Request):
    """R-D01：粘贴（JSON）或上传（multipart）CSV，落一行 diagnose_runs 并返回 run_id。"""
    ctype = (request.headers.get("content-type") or "").lower()
    filename = ""
    if "multipart/form-data" in ctype:
        form = await request.form()
        platform = str(form.get("platform") or "").strip() or "全部"
        upload = form.get("file")
        if upload is None:
            return fail("没有收到文件，请重新选择 CSV", retryable=False)
        filename = getattr(upload, "filename", "") or ""
        text = _diag_decode(await upload.read())
    else:
        try:
            body = await request.json()
        except Exception:
            return fail("请求体不是合法 JSON", retryable=False)
        platform = str(body.get("platform") or "").strip() or "全部"
        filename = str(body.get("filename") or "")
        text = str(body.get("csv") or "")

    if filename.lower().endswith((".xlsx", ".xls")):
        return fail("Excel 文件暂不支持，请另存为 CSV 后再上传", retryable=False)

    error, rows, skipped, columns = _diag_parse(text)
    if error:
        return fail(error, retryable=False)
    if not rows:
        return fail("没有可用数据行：每行都要有标题和阅读数", retryable=False)

    payload = json.dumps({"rows": rows, "columns": columns, "skipped": skipped}, ensure_ascii=False)
    run_id = db.execute(
        "INSERT INTO diagnose_runs(ts, platform, row_count, score, payload, note) VALUES(?,?,?,?,?,?)",
        (db.now_str(), platform, len(rows), None, payload, None),
    )
    return ok({
        "run_id": run_id,
        "row_count": len(rows),
        "skipped": skipped,
        "columns": columns,
        "platform": platform,
    })


@app.post("/api/diagnose/run")
def api_diagnose_run(req: DiagnoseRunReq):
    """跑一次诊断：算指标 → 规则建议 → 可选模型总评 → 回写这一行。"""
    row = db.query_one("SELECT * FROM diagnose_runs WHERE id = ?", (int(req.run_id),))
    if not row:
        return fail(f"没有这次导入记录：run_id={req.run_id}", retryable=False, status_code=404)
    try:
        payload = json.loads(row["payload"])
    except Exception:
        return fail("这次导入的数据已损坏，请重新导入", retryable=False)
    rows = payload.get("rows") or []
    if not rows:
        return fail("这次导入没有可用数据行，请重新导入", retryable=False)

    result = _diag_compute(rows)
    note = _diag_note(result["metrics"], result["suggestions"], len(rows), result["weeks"], result["anomalies"])
    payload.update(result)
    payload["note"] = note
    db.execute(
        "UPDATE diagnose_runs SET score = ?, payload = ?, note = ? WHERE id = ?",
        (result["score"], json.dumps(payload, ensure_ascii=False), note, int(req.run_id)),
    )
    return ok({
        "metrics": result["metrics"],
        "bars": result["bars"],
        "suggestions": result["suggestions"],
        "note": note,
        "anomalies": result["anomalies"],
        "score": result["score"],
    })


@app.get("/api/diagnose/runs")
def api_diagnose_runs(limit: int = Query(10, ge=1, le=50)):
    """历史记录列表（不含 payload，避免一次拉回全部原始行）。"""
    rows = db.query(
        "SELECT id, ts, platform, row_count, score FROM diagnose_runs ORDER BY id DESC LIMIT ?",
        (int(limit),),
    )
    return ok({"count": len(rows), "items": rows})


@app.get("/api/diagnose/runs/{run_id}")
def api_diagnose_run_detail(run_id: int):
    """回看某一次的完整结果：直接回落库的 payload，含原始行，能追到每一个数字。"""
    row = db.query_one("SELECT * FROM diagnose_runs WHERE id = ?", (int(run_id),))
    if not row:
        return fail(f"没有这次诊断记录：id={run_id}", retryable=False, status_code=404)
    try:
        payload = json.loads(row["payload"])
    except Exception:
        payload = {}
    payload.update({
        "id": row["id"],
        "ts": row["ts"],
        "platform": row["platform"],
        "row_count": row["row_count"],
        "score": row["score"],
    })
    return ok(payload)
