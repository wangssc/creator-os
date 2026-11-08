# Creator OS 启动说明

## 最快的方式

双击项目根目录的 **`启动.cmd`**。它会自动找能用的解释器、检查依赖、开浏览器。

## 为什么不能直接敲 `python app.py`

本机 `PATH` 上的 `python` 指向 `D:\python.exe`（Python 3.14.2），这个解释器**没有 pip、也没装本项目依赖**。
直接用它启动会报 `ModuleNotFoundError: No module named 'fastapi'`。

实际能跑本项目的解释器是：

```
C:\Users\32719\.workbuddy\binaries\python\versions\3.13.12\python.exe
```

`启动.cmd` 里已经硬编码了这一条作为首选，找不到才回退到别的。

## 手动启动（等价命令）

```cmd
cd /d D:\aa\creator-os

"C:\Users\32719\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8787
```

打开 http://127.0.0.1:8787

## 热点采集要多起一个 Node 服务

`service/hotnode` 是一个独立的 Node 进程，引用 `D:\aa\refs\DailyHotApi`，
负责抓 13 个平台（微博/知乎/百度/头条/B站/抖音/36氪/虎嗅/少数派/掘金/IT之家/澎湃/V2EX）。

```cmd
cd /d D:\aa\creator-os\service\hotnode
npm start
```

或者双击 `service\hotnode\启动.cmd`。它只监听 127.0.0.1:6688，不对外。

**不起也能用**：主服务的 `POST /api/hot/refresh` 会先打这个服务，连不上就自动回退到
`collectors/` 里原来的四源实现（微博/知乎/百度/头条），只是平台从 13 个退回 4 个。
库里 `topics.source` 记录这批数据是哪条路径来的：`hotnode` 或 `local`。

细节和已知问题（v2ex 在本机采不到、微博没有热度值）见 `service/hotnode/README.md`。

## 依赖（已经装好了，重装才需要）

```cmd
"C:\Users\32719\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m pip install fastapi uvicorn httpx python-dotenv
```

## 模型密钥

`.env` 里的 `ZHIPU_API_KEY` 已填好（取自本机 modlens 配置，同一个智谱账号）。

不填也能启动：热点采集、信息展示、历史记录全部可用，只有三个生成接口会返回 `未配置 ZHIPU_API_KEY`。

换 key 就编辑 `.env` 这一行，改完**必须重启服务**（配置在启动时读入）。

## 模型选择（改 `config.py` 的 `MODEL_CHAIN`）

实测延迟与质量，按这个顺序：

| 模型 | 单次耗时 | 说明 |
|---|---|---|
| `glm-4.5-air` | 约 2–15 秒 | 首选，指令遵循最好 |
| `glm-4-flash` | 约 2 秒 | 快，但内容偏模板化，容易编数据 |
| `glm-4-plus` | 约 1 秒 | 最后兜底 |

**不要用 `glm-4.5-flash`**：实测单次 40 秒以上，会撞超时再回退，等于白等。

## 数据库

`D:\aa\creator-os\data\creator.db`，删掉即重置（热点会重新抓）。

## 常用接口自测

```cmd
curl http://127.0.0.1:8787/api/stats
curl "http://127.0.0.1:8787/api/hot?limit=5"
curl -X POST http://127.0.0.1:8787/api/hot/refresh
```

## 端口被占用

```cmd
netstat -ano | findstr :8787
taskkill /F /PID <上面查到的PID>
```

## 日志

服务日志在 `data\_stdout.log` 和 `data\_stderr.log`。生成失败先看这里。
