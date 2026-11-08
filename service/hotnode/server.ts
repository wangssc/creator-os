/**
 * creator-os 热点采集旁路服务（hotnode）
 *
 * 为什么单独起一个 Node 进程：
 *   DailyHotApi 是 TypeScript + Hono，56 个平台各自处理反爬与字段差异，
 *   这部分资产不值得用 Python 重写一遍。主项目仍是 Python（FastAPI），
 *   这里只做一个薄壳：把 DailyHotApi 的路由函数搬到一个统一出口，
 *   对 Python 侧只暴露 GET /hot/:platform 的稳定结构。
 *
 * 边界：
 *   - 本进程不碰数据库、不碰 prompts、不 import 主项目任何 Python 代码。
 *   - Python 侧只是 HTTP 客户端，拿不到也加载不到任何 Node 依赖。
 */

import { serve } from "@hono/node-server";
import { Hono } from "hono";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// 端口与监听地址：默认只绑本机，避免把采集能力暴露到局域网
const PORT = Number(process.env.HOTNODE_PORT || 6688);
const HOST = process.env.HOTNODE_HOST || "127.0.0.1";

// DailyHotApi 仓库位置：默认取同级 refs 目录，可用环境变量覆盖
const REF_DIR =
  process.env.DAILYHOT_DIR || path.resolve(__dirname, "../../../refs/DailyHotApi");
const SRC_ROUTES = path.join(REF_DIR, "src", "routes");
const DIST_ROUTES = path.join(REF_DIR, "dist", "routes");

// ---------------------------------------------------------------- 平台映射表

type PlatformMeta = {
  slug: string;
  name: string;
  type?: string;
  note?: string;
};

function loadPlatforms(): PlatformMeta[] {
  const file = path.join(__dirname, "platforms.json");
  const raw = JSON.parse(fs.readFileSync(file, "utf8")) as PlatformMeta[];
  if (!Array.isArray(raw) || raw.length === 0) {
    throw new Error(`platforms.json 为空：${file}`);
  }
  return raw;
}

const PLATFORMS = loadPlatforms();
const BY_SLUG = new Map(PLATFORMS.map((p) => [p.slug, p]));

/**
 * 找到某个平台对应的路由文件。
 * 有构建产物优先用 dist（生产更稳），否则回退到 src 的 .ts（由 tsx 现场编译）。
 */
function resolveRouteFile(slug: string): string | null {
  for (const base of [DIST_ROUTES, SRC_ROUTES]) {
    for (const ext of [".js", ".ts"]) {
      const f = path.join(base, slug + ext);
      if (fs.existsSync(f)) return f;
    }
  }
  return null;
}

// 启动时就校验一遍：映射表里写了但仓库里没有，属于配置错误，早报比晚报好
const MISSING = PLATFORMS.filter((p) => !resolveRouteFile(p.slug)).map((p) => p.slug);
if (MISSING.length) {
  console.warn(`[hotnode] 以下平台在 DailyHotApi 里找不到路由：${MISSING.join(", ")}`);
}

// 路由模块缓存：动态 import 只做一次，后续请求复用同一份模块实例（内部有 node-cache）
const routeCache = new Map<string, any>();

async function loadHandler(slug: string): Promise<any> {
  const hit = routeCache.get(slug);
  if (hit) return hit;
  const file = resolveRouteFile(slug);
  if (!file) throw new Error(`DailyHotApi 缺少路由文件：${slug}`);
  const mod = await import(pathToFileURL(file).href);
  const handler = mod.handleRoute;
  if (typeof handler !== "function") {
    throw new Error(`路由文件没有导出 handleRoute：${file}`);
  }
  routeCache.set(slug, handler);
  return handler;
}

// ---------------------------------------------------------------- 统一结构

/** DailyHotApi 的 hot 可能是数字、字符串、undefined，统一成整数。 */
function toHeat(v: unknown): number {
  if (typeof v === "number" && Number.isFinite(v)) return Math.trunc(v);
  if (typeof v === "string") {
    const m = v.replace(/,/g, "").match(/([\d.]+)\s*(万|亿)?/);
    if (!m) return 0;
    let n = Number(m[1]);
    if (!Number.isFinite(n)) return 0;
    if (m[2] === "万") n *= 1e4;
    if (m[2] === "亿") n *= 1e8;
    return Math.trunc(n);
  }
  return 0;
}

type UnifiedItem = {
  rank: number;
  id: string | number;
  title: string;
  heat: number;
  url: string;
  mobileUrl: string;
  desc: string;
  timestamp: number | string | null;
};

