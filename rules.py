"""文本规则分析：纯函数，不调模型、不读数据库、不依赖任何全局状态。

为什么单拎成一个文件：表达体检（/api/check/express）和口播节奏（/api/script/pacing）
都要切句、都要数书面词。两处各写一套迟早分叉，最后页面上显示的分数和停顿对不上，
用户会以为系统坏了。

约定：
- 「字」一律指去掉空白与标点之后的字符数。中文口播估时长按正文字数算，
  标点不发音，算进去会把 60 秒的稿子估成 68 秒。
"""

import re

# 书面词表：写报告用的词，不是说话用的词。正好 40 个。
WRITTEN_WORDS = [
    "因此", "然而", "综上所述", "总而言之", "基于", "鉴于", "倘若", "从而",
    "进一步", "旨在", "赋能", "抓手", "闭环", "沉淀", "对齐", "颗粒度",
    "维度", "层级", "链路", "复盘", "迭代", "生态", "助力", "聚焦",
    "深耕", "打通", "复用", "抽象", "认知", "范式", "路径", "机制",
    "体系", "逻辑", "本质", "底层", "构成", "奠定", "彰显", "凸显",
]

# 书面词 → 口语说法。有词表的换掉，没有的告诉用户直接删。
WRITTEN_FIX = {
    "因此": "所以",
    "然而": "但是",
    "综上所述": "说白了",
    "总而言之": "说到底",
    "基于": "按",
    "鉴于": "看",
    "倘若": "要是",
    "从而": "这样",
    "进一步": "再",
    "旨在": "就是为了",
    "赋能": "帮上忙",
    "抓手": "着力点",
    "闭环": "有头有尾",
    "沉淀": "攒下来",
    "对齐": "对一下",
    "颗粒度": "细到什么程度",
    "维度": "方面",
    "层级": "层次",
    "链路": "流程",
    "复盘": "回头看",
    "迭代": "改一版",
    "生态": "一圈人",
    "助力": "帮",
    "聚焦": "盯住",
    "深耕": "做久",
    "打通": "连起来",
    "复用": "再用一次",
    "抽象": "虚",
    "认知": "看法",
    "范式": "套路",
    "路径": "路子",
    "机制": "规矩",
    "体系": "一整套",
    "逻辑": "道理",
    "本质": "根子上",
    "底层": "最下面那层",
    "构成": "组成",
    "奠定": "打下",
    "彰显": "显出",
    "凸显": "冒出",
}

# 立场标记：全文一处都没有，说明这篇没有「你」在里面。
STANCE_MARKERS = [
    "我认为", "我见过", "我的判断", "我反对", "我觉得", "我看到",
    "我遇到", "我的看法", "我不同意", "我判断",
]

# 具体名词与场景词。数的是「能不能看见画面」，不是文采。
CONCRETE_WORDS = [
    "钱", "块", "房租", "工资", "账单", "押金", "饭", "外卖", "地铁", "公交",
    "手机", "电脑", "屏幕", "键盘", "耳机", "车", "房", "桌子", "椅子", "床",
    "公司", "工位", "会议室", "电梯", "客户", "老板", "同事", "面试", "简历",
    "合同", "邮件", "报表", "深夜", "早上", "中午", "晚上", "周末", "小时",
    "分钟", "天", "年", "月", "岁", "杯", "碗", "袋", "张", "条", "趟",
]

SENT_END = "。！？；"
SENT_SPLIT_RE = re.compile(r"[。！？；…\n]+")
COMMA_RE = re.compile(r"[，,、；;：:]")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")
CJK_STRIP_RE = re.compile(r"[\s，。！？；：、（）()\[\]【】「」“”\"'’‘·—…,.!?;:%\-/]")

LONG_SENTENCE = 25  # 超过这个长度算长句


def count_chars(text: str) -> int:
    """去掉空白与标点后的字数。口播估时用这个，不是 len(text)。"""
    return len(CJK_STRIP_RE.sub("", text or ""))


def split_sentences(text: str) -> list[str]:
    """按 。！？；…和换行切句，去掉空段，保留原句里的标点。"""
    parts = SENT_SPLIT_RE.split(text or "")
    return [p.strip() for p in parts if p and p.strip()]


