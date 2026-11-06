/**
 * dsh-limitup-widget — Host half.
 *
 * 两件事：
 * 1. 在 webServer 上挂一个同源前缀路由 `/limitup-widget`：
 *    - `GET /limitup-widget/api/{snapshot,refresh,health}` 反代到本地 Python 后端
 *      （客户端所有请求都走同源，避免 CORS / 混合内容问题）；
 *    - `GET /limitup-widget/api/config` 把轮询参数告诉浏览器小窗；
 *    - `GET /limitup-widget/` 302 到完整看板页面；
 *    - 其它路径 404 / 非 GET 405，后端不可达时返回 502 JSON。
 * 2. 启动时探活 `backendUrl/api/health`：后端没起且 `autostart` 打开时用
 *    `pythonBin script` 拉起（HOST/PORT/TZ 环境对齐），轮询到健康为止；
 *    只杀“本插件自己拉起的子进程”，绝不杀已在运行的后端；dispose 时清理。
 *
 * 本文件是纯 ESM Host 插件：`export function apply(ctx, config)`。
 */
import { spawn } from 'node:child_process';
import { dirname } from 'node:path';

/** Cordis 行名（与 cordis.patch.yml 的 id 一致）。 */
export const name = 'limitup-widget';

/** 默认配置；cordis.patch.yml 的 insert.config 会覆盖。 */
export const DEFAULT_CONFIG = Object.freeze({
  backendUrl: 'http://127.0.0.1:8000',
  autostart: true,
  pythonBin: 'python3',
  script: '',
  startupTimeoutMs: 45000,
  pollTradingMs: 60000,
  pollIdleMs: 300000,
});

/** 同源代理前缀（客户端只认这个前缀）。 */
const ROUTE_PREFIX = '/limitup-widget';
/** 各代理路径的超时（refresh 后端最多同步约 30s）。 */
const PROXY_TIMEOUT_MS = Object.freeze({
  '/api/snapshot': 12000,
  '/api/refresh': 45000,
  '/api/health': 6000,
});
const HEALTH_PROBE_TIMEOUT_MS = 4000;
/** 代理失败触发补拉后端的限频，避免每次 502 都 spawn。 */
const KICK_INTERVAL_MS = 30000;
/** 启动健康轮询间隔。 */
const STARTUP_POLL_MS = 1000;

/** 统一带前缀的日志（DSH 的 Host 日志里一眼可辨）。 */
function log(...args) {
  console.log('[limitup-widget]', ...args);
}

/** 错误对象转可读字符串。 */
function describe(error) {
  if (error === null || error === undefined) return 'unknown error';
  if (typeof error === 'string') return error;
  if (error instanceof Error) return error.message || String(error);
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

/** 读有界整数配置，非法值回退默认值。 */
function boundedInt(value, fallback, min, max) {
  const parsed = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(parsed)));
}

/** 规范化后端地址：只接受 http(s)，去掉尾部斜杠。 */
function normalizeBackendUrl(value) {
  if (typeof value !== 'string' || value.trim() === '') return null;
  try {
    const url = new URL(value.trim());
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    const path = url.pathname.replace(/\/+$/, '');
    return `${url.origin}${path}`;
  } catch {
    return null;
  }
}

/** 从 backendUrl 取端口（拉起后端时 PORT 必须对齐）。 */
function portOf(backendUrl) {
  try {
    const url = new URL(backendUrl);
    if (url.port !== '') return Number(url.port);
    return url.protocol === 'https:' ? 443 : 80;
  } catch {
    return 8000;
  }
}

/**
 * 合并默认配置并做边界校验。
 * @param {unknown} raw cordis.patch.yml 里的 config。
 * @returns 规范化后的配置对象。
 */
