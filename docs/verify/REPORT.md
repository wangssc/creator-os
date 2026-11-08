# 验收报告 2026-11-08 20:59

## 总判定

**PASS**

## 红线

| 断言 | 结果 | 证据 |
| --- | --- | --- |
| 断言 1：空素材母题的 own_detail 必须是空字符串 | PASS | {"id": 46, "claim": "小处省钱导致大处失机", "own_detail": "", "missing": ["缺一手经历，请补一条亲历", "缺数据支持，如节俭与财富关系的统计研究", "缺名人或专家观点支持"]} |
| 断言 2：missing 里必须包含「缺一手经历」 | PASS | missing=["缺一手经历，请补一条亲历", "缺数据支持，如节俭与财富关系的统计研究", "缺名人或专家观点支持"] |
| 断言 3：有素材母题的 own_detail 必须非空 | PASS | {"id": 47, "own_detail": "我改过 17 版简历，最后一版只剩一行项目描述，面试邀约反而变多"} |
| 断言 4：own_detail 每句都能在 evidence 原文里逐字找到 | PASS | own_detail=我改过 17 版简历，最后一版只剩一行项目描述，面试邀约反而变多；素材=["我改过 17 版简历，最后一版只剩一行项目描述，面试邀约反而变多", "室友把社团经历写了八条，投三十家只回了两个"] |
| 断言 5：派生结果不含第一人称经历组合词 | PASS | 全文 1168 字，无命中；开头：小处省钱导致大处失机 很多人习惯在小处精打细算，却不知道这种习惯正在悄悄消耗他们的注意力，让他们错失更大的机会。 小钱耗大注意力 小钱消耗注意力会导致大机会被忽视。一个人每天花半小时比较不同平台的优惠券。这些时间本可以用来学习新技能。你看， |

## 功能验收

| # | 项目 | 结果 | 证据 |
| --- | --- | --- | --- |
| V01 | 服务存活 /api/stats | PASS | http 200 week_generated=2 |
| V02 | GET /api/style | PASS | len=180 active=True |
| V03 | 体检硬痕迹检出 7/7 | PASS | 命中=['破折号 ——', '无源权威 研究表明', '段头【】', '首先其次最后', '让我们 一起', '四字成语堆砌', '段尾升华 未来'] 漏=[] total=38 耗时=13.7s |
| V04 | 体检对 AI 味文本评分 < 60 | PASS | total=38 issues=12 |
| V05 | 估时落在 裸算~裸算+35% | PASS | chars=272 裸算=60.4s 实际=73.0s |
| V06 | 批量匹配 <500ms 且无 model_error | PASS | 47ms count=20 matched=20 |
| V07 | 单条 angle <15s 且 8-60 字 | PASS | 6.3s len=14 angle=于正求原谅，解决才是简历亮点 |
| V08 | 空素材母题 own_detail 必须是空串 | PASS | thesis 1 own_detail='' missing=['缺一手经历，请补一条亲历', '缺具体案例说明省钱如何限制机会', '缺数据支持大方投资带来的长期收益'] |
| V12 | 空素材时必须提示缺一手经历 | PASS | missing=['缺一手经历，请补一条亲历', '缺具体案例说明省钱如何限制机会', '缺数据支持大方投资带来的长期收益'] |
| V09 | 有素材母题 own_detail 逐字可回溯 | PASS | thesis 9 own='我改过 17 版简历，最后一版只剩一行项目描述，面试邀约反而' 在素材原文里=True |
| V10 | 空素材派生文章不含编造经历 | PASS | 命中禁词=[] |
| V11 | posts 表已清空 | PASS | posts=0 reviews=0 |
| V13 | 三个生成提示词都带反编造+具体性界线 | PASS | {'article.md': True, 'note.md': True, 'script.md': True} |

## 前端静态检查

| # | 项目 | 结果 | 说明 |
| --- | --- | --- | --- |
| 1 | index.html 存在 pageCard 函数 | PASS | 剥离注释后匹配 1 处；index.html 150671 字符 |
| 2 | MOCK_MODE 默认值为 false | PASS | 实际赋值=['false'] 代码中 MOCK_MODE = true 次数=0（注释里提到 MOCK_MODE = true，但不在代码中，不计为违规） |
| 3 | teleprompter.html 存在且含空格暂停逻辑 | PASS | 10544 字符；空格监听=True 暂停逻辑=True |
| 4 | CSS 变量段（:root）存在且含 --green: 变量 | PASS | index.html 自身 :root 解析=True 声明数=17；精确命中 --green: 声明=['--green: #16A34A']（原备份对比无备份即 SKIP，已收敛为自身检查） |
| 5 | 没有 .py 文件被前端改动过 | PASS | 无 .py 与前端文件在 120 秒内同时修改（注意：时间线只能证否，不能证明作者）；后端：weibo.py@18:06:24；toutiao.py@18:06:36；zhihu.py@18:07:31；baidu.py@18:07:35；llm.py@18:08:38；__init__.py@21:42:57；rules.py@21:44:43；hotapi.py@21:48:53；db.py@ …（截断） |
| 6 | 四模块不再读 MOCK 硬编码 | PASS | 剥离注释后引用计数=0 命中=[]；MOCK 对象定义命中=0 命中=[]（定义命中=0 才 PASS）；LATER_MODULES 残留 market/books/replicate 键=[] |