def sentence_len(sent: str) -> int:
    """单句字数，同样去掉标点。"""
    return count_chars(sent)


def _count_words(text: str, words: list[str]) -> int:
    """统计一组词在文中出现的总次数。"""
    n = 0
    for w in words:
        n += text.count(w)
    return n


def _concrete_hits(text: str) -> int:
    """具体度：数字 + 具体名词/场景词的出现次数。"""
    return len(NUM_RE.findall(text)) + _count_words(text, CONCRETE_WORDS)


def _long_sentence_fix(sent: str) -> str:
    """给长句一个可以直接替换的改法：在最靠近中间的逗号处断成两句。"""
    marks = [m.start() for m in COMMA_RE.finditer(sent)]
    if not marks:
        return "这句没有停顿，先补一个具体动作，再在动作后面断句"
    mid = min(marks, key=lambda i: abs(i - len(sent) / 2))
    return sent[:mid] + "。" + sent[mid + 1 :].lstrip()


def _written_issue(sent: str, word: str) -> dict:
    """给书面词一条问题，quote 用整句，方便用户定位。"""
    quote = sent if len(sent) <= 30 else sent[:30]
    fix_word = WRITTEN_FIX.get(word)
    if fix_word:
        fix = sent.replace(word, fix_word, 1)
    else:
        fix = sent.replace(word, "", 1).replace("，，", "，").strip("，")
    return {
        "quote": quote,
        "problem": f"「{word}」是写报告的词，念出来会像在作汇报",
        "fix": fix if fix != sent else f"删掉「{word}」，直接说后半句",
    }


# ================================================================
# 硬痕迹：机器写作留下的可枚举指纹
# ================================================================
# 这一组全部是确定性正则/词表，不调模型、不猜语义。
# 之所以要单独拎出来：句长、书面词那些是「风格分」，扣分就完了；
# 硬痕迹是「可定位的错」，每一条都必须指回原文的某一段，并给一句能直接照做的改法。
#
# 加规则的口径：只收那些「人不会这么写、机器很容易这么写」的东西。
# 像「因此」「基于」那种书面词已经在 WRITTEN_WORDS 里按密度扣分了，不在这里重复报。

DASH_RE = re.compile(r"——|—")
VAGUE_AUTHORITY_RE = re.compile(
    r"研究表明|数据显示|调查显示|据统计|相关研究显示|业内普遍认为|业内普遍共识|"
    r"专家指出|有专家表示|权威人士称"
)
SCAFFOLD_RE = re.compile(r"[【】†‡◆◇■□▪▫]")
CITE_RESIDUE_RE = re.compile(r"oaicite|turn\d+search\d+|grok_card|\[cite:\s*\d+\]", re.I)
CALLOUT_RE = re.compile(r"让我们一起|让我们|共同拥抱|一起拥抱|携手共创|共同打造|一起见证")
IDIOM_RE = re.compile(
    r"至关重要|不可或缺|息息相关|众所周知|不言而喻|毋庸置疑|举足轻重|相辅相成|"
    r"精益求精|全力以赴|与时俱进|深入人心|层出不穷|日新月异|方兴未艾"
)
# 排序词只在句首且后面接逗号/「要/我/我们」时才算痕迹。
# 单看「最后」会把「最后一版简历」这种正常说法误伤，所以必须带上下文。
SEQUENCE_RE = re.compile(r"^\s*(?:首先|其次|最后|再者)\s*(?:[，,、：:]|要|我们|我|你)")
SUBLIME_TAIL_WORDS = ("未来", "时代", "行业", "趋势", "新篇章", "星辰大海", "浪潮", "格局")

SUBLIME_TAIL_CHARS = 12  # 只看句尾这么多个字，超出就不算「段尾升华」


def _hit_dash(s: str):
    m = DASH_RE.search(s)
    return m.group(0) if m else None


def _hit_vague_authority(s: str):
    m = VAGUE_AUTHORITY_RE.search(s)
    return m.group(0) if m else None


def _hit_scaffold(s: str):
    m = SCAFFOLD_RE.search(s)
    return m.group(0) if m else None


def _hit_cite_residue(s: str):
    m = CITE_RESIDUE_RE.search(s)
    return m.group(0) if m else None


def _hit_callout(s: str):
    m = CALLOUT_RE.search(s)
    return m.group(0) if m else None


