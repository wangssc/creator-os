"""SQLite 连接、建表与迁移。

不使用 ORM，全部标准库 sqlite3。
每次操作新建连接（短连接），避免 FastAPI 多线程 + 定时任务并发下的跨线程报错。
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from config import DB_PATH

# 建表语句：全部 IF NOT EXISTS，反复执行不报错
SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL,
  title TEXT NOT NULL,
  heat INTEGER DEFAULT 0,
  url TEXT,
  fetched_at TEXT NOT NULL,
  source TEXT,
  UNIQUE(platform, title, fetched_at)
);
CREATE INDEX IF NOT EXISTS idx_topics_time ON topics(fetched_at DESC);

CREATE TABLE IF NOT EXISTS contents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  topic_id INTEGER,
  title TEXT,
  body TEXT,
  meta TEXT,
  model TEXT,
  prompt_ver TEXT,
  cost REAL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content_id INTEGER,
  platform TEXT NOT NULL,
  url TEXT,
  published_at TEXT,
  read_n INTEGER DEFAULT 0,
  like_n INTEGER DEFAULT 0,
  share_n INTEGER DEFAULT 0,
  comment_n INTEGER DEFAULT 0,
  rate REAL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period_start TEXT, period_end TEXT, report TEXT, model TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, kind TEXT, spec TEXT
);

-- 母题库：用户自己的立场与一手素材，是资产不是缓存。
-- evidence 存 JSON 数组字符串，每条一句话亲历或见闻；空数组表示这条母题还没接到地上。
CREATE TABLE IF NOT EXISTS theses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  stance TEXT NOT NULL,
  against TEXT,
  evidence TEXT,
  keywords TEXT,
  weight REAL DEFAULT 1.0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_theses_weight ON theses(weight DESC);

-- 观点卡：一张母题 → 一张卡 → 派生四种形态。
-- 注意命名：贴图卡片占的是 templates 表 + /api/gen/cards，这里一律用单数 card。
-- own_detail 必须逐字来自 theses.evidence，空就是空，不许模型补。
CREATE TABLE IF NOT EXISTS thesis_cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thesis_id INTEGER NOT NULL,
  topic_id INTEGER,
  claim TEXT NOT NULL,
  supports TEXT,
  counter TEXT,
  own_detail TEXT,
  missing TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_thesis ON thesis_cards(thesis_id);

CREATE TABLE IF NOT EXISTS point_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  reason TEXT NOT NULL,
  delta INTEGER NOT NULL,
  balance_after INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_point_records_ts ON point_records(ts DESC);

-- 账号诊断：一次导入 = 一行。payload 存这次导入的原始行 + 算完的结果，
-- 所以回看时每个数字都能追回具体哪一行；score 在算不出质量分时留 NULL。
CREATE TABLE IF NOT EXISTS diagnose_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  platform TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  score INTEGER,
  payload TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_diagnose_runs_ts ON diagnose_runs(ts DESC);
"""


def now_str() -> str:
    """返回本地时间字符串，格式 YYYY-MM-DD HH:MM:SS，全库统一口径。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def conn():
    """短连接上下文：自动提交、自动关闭，异常时回滚。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db() -> None:
    """建表与索引。可反复调用。

    迁移：posts 表在原始设计里没有 title 列，录入表单需要直接记标题，
    这里做一次幂等的加列检查，老库也能平滑升上来。

    topics.source 同理由：采集从内置四源换成 DailyHotApi 旁路服务后，
    需要能从库里分辨这批数据是哪条路径采来的（hotnode / local），
    老库没有这列，幂等补上；老数据留 NULL，表示「换源之前采的」。
    """
    with conn() as c:
        c.executescript(SCHEMA)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(posts)").fetchall()]
        if "title" not in cols:
            c.execute("ALTER TABLE posts ADD COLUMN title TEXT")
        tcols = [r["name"] for r in c.execute("PRAGMA table_info(topics)").fetchall()]
        if "source" not in tcols:
            c.execute("ALTER TABLE topics ADD COLUMN source TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_topics_source ON topics(source)")
    # 种子的插入放在连接关闭之后：两个连接同时开着时，另一个连接可能还看不到刚建的表
    seed_theses()


