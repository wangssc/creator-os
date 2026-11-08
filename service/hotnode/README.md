# hotnode —— creator-os 的热点采集旁路服务

一个独立的 Node 进程，引用 `D:\aa\refs\DailyHotApi`（MIT）的采集实现，
把 56 个平台收敛成一个稳定出口，给 Python 主服务用。

## 为什么不是直接在 Python 里重写

DailyHotApi 里有 56 个平台各自的反爬处理、字段差异、时间格式兼容。
这部分价值在「已经跑通」，重写一遍等于把这些坑重新踩一次。
所以主项目技术栈不动（FastAPI + SQLite 照旧），只是把「抓」这一步外挂到一个 Node 进程。

**边界很清楚**：Node 侧不碰数据库、不碰 prompts、不 import 任何 Python 代码；
Python 侧只是 HTTP 客户端，进程里没有任何 Node 依赖。

## 启动

双击 `启动.cmd`，或：

```cmd
cd /d D:\aa\creator-os\service\hotnode
npm start
```

首次运行先 `npm install`（`启动.cmd` 会自动做）。
另外 `D:\aa\refs\DailyHotApi` 本身也要装过一次依赖：

```cmd
cd /d D:\aa\refs\DailyHotApi
npm install
```

用 `tsx` 直接跑 TypeScript，不做构建：少一步 `tsc` 就少一处和参考仓库版本错位的可能。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /hot/:platform` | 单平台，返回统一结构。`?cache=false` 绕过 DailyHotApi 自带 60s 缓存 |
| `GET /hot` | 全部 13 个平台一次拉完，排查用（慢，日常刷新不走这个） |
| `GET /platforms` | 平台映射表原文，核对用 |
| `GET /health` | 探活，顺带回显引用的仓库路径和缺失平台 |

统一结构：

```json
{
  "ok": true,
  "data": {
    "platform": "weibo",
    "name": "微博",
    "title": "微博",
    "type": "热搜榜",
    "updateTime": "...",
    "fromCache": false,
    "total": 51,
    "items": [
      { "rank": 1, "title": "...", "heat": 0, "url": "...", "desc": "...", "timestamp": 1761... }
    ]
  },
  "cost_ms": 812
}
```

失败（平台不在白名单 / 上游 502 / 返回 0 条）：

```json
{ "ok": false, "error": "AxiosError: Request failed with status code 502", "platform": "v2ex" }
```

## 采哪些平台

`platforms.json` 是**唯一事实来源**：Node 侧拿它做接口白名单 + 展示名，
Python 侧 `config.py` 读同一个文件决定刷新哪几个。改平台只改这一个文件。

当前 13 个：weibo、zhihu、baidu、toutiao、bilibili、douyin、36kr、huxiu、
sspai、juejin、ithome、thepaper、v2ex。

## 已知情况（2026-10-31 实测）

- **v2ex 采不到**：上游 `https://www.v2ex.com/api/topics/hot.json` 从本机直连返回 502，
  并且要耗 21s 才失败。不是代码问题，换网络环境（挂代理）才会好。
  它不会拖垮整批：单平台失败只记 0 条，其余 12 个照常入库。
- **微博没有热度值**：DailyHotApi 的 weibo 路由把 `hot` 写成 `undefined`（上游接口确实有 `num`，
  但它没取）。所以 weibo 这批 `heat=0`，按热度排序对它无效，按时间排序正常。
  要修就得改参考仓库，本次没动。
- **单次全量刷新约 24 秒**：22 秒花在等 v2ex 超时，正常平台 1~8 秒就回来了。
  `config.HOT_API_TIMEOUT_SEC = 25`，低于 12 会让 thepaper（实测 11.9s）随机失败。

## 排障

```cmd
REM 服务活了没
curl http://127.0.0.1:6688/health

REM 单个平台通不通
curl "http://127.0.0.1:6688/hot/weibo"

REM 端口被占
netstat -ano | findstr :6688
taskkill /F /PID <PID>
```

**注意本机有 `http_proxy` 环境变量**（127.0.0.1:60809 上跑着代理）。
手动 `curl` 本机端口要加 `--noproxy '*'`，否则请求会被代理截走报 502。
Python 侧的 httpx 客户端已经设了 `trust_env=False`，不受影响。