def _hit_idiom(s: str):
    m = IDIOM_RE.search(s)
    return m.group(0) if m else None


def _hit_sequence(s: str):
    m = SEQUENCE_RE.search(s)
    return m.group(0) if m else None


def _hit_sublime_tail(s: str):
    tail = s[-SUBLIME_TAIL_CHARS:]
    for w in SUBLIME_TAIL_WORDS:
        if w in tail:
            return w
    return None


# (key, finder, 问题模板, 改法函数)
# 改法函数拿得到原句和命中片段，能直接给出一句可替换的文本。
HARD_TELL_RULES = [
    (
        "dash",
        _hit_dash,
        "用了破折号「{}」，口播时没有对应的语气，念到这儿会卡一下",
        lambda s, hit: s.replace("——", "。").replace("—", "。"),
    ),
    (
        "vague_authority",
        _hit_vague_authority,
        "「{}」后面没跟具体出处，等于借一个查不到的人给自己背书",
        lambda s, hit: s.replace(hit, "", 1).lstrip("，、 ") or "删掉这半句，把结论自己讲出来",
    ),
    (
        "scaffold",
        _hit_scaffold,
        "句子里留着「{}」这种模板记号，一眼看出不是人写的",
        lambda s, hit: SCAFFOLD_RE.sub("", s).strip()
        or "删掉这个记号，把后面那句话直接当开头",
    ),
    (
        "cite_residue",
        _hit_cite_residue,
        "出现了「{}」这种工具残留，是从别处粘过来没清干净的",
        lambda s, hit: CITE_RESIDUE_RE.sub("", s).strip() or "整段删掉这个标记",
    ),
    (
        "callout",
        _hit_callout,
        "「{}」是喊口号的句式，听众不会因为被号召就动手",
        lambda s, hit: s.replace(hit, "", 1).strip("，、 ")
        or "把号召换成一个今天就能做的具体动作",
    ),
    (
        "idiom_pile",
        _hit_idiom,
        "一句话里塞了「{}」这类四字词，听着像通稿",
        lambda s, hit: "挑一个留下，其余删掉，换成一个能看见的画面",
    ),
    (
        "sequence",
        _hit_sequence,
        "「{}」是汇报里的排序词，口语里没人这么起句",
        lambda s, hit: s.replace(hit + "，", "", 1).replace(hit, "", 1).strip()
        or "直接说要做的第一件事",
    ),
    (
        "sublime_tail",
        _hit_sublime_tail,
        "句尾升到「{}」这种大词上，前面讲的具体东西被抽空了",
        lambda s, hit: "删掉最后这句大词，停在前一句的具体事实上",
    ),
]


def hard_tell_issues(sents: list[str]) -> list[dict]:
    """按句子扫硬痕迹，每类只报第一条，返回可直接展示的 issue 列表。

    quote 一律取原句切片，保证能在原文里搜到，用户点一下就能定位。
    """
    out: list[dict] = []
    seen: set[str] = set()

    for s in sents:
        for key, finder, problem_tpl, fix_fn in HARD_TELL_RULES:
            if key in seen:
                continue
            hit = finder(s)
            if not hit:
                continue
            seen.add(key)
            try:
                fix_text = fix_fn(s, hit) or ""
            except Exception:
                fix_text = ""
            out.append(
                {
                    "quote": s if len(s) <= 40 else s[:40],
                    "problem": problem_tpl.format(hit),
                    "fix": fix_text.strip() or "删掉这一句，换成一句具体的话",
                }
            )

    return out