# 8 条种子母题，围绕三个方向：职业选择 / AI 与信息辨别 / 中年重启。
# 每条 stance 都是一句可以被反驳的判断，不是「关于 XX 的思考」这种中性话题。
# evidence 一律留空数组——这是设计意图，不是没写完：母题库的价值来自用户自己的一手素材，
# 预置亲历等于替用户编经历，正好违背整个产品的立意。
SEED_THESES = [
    (
        "省钱是穷人的陷阱",
        "越缺钱越该在小钱上大方，省掉的是机会不是开销。",
        "买贵的是浪费，能省一点是一点。",
        "省钱,消费,理财,月薪,开支",
    ),
    (
        "稳定是最贵的赌注",
        "把稳定当目标的人，最后都在替别人的稳定兜底。",
        "先找个稳定工作，再谈理想。",
        "稳定,考公,编制,裸辞,职业选择",
    ),
    (
        "热爱不能当饭吃",
        "先练出能换钱的手艺，再谈热爱，顺序反了就是逃避。",
        "跟着热爱走，钱自然会来。",
        "热爱,转行,兴趣,职业规划,天赋",
    ),
    (
        "AI不会替你思考",
        "把判断交给模型的人，会先失去判断力，再失去岗位。",
        "AI 都这么强了，人只要会提问就行。",
        "AI,人工智能,大模型,ChatGPT,提示词",
    ),
    (
        "信息越多越蠢",
        "刷得越多不等于知道越多，多数信息只是在加固你的偏见。",
        "多看点总没坏处，信息就是优势。",
        "信息,算法,推荐,信息茧房,刷手机",
    ),
    (
        "案例不能当证据",
        "一个成功案例什么都证明不了，它只说明有人侥幸没被淘汰。",
        "人家都做成了，说明这条路能走。",
        "案例,成功学,复盘,幸存者偏差,风口",
    ),
    (
        "四十岁不是下坡",
        "中年重启靠的不是补短板，是把二十年经验压成一个能卖的东西。",
        "过了三十五就别折腾了，安分点。",
        "中年,35岁,转型,重新开始,再就业",
    ),
    (
        "经验要能卖钱",
        "说不清自己解决过哪个具体问题的人，工作经验等于零。",
        "干得久了自然有经验，简历上写年限就够了。",
        "经验,简历,面试,职业,能力",
    ),
]


def seed_theses() -> int:
    """母题表为空时插入 8 条种子，返回插入条数。表非空则什么都不做。"""
    with conn() as c:
        n = c.execute("SELECT COUNT(*) FROM theses").fetchone()[0]
        if n:
            return 0
        ts = now_str()
        rows = [(t, s, a, "[]", k, 1.0, ts) for (t, s, a, k) in SEED_THESES]
        c.executemany(
            "INSERT INTO theses(title, stance, against, evidence, keywords, weight, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        return len(rows)


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    """查询多行，返回 dict 列表。"""
    with conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    """查询单行，无结果返回 None。"""
    with conn() as c:
        row = c.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """执行写操作，返回 lastrowid。"""
    with conn() as c:
        cur = c.execute(sql, tuple(params))
        return cur.lastrowid


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> int:
    """批量写入，返回受影响行数。"""
    with conn() as c:
        cur = c.executemany(sql, [tuple(x) for x in seq])
        return cur.rowcount


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """读单个设置项，不存在返回 default。"""
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    """写单个设置项，存在则覆盖。"""
    execute(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def touch_streak() -> int:
    """连续使用天数：每天首次访问 +1，断一天归 1。

    状态存在 settings 表：last_active_date（YYYY-MM-DD）、streak_count。
    今天已经访问过则原样返回，不重复累加。
    """
    now = datetime.now()
    today = now.date()
    last = get_setting("last_active_date")
    streak = int(get_setting("streak_count") or 0)

    if last == today.isoformat():
        return streak

    yesterday = (today - timedelta(days=1)).isoformat()
    streak = streak + 1 if last == yesterday else 1

    set_setting("last_active_date", today.isoformat())
    set_setting("streak_count", str(streak))
    return streak


def points_balance() -> int:
    """读积分余额；settings 里没有 points_balance 时初始化为 860 并返回。"""
    raw = get_setting("points_balance")
    if raw is None:
        set_setting("points_balance", "860")
        return 860
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 860


def points_change(reason: str, delta: int) -> dict:
    """积分变动唯一入口：单连接内读余额→算新值→写流水→UPSERT 余额。

    不在 conn() 内调用 set_setting()/query_one()：它们各自另开连接会嵌套并可能锁库，
    这里直接用同一个连接 c.execute() 参数化 SQL。
    """
    with conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", ("points_balance",)).fetchone()
        balance = int(row["value"]) if row else 860
        after = balance + int(delta)
        ts = now_str()
        c.execute(
            "INSERT INTO point_records(ts, reason, delta, balance_after) VALUES(?,?,?,?)",
            (ts, reason, int(delta), after),
        )
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("points_balance", str(after)),
        )
        return {"ts": ts, "reason": reason, "delta": int(delta), "balance_after": after}


def points_list(limit: int = 50) -> list[dict]:
    """积分流水，按 id 倒序，最多 limit 条。"""
    return query(
        "SELECT ts, reason, delta, balance_after FROM point_records ORDER BY id DESC LIMIT ?",
        (limit,),
    )