export function normalizeConfig(raw) {
  const input = raw !== null && typeof raw === 'object' ? raw : {};
  const backendUrl = normalizeBackendUrl(input.backendUrl) ?? DEFAULT_CONFIG.backendUrl;
  const pythonBin =
    typeof input.pythonBin === 'string' && input.pythonBin.trim() !== ''
      ? input.pythonBin.trim()
      : DEFAULT_CONFIG.pythonBin;
  return {
    backendUrl,
    port: portOf(backendUrl),
    autostart: input.autostart !== false,
    pythonBin,
    script: typeof input.script === 'string' ? input.script.trim() : DEFAULT_CONFIG.script,
    startupTimeoutMs: boundedInt(input.startupTimeoutMs, DEFAULT_CONFIG.startupTimeoutMs, 1000, 300000),
    pollTradingMs: boundedInt(input.pollTradingMs, DEFAULT_CONFIG.pollTradingMs, 15000, 600000),
    pollIdleMs: boundedInt(input.pollIdleMs, DEFAULT_CONFIG.pollIdleMs, 30000, 3600000),
  };
}

/**
 * 纯函数：根据健康检查结果与运行状态决定是否拉后端。
 * @param {{healthy: boolean, autostart: boolean, spawned: boolean, starting: boolean, scriptPresent: boolean}} input
 * @returns {'respect'|'busy'|'disabled'|'skip'|'no-script'|'spawn'}
 */
export function planAutostart(input) {
  if (input.healthy) return 'respect';
  if (input.starting) return 'busy';
  if (!input.autostart) return 'disabled';
  if (input.spawned) return 'skip';
  if (!input.scriptPresent) return 'no-script';
  return 'spawn';
}

/**
 * 探活后端 `/api/health`（不触发任何 akshare 调用）。
 * @param {string} backendUrl 后端根地址。
 * @param {{fetchFn?: typeof fetch, timeoutMs?: number}} [options]
 * @returns {Promise<{ok: boolean, data?: unknown, error?: string}>}
 */
export async function probeHealth(backendUrl, options = {}) {
  const fetchFn = options.fetchFn ?? globalThis.fetch;
  const timeoutMs = options.timeoutMs ?? HEALTH_PROBE_TIMEOUT_MS;
  const target = `${backendUrl.replace(/\/+$/, '')}/api/health`;
  try {
    const response = await fetchFn(target, {
      signal: AbortSignal.timeout(timeoutMs),
      headers: { accept: 'application/json' },
    });
    if (!response.ok) return { ok: false, error: `HTTP ${response.status}` };
    const data = await response.json().catch(() => null);
    return { ok: Boolean(data && data.ok === true), data };
  } catch (error) {
    return { ok: false, error: describe(error) };
  }
}

/**
 * 确保后端在跑：已在跑 → 什么都不做；没在跑且 autostart → 拉起并轮询健康。
 * 依赖可注入（fetch/spawn/sleep/now），便于纯 node 测试。
 * @param {ReturnType<typeof normalizeConfig>} config
 * @param {{child?: any, spawned?: boolean, starting?: boolean, disposed?: boolean}} state
 * @param {{fetchFn?: typeof fetch, spawnFn?: typeof spawn, sleep?: (ms: number) => Promise<void>, now?: () => number}} [deps]
 * @returns {Promise<{status: string, code?: number, error?: string}>}
 */