def analyze_text(text: str) -> dict:
    """规则体检。返回 {metrics, total, rule_issues}。

    total 分四块：句长 30 + 书面词 25 + 具体度 25 + 立场 20。
    每一块都是 0 到满分之间线性取，没有凭空的加减分。
    """
    text = text or ""
    sents = split_sentences(text)
    n = max(1, len(sents))
    lens = [sentence_len(s) for s in sents] or [0]

    avg = sum(lens) / max(1, len(lens))
    long_ratio = sum(1 for x in lens if x > LONG_SENTENCE) / max(1, len(lens))

    written_sents = [s for s in sents if any(w in s for w in WRITTEN_WORDS)]
    written_ratio = len(written_sents) / n
    written_hits = _count_words(text, WRITTEN_WORDS)

    concrete = _concrete_hits(text)
    concrete_per100 = concrete / max(1, count_chars(text)) * 100
    stance = _count_words(text, STANCE_MARKERS)

    # 句长：平均 18 字以内满分，35 字以上 0 分
    if avg <= 18:
        s_len = 30.0
    elif avg >= 35:
        s_len = 0.0
    else:
        s_len = 30.0 * (35 - avg) / 17.0

    # 书面词：一半以上句子带书面词就是 0 分
    s_written = 25.0 * (1 - min(written_ratio / 0.5, 1.0))

    # 具体度：每 100 字有 2.5 个具体物满分
    s_concrete = 25.0 * min(concrete_per100 / 2.5, 1.0)

    # 立场：出现 3 次满分
    s_stance = 20.0 * min(stance / 3.0, 1.0)

    total = int(round(s_len + s_written + s_concrete + s_stance))

    # ---- 规则问题：硬痕迹优先，其次长句，再书面词，最后缺立场和缺画面
    # 硬痕迹排在最前是有意的：它是可定位的错，不该被「长句」这类风格问题挤出名额。
    issues: list[dict] = list(hard_tell_issues(sents))

    long_issues: list[dict] = []
    for s in sents:
        if sentence_len(s) > LONG_SENTENCE:
            long_issues.append(
                {
                    "quote": s if len(s) <= 40 else s[:40],
                    "problem": f"这一句 {sentence_len(s)} 字，一口气念不完，念到一半要换气",
                    "fix": _long_sentence_fix(s),
                }
            )
    issues.extend(long_issues[:4])

    for s in written_sents[:3]:
        word = next((w for w in WRITTEN_WORDS if w in s), None)
        if word:
            issues.append(_written_issue(s, word))

    if stance == 0 and sents:
        issues.append(
            {
                "quote": sents[0] if len(sents[0]) <= 40 else sents[0][:40],
                "problem": "全文没有一处你自己的判断，读起来像转述别人的话",
                "fix": "我认为" + sents[0],
            }
        )

    if concrete_per100 < 1 and sents:
        issues.append(
            {
                "quote": sents[0] if len(sents[0]) <= 40 else sents[0][:40],
                "problem": f"每 100 字只有 {concrete_per100:.1f} 个具体的东西，读者看不见画面",
                "fix": "在开头补一个具体的场景：谁、在哪、手上在做什么",
            }
        )

    return {
        "metrics": {
            "avg_sentence_len": round(avg, 1),
            "long_sentence_ratio": round(long_ratio, 3),
            "written_word_ratio": round(written_ratio, 3),
            "concrete_hits": int(concrete),
            "stance_hits": int(stance),
        },
        "total": max(0, min(100, total)),
        "rule_issues": issues,
    }


def pace(text: str, wpm: int = 270) -> dict:
    """口播节奏估算。

    中文口播按 4.5 字/秒（= 270 字/分钟）。停顿单独加：逗号后 0.3 秒、句末 0.6 秒。
    句内逗号的停顿既算进这一句的 duration，也体现在总时长里，不重复计入 pause 字段。
    """
    wpm = int(wpm or 270)
    if wpm <= 0:
        wpm = 270
    cps = wpm / 60.0  # 字/秒

    lines = []
    warnings = []
    total = 0.0
    chars_total = 0

    for idx, sent in enumerate(split_sentences(text), 1):
        speak = sentence_len(sent)
        commas = len(COMMA_RE.findall(sent))
        inner_pause = round(commas * 0.3, 2)
        pause = 0.6  # 句末
        duration = round(speak / cps + inner_pause, 2)

        too_long = speak > 18
        lines.append(
            {
                "text": sent,
                "chars": speak,
                "pause": pause,
                "too_long": too_long,
                "duration": duration,
            }
        )
        chars_total += speak
        total += duration + pause

        if speak > 25:
            warnings.append(
                f"第 {idx} 句 {speak} 字，超过 25 字，念出来会赶，建议拆成两句"
            )
        elif too_long:
            warnings.append(f"第 {idx} 句 {speak} 字，超过 18 字，稍微收一下")

    return {
        "estimated_seconds": round(total, 1),
        "chars": chars_total,
        "lines": lines,
        "warnings": warnings,
    }
