# Creator OS

一个跑在本机的自媒体内容工作台：扒全网热点 → 生成公众号/小红书稿 → 渲成贴图号卡片 → 发出去后录数据做复盘。
前后端都在本地，数据存 SQLite，不上传任何服务器。

---

## 启动

```bash
cd D:/aa/creator-os
cp .env.example .env        # 然后把 ZHIPU_API_KEY 填进去
python -m uvicorn app:app --host 127.0.0.1 --port 8787
```

打开 <http://127.0.0.1:8787>。

依赖五个：`fastapi` `uvicorn` `httpx` `python-dotenv` `python-multipart`（Python 3.10+，`int | None` 写法需要；`python-multipart` 供 `POST /api/replicate` 的 multipart 上传使用）。

```bash
pip install fastapi uvicorn httpx python-dotenv python-multipart
```

> 更详细的排障（端口被占、Python 路径怎么找、库文件在哪）见 `RUN.md`。

**没有 API Key 会怎样**：热点采集、发布记录、复盘的数据读表都正常；文章/笔记/脚本/卡片这四类生成会直接返回
`{"ok":false,"error":"未配置 ZHIPU_API_KEY"}`。不会假装成功。

---

## 四个能用的模块

### 1. 热点（`/api/hot`）

四个源实时扒：微博、知乎、百度、今日头条。纯标准库 + httpx，不需要任何第三方爬虫包。

- 数据落 `topics` 表，按平台累积历史（当前 1642 行历史，每平台最新一轮合计 182 条）；同一标题 30 分钟内不重复抓（`REFRESH_INTERVAL_SEC`）
- 首页「热点」数字的口径是**每平台最新一轮**的条数之和，不是历史总行数，否则这个数只会一直涨、失去意义
- 抓下来带 `source` / `title` / `url` / `heat`，百度当前接口已无热度字段，如实记 0，前端显示「热度 —」而不是编个数
- 看到想写的点「去写作」，标题直接带进生成页

### 2. 生成（`/api/gen/article`、`/api/gen/note`、`/api/gen/script`）

按公众号长文 / 小红书笔记 / 短视频口播稿三套 prompt 走智谱。