export async function startOrVerifyBackend(config, state, deps = {}) {
  const fetchFn = deps.fetchFn ?? globalThis.fetch;
  const spawnFn = deps.spawnFn ?? spawn;
  const sleep = deps.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const now = deps.now ?? Date.now;

  if (state.starting) return { status: 'busy' };
  state.starting = true;
  try {
    const health = await probeHealth(config.backendUrl, { fetchFn });
    if (state.disposed) return { status: 'disposed' };
    // starting 已由本函数占用，这里把决策输入固定为 false，避免误判 busy。
    const decision = planAutostart({
      healthy: health.ok,
      autostart: config.autostart,
      spawned: state.spawned === true,
      starting: false,
      scriptPresent: config.script !== '',
    });
    if (decision !== 'spawn') {
      log(
        decision === 'respect'
          ? `backend already running at ${config.backendUrl}`
          : `autostart skipped (${decision})`,
      );
      return { status: decision };
    }

    // 先记 spawned 再 spawn：任何异常路径都不会重复拉起。
    state.spawned = true;
    const env = {
      ...process.env,
      HOST: '127.0.0.1',
      PORT: String(config.port),
      TZ: 'Asia/Shanghai',
      PYTHONUNBUFFERED: '1',
    };
    let child;
    try {
      child = spawnFn(config.pythonBin, [config.script], {
        cwd: dirname(config.script),
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      state.spawned = false;
      log(`spawn failed: ${describe(error)}`);
      return { status: 'spawn-failed', error: describe(error) };
    }
    state.child = child;
    log(
      `starting backend (pid ${String(child.pid)}): ${config.pythonBin} ${config.script} ` +
        `[HOST=127.0.0.1 PORT=${config.port} TZ=Asia/Shanghai]`,
    );
    if (child.stdout && typeof child.stdout.on === 'function') {
      child.stdout.on('data', (chunk) => log(`backend: ${String(chunk).trimEnd()}`));
    }
    if (child.stderr && typeof child.stderr.on === 'function') {
      child.stderr.on('data', (chunk) => log(`backend! ${String(chunk).trimEnd()}`));
    }
    if (typeof child.once === 'function') {
      child.once('error', (error) => log(`backend process error: ${describe(error)}`));
      child.once('exit', (code, signal) => log(`backend process exited: code=${String(code)} signal=${String(signal)}`));
    }

    const deadline = now() + config.startupTimeoutMs;
    while (!state.disposed && now() < deadline) {
      await sleep(STARTUP_POLL_MS);
      if (state.disposed) break;
      if (typeof child.exitCode === 'number') {
        log(`backend exited before becoming healthy (code ${child.exitCode})`);
        return { status: 'exited', code: child.exitCode };
      }
      const probe = await probeHealth(config.backendUrl, { fetchFn });
      if (probe.ok) {
        log(`backend is healthy at ${config.backendUrl}`);
        return { status: 'started' };
      }
    }
    if (state.disposed) return { status: 'disposed' };
    log(`backend did not become healthy within ${config.startupTimeoutMs}ms`);
    return { status: 'timeout' };
  } finally {
    state.starting = false;
  }
}

/** 只终止本插件拉起的子进程（没拉起过 → 什么都不做）。 */
function stopChild(state) {
  const child = state.child;
  state.child = null;
  if (!child || typeof child.kill !== 'function') return;
  try {
    log(`stopping backend process spawned by this plugin (pid ${String(child.pid)})`);
    child.kill('SIGTERM');
  } catch (error) {
    log(`failed to stop backend process: ${describe(error)}`);
  }
}

/** 代理请求失败时补一次“确保后端在跑”，带限频。 */
function kickBackend(config, state) {
  if (!config.autostart || state.disposed || state.spawned || state.starting) return;
  const now = Date.now();
  if (now - (state.lastKickAt ?? 0) < KICK_INTERVAL_MS) return;
  state.lastKickAt = now;
  log('backend unreachable — trying to start it');
  startOrVerifyBackend(config, state).catch((error) => log(`heal attempt failed: ${describe(error)}`));
}

/** 发送 JSON 响应（UTF-8、no-store）。 */
function sendJson(response, status, value, extraHeaders = {}) {
  const body = Buffer.from(JSON.stringify(value), 'utf8');
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': body.length,
    'cache-control': 'no-store',
    ...extraHeaders,
  });
  response.end(body);
}

/** 把后端响应原样转发（保留 status/content-type）。 */
async function forwardToBackend(config, state, subPath, search, method, response) {
  const timeout = PROXY_TIMEOUT_MS[subPath];
  const target = `${config.backendUrl}${subPath}${search || ''}`;
  let backendResponse;
  try {
    backendResponse = await fetch(target, {
      signal: AbortSignal.timeout(timeout),
      headers: { accept: 'application/json' },
    });
  } catch (error) {
    kickBackend(config, state);
    const detail = describe(error);
    log(`proxy ${subPath} failed: ${detail}`);
    return sendJson(response, 502, {
      ok: false,
      error: 'backend_unreachable',
      backendUrl: config.backendUrl,
      detail,
    });
  }
  const body = Buffer.from(await backendResponse.arrayBuffer());
  const contentType = backendResponse.headers.get('content-type') || 'application/json; charset=utf-8';
  response.writeHead(backendResponse.status, {
    'content-type': contentType,
    'content-length': body.length,
    'cache-control': 'no-store',
  });
  if (method === 'HEAD') response.end();
  else response.end(body);
}