function normalize(slug: string, meta: PlatformMeta, raw: any) {
  const list: any[] = Array.isArray(raw?.data) ? raw.data : [];
  const items: UnifiedItem[] = [];
  for (const it of list) {
    const title = String(it?.title ?? "").trim();
    if (!title) continue; // 空标题进库只会污染 topics 表
    items.push({
      rank: items.length + 1,
      id: it?.id ?? `${slug}-${items.length}`,
      title,
      heat: toHeat(it?.hot),
      url: String(it?.url ?? it?.mobileUrl ?? ""),
      mobileUrl: String(it?.mobileUrl ?? it?.url ?? ""),
      desc: String(it?.desc ?? ""),
      timestamp: it?.timestamp ?? null,
    });
  }
  return {
    platform: slug,
    name: meta.name,
    title: String(raw?.title ?? meta.name),
    type: String(raw?.type ?? meta.type ?? ""),
    description: String(raw?.description ?? ""),
    link: String(raw?.link ?? ""),
    updateTime: raw?.updateTime ?? null,
    fromCache: Boolean(raw?.fromCache),
    total: items.length,
    items,
  };
}

// ---------------------------------------------------------------- HTTP 入口

const app = new Hono();

app.get("/health", (c) =>
  c.json({
    ok: true,
    node: process.version,
    ref: REF_DIR,
    routeFile: fs.existsSync(DIST_ROUTES) ? "dist" : "src",
    platforms: PLATFORMS.length,
    missing: MISSING,
  }),
);

// 映射表本身也暴露出来，Python 侧或人排查时能直接核对
app.get("/platforms", (c) => c.json({ ok: true, data: PLATFORMS }));

/** 核心接口：一个平台一次请求，返回统一结构。 */
app.get("/hot/:platform", async (c) => {
  const slug = c.req.param("platform");
  const meta = BY_SLUG.get(slug);
  if (!meta) {
    return c.json(
      {
        ok: false,
        error: `平台 ${slug} 不在采集白名单里`,
        hint: `可用：${PLATFORMS.map((p) => p.slug).join(", ")}`,
      },
      404,
    );
  }

  // cache=false 走上游实时抓取；默认吃 DailyHotApi 自带的 60s 缓存
  const noCache = c.req.query("cache") === "false";
  const t0 = Date.now();
  try {
    const handler = await loadHandler(slug);
    const raw = await handler(c, noCache);
    const data = normalize(slug, meta, raw);
    const cost = Date.now() - t0;
    console.log(`[hotnode] ${slug} ${data.total} 条 ${cost}ms${data.fromCache ? " (cache)" : ""}`);
    if (data.total === 0) {
      return c.json(
        {
          ok: false,
          error: `${meta.name} 返回 0 条：上游可能改版或限流`,
          platform: slug,
          cost_ms: cost,
        },
        502,
      );
    }
    return c.json({ ok: true, data, cost_ms: cost });
  } catch (e: any) {
    const cost = Date.now() - t0;
    const msg = `${typeOf(e)}: ${e?.message ?? e}`;
    console.error(`[hotnode] ${slug} 失败 ${cost}ms ${msg}`);
    return c.json({ ok: false, error: msg, platform: slug, cost_ms: cost }, 502);
  }
});

/** 一次性拉全部平台，方便人肉排查；Python 的日常刷新走的是逐平台接口。 */
app.get("/hot", async (c) => {
  const noCache = c.req.query("cache") === "false";
  const t0 = Date.now();
  const results = await Promise.allSettled(
    PLATFORMS.map(async (meta) => {
      const handler = await loadHandler(meta.slug);
      const raw = await handler(c, noCache);
      return normalize(meta.slug, meta, raw);
    }),
  );
  const data: Record<string, any> = {};
  const failed: Record<string, string> = {};
  results.forEach((r, i) => {
    const slug = PLATFORMS[i].slug;
    if (r.status === "fulfilled") data[slug] = r.value;
    else failed[slug] = String(r.reason?.message ?? r.reason);
  });
  return c.json({ ok: true, data, failed, cost_ms: Date.now() - t0 });
});

function typeOf(e: any): string {
  return e?.constructor?.name || "Error";
}

app.notFound((c) => c.json({ ok: false, error: `没有这个接口：${c.req.path}` }, 404));

serve({ fetch: app.fetch, port: PORT, hostname: HOST }, (info) => {
  console.log(`[hotnode] 监听 http://${HOST}:${info.port}`);
  console.log(`[hotnode] 引用仓库 ${REF_DIR}`);
  console.log(`[hotnode] 采集平台 ${PLATFORMS.map((p) => p.slug).join(", ")}`);
});