- 模型回退链：`glm-4.5-flash` → `glm-4-flash` → `glm-4.6`，前一个超时或 5xx 就换下一个
- JSON 模式输出，模型偶尔包 ```` ```json ```` 围栏会自动剥掉；解析失败重试 1 次
- 每次生成都写 `contents` 表，`model` 字段记真实返回的模型名，方便回溯是哪一版写的
- 「一键多平台」是一次调 article + note，同一主题出两版，不用重新描述需求

### 3. 贴图号（`/api/gen/cards` + `static/render/card.html`）

把一段文案拆成 6–9 张 1080×1440 的卡片，Canvas 在浏览器里画，下载是 PNG。

- 8 种版式：大字居中 / 三行递进 / 四宫格 / 深色反白 / 左侧竖线 / 金句居中 / 时间轴 / 封面图
- 中文按字测量宽度折行（`measureText` 逐字累加），标题太长自动缩字号，不会出现半截字
- 渲染器是独立页面，主页面用 iframe 调 `contentWindow.downloadPng()`，所以改样式不用动主程序

### 4. 发布复盘（`/api/posts` + `/api/review`）

发出去之后把阅读/点赞/转发/评论录进来，让模型对比着看哪类内容真跑赢了。

- **互动率口径全站唯一**：`rate = (点赞 + 转发 + 评论) / 阅读`，阅读为 0 时存 `NULL` 不存 0。
  所有写入都走 `calc_rate()`，不允许在别处再实现一遍，否则前后端口径分叉、结论就不可信
- 录入 6 项（标题 / 平台 / 阅读 / 点赞 / 转发 / 评论），互动率由服务端算，前端不参与
- 复盘固定输出三段：**最好与最差的对比**、**时间因素**、**3 条下周可执行动作**
  
  硬约束写在 `prompts/review.md` 里：只许用表格里的数字、不许「建议多发优质内容」这类空话、
  每条动作必须写清「做什么 + 怎么验证」。改 prompt 不用改代码
- 复盘结果落 `reviews` 表，可回看历史

### 首页连续使用天数

不是前端 localStorage 假的。`db.touch_streak()` 在每次 `/api/stats` 时比日期：
昨天访问过就 +1，断一天归 1，同一天反复刷新不重复累加。状态存在 `settings` 表。

### 5. 四个数据模块（真实 API）

- `GET /api/market?q=&cat=&limit=200`：AI 超市，读 `data/market.json` 静态快照，按 star 降序
- `GET /api/books?cat=`：AI 书架，读 `data/books.json` 静态快照，只回元信息
- `GET /api/points` 与 `POST /api/points/adjust`：积分余额与流水；余额存 `settings`、流水存 `point_records`
- `POST /api/replicate`：爆款贴图复刻，multipart 上传 1–8 张图，只返回本地结构报告

market/books 为静态快照 JSON、points 落库、replicate 不留存。

### 6. 账号诊断（`/api/diagnose/*`）

把你导出的内容数据贴进来或传 CSV，算出四项指标、周内走势和能直接执行的建议。

- 输入：`标题` `平台` `阅读` 必填，`发布时间` `点赞` `转发` `评论` `粉丝数` 可选；
  表头接受常见同义写法（`阅读量` / `播放` / `views` …），缺必需列直接报错并回显读到的表头
- 两个入口都是真接口：粘贴（Tab 或逗号分隔）发 JSON，选文件走 multipart；
  CSV 按 UTF-8 / GBK 解码，Excel 明确拒绝并提示「请另存为 CSV」
- 互动率复用 `calc_rate()`；算不出来的指标返回 `null`，界面显示「—」，不填 0、不填示例值
- 建议先走纯规则（异常点 > 互动率 > 更新频率 > 质量分 > 稳定性，最多 3 条），
  配了 `ZHIPU_API_KEY` 才额外让模型写一句总评（`prompts/diagnose.md`）；模型失败不影响诊断结果，`note` 为 `null`
- 每次导入落一行 `diagnose_runs`，`payload` 里同时存原始行和算完的结果，回看时每个数字都能追回具体哪一行

---

## 目录

```
app.py            FastAPI 全部路由
config.py         配置与常量（端口、模型链、库路径、UA）
db.py             建表 / 迁移 / 短连接 / 连续天数
llm.py            模型调用（重试、回退、JSON 模式）
collectors/       微博 知乎 百度 头条 四个采集器
prompts/          article / note / script / cards / review / diagnose 六套 prompt
static/index.html 前端单体（无框架，var S + render() 重绘）
static/render/card.html  Canvas 卡片渲染器
data/creator.db   SQLite（已 gitignore）

十张表：topics（热点历史）/ contents（生成结果）/ posts（发布记录）/ settings（状态与积分余额）/
reviews（复盘）/ templates（预留）/ theses（母题库）/ thesis_cards（观点卡）/
point_records（积分流水）/ diagnose_runs（账号诊断的导入批次与结果）
```

---

## 已知限制

1. **没 key 时生成链路是空跑**。真实响应、JSON 解析、模型回退、`contents` 落库这几段都只在错误路径上验证过。
2. **热点源是公开接口，随时会变**。知乎原来那个 `www.zhihu.com/api/v3/feed/...` 已经 401，
   现在走 `api.zhihu.com`；哪天四家里有几家返回 0 条，先怀疑对方改接口，不是代码坏了。
3. **复盘至少要有 2 条以上数据才有意义**，1 条只能读出「它自己」。想让「时间因素」那段有料，
   最好横跨 7 天以上录，且同一周内不同时段都发过。
4. **表里没有「标题风格」这个字段**。模型是从标题文本自己判断是数字型/疑问型/故事型的，
   判断可能和你不一样。要稳定归因得自己加一列 `title_style` 手填。
5. **无鉴权，只监听 127.0.0.1**。不要改成 `0.0.0.0` 挂公网，`/api/settings` 虽然过滤了密钥类键名，
   但整站没有登录，谁都能调。
6. **SQLite 短连接，不适合并发**。单人单机没问题，多标签页同时写有极小概率 `database is locked`。
7. **market / books / points / replicate / diagnose 已接真实数据**：AI 超市、AI 书架、积分查看、爆款贴图复刻
   分别走 `/api/market`、`/api/books`、`/api/points`、`/api/replicate`，不再读前端 MOCK 硬编码；
   账号诊断已上线，走手动导入（`/api/diagnose/import` + `/run`），不接任何平台接口——
   公众号 / 小红书 / 抖音都没有面向个人开发者的开放数据接口，自动同步仍不做。