/**
 * 前缀路由的完整请求处理（导出以便测试直接调用）。
 * @param {ReturnType<typeof normalizeConfig>} config
 * @param {object} state
 * @param {import('node:http').IncomingMessage} request
 * @param {import('node:http').ServerResponse} response
 */
async function handleRequest(config, state, request, response) {
  const method = request.method ?? 'GET';
  const url = new URL(request.url ?? '/', 'http://127.0.0.1');
  const pathname = url.pathname;

  // 完整看板：302 到后端自己的页面（同源代理之外的唯一跳转）。
  if (pathname === ROUTE_PREFIX || pathname === `${ROUTE_PREFIX}/`) {
    if (method !== 'GET' && method !== 'HEAD') {
      return sendJson(response, 405, { ok: false, error: 'method_not_allowed' }, { allow: 'GET, HEAD' });
    }
    response.writeHead(302, { location: `${config.backendUrl}/`, 'cache-control': 'no-store' });
    response.end();
    return;
  }

  const subPath = pathname.slice(ROUTE_PREFIX.length);

  // 客户端轮询参数（由 Host 配置统一提供，客户端不硬编码）。
  if (subPath === '/api/config') {
    if (method !== 'GET' && method !== 'HEAD') {
      return sendJson(response, 405, { ok: false, error: 'method_not_allowed' }, { allow: 'GET, HEAD' });
    }
    return sendJson(response, 200, {
      ok: true,
      backendUrl: config.backendUrl,
      backendPageUrl: `${ROUTE_PREFIX}/`,
      pollTradingMs: config.pollTradingMs,
      pollIdleMs: config.pollIdleMs,
      autostart: config.autostart,
    });
  }

  if (PROXY_TIMEOUT_MS[subPath] === undefined) {
    return sendJson(response, 404, { ok: false, error: 'not_found', path: pathname });
  }
  if (method !== 'GET' && method !== 'HEAD') {
    return sendJson(response, 405, { ok: false, error: 'method_not_allowed' }, { allow: 'GET, HEAD' });
  }
  return forwardToBackend(config, state, subPath, url.search, method, response);
}

/**
 * 生成前缀路由 handler（node http 签名，可被 webServer 直接注册）。
 * @param {ReturnType<typeof normalizeConfig>} config
 * @param {object} state
 */
export function createProxyHandler(config, state) {
  return function handler(request, response) {
    handleRequest(config, state, request, response).catch((error) => {
      log(`request failed: ${describe(error)}`);
      try {
        sendJson(response, 500, { ok: false, error: 'internal_error', detail: describe(error) });
      } catch {
        /* response already gone */
      }
    });
  };
}

/**
 * Host 插件入口。
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {unknown} config
 */
export function apply(ctx, config) {
  const settings = normalizeConfig(config);
  const state = {
    child: null,
    spawned: false,
    starting: false,
    disposed: false,
    lastKickAt: 0,
  };
  log(`host plugin loaded; backend=${settings.backendUrl} autostart=${String(settings.autostart)}`);

  // 同源代理路由（webServer 可能晚于本插件就绪，用 ctx.inject 等待）。
  ctx.inject(['webServer'], (webCtx) => {
    const server = webCtx.get('webServer');
    webCtx.effect(
      () =>
        server.register({
          kind: 'prefix',
          path: ROUTE_PREFIX,
          handler: createProxyHandler(settings, state),
        }),
      'limitup-widget: http routes',
    );
  });

  // 后端探活 + 自动拉起；dispose 时只清理本插件拉起的子进程。
  ctx.effect(() => {
    state.disposed = false;
    startOrVerifyBackend(settings, state).catch((error) => log(`autostart failed: ${describe(error)}`));
    return () => {
      state.disposed = true;
      stopChild(state);
    };
  }, 'limitup-widget: backend autostart');
}