## 模块验收

| # | 项目 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | GET / 返回 200 | PASS | GET / -> HTTP 200（期望 200）；根路径返回 HTML，不断言 JSON |
| 2 | GET /api/market 基础结构与 starNum 降序 | PASS | HTTP 200 ok=True items=9 total=9 cats=6；每条含 url=True；starNum 序列=[52.3, 41.8, 38.6, 32.1, 28.4, 24.7, 21.9, 12.4, 9.8] 降序=True |
| 3 | GET /api/market?q=自动化&cat=效率 过滤生效 | PASS | HTTP 200 返回 1 条，cat 列表=['效率']；全部为「效率」或空结果=True |
| 4 | GET /api/books 8 条且无正文字段 | PASS | HTTP 200 items=8（期望 8）全部 enabled=true=True 每条含 url=True；url 实际值=['', '', '', '', '', '', '', '']；响应体正文字段命中=[]（期望 []） |
| 5 | GET /api/points 结构与记录字段 | PASS | HTTP 200 balance=793（数值=True）delta=本周 -67 records=13 每条含 balance_after=True rules=3（非空=True）；record0={"ts": "2026-11-08 20:59:06", "reason": "生成内容 · derive", "delta": -10, "balance_after": 793} |
| 6 | POST /api/points/adjust 前后对比 + sqlite 行数 | PASS | POST /api/points/adjust HTTP 200 ok=True；前 balance=793 records=13 sqlite行数=13 → 后 balance=792 records=14 sqlite行数=14；期望 balance-1 / records+1 / sqlite+1；data={"ts": "2026-11-08 20:59:07", "reason": "模 …（截断） |
| 7 | POST /api/replicate multipart 与无落库表 | PASS | POST /api/replicate（multipart 字段 files，1x1 PNG 70 字节）HTTP 200 ok=True；structure/layers/hooks/directions 非空=True file_count=1；note="结构报告为本地模板生成，未调用视觉模型；本次收到 1 个文件，各文件字节数：70 字节。"；sqlite 表=['topics', 'sq …（截断） |
| 8 | index.html 无四模块 MOCK 表达式且 LATER_MODULES 解禁 | PASS | 剥离注释后引用计数=0 命中=[]；MOCK 对象定义命中=0 命中=[]（定义命中=0 才 PASS）；LATER_MODULES 段=diagnose: "账号诊断"；残留 market/books/replicate 键=[]；diagnose 保留=True |
| 9 | app.py 扣分挂载静态断言 | PASS | 扣分挂载为静态直读：app.py 中 db.points_change( 出现 7 次（≥6），POINT_COST 常量存在=True；运行时回归由全量 run_all.py 覆盖，不在本脚本内断言；运行时探测 key_configured=True |

## 发现的问题（按严重程度排序）

本轮没有 FAIL 项。

## 无法验证的部分

1. supports 的因果性：脚本只核对条数（2-3），「因为」后面是否真的构成因果/对比，需要人读一遍。
2. 页面视觉合理性：index.html 各页面的布局、配色、留白、移动端断点，静态检查无法判断。
3. 热点真实性：只能判断标题不重复、无占位词、带 platform，无法确认抓的是当下真实榜位。
4. CSS :root 由 check_frontend.py 第 4 项在 index.html 自身检查（:root 段含 --green: 变量），不再依赖备份目录。
5. .py 的作者归属：文件修改时间只能证明「有没有在 120 秒内交叉写入」，不能证明是哪个工程师改的。
6. 提词器的实际滚动与空格暂停效果：需要真人对着屏幕按一次空格才能确认。
7. LLM 输出的稳定性：同一接口多次调用结果不同，本轮 PASS 不代表下一轮还 PASS（本轮红线断言 5 即为一次随机命中）。

## 阶段执行情况

| 阶段 | 退出码 | 结论 |
| --- | --- | --- |
| 环境自检 | 0 | 通过 |
| 红线 | 0 | 通过 |
| 功能 | 0 | 通过 |
| 前端 | 0 | 通过 |
| 模块验收 | 0 | 通过 |
