/**
 * test-node-plugin.mjs — Host 半（lib/index.js）的纯 node 测试（无测试框架、无外部网络）。
 *
 * 证明：
 * 1. apply(fakeCtx, config) 在 webServer 上注册 kind=prefix、path=/limitup-widget 的路由；
 * 2. 路由映射：/api/config 200、/ → 302 到后端页面、未知路径 404、非 GET 405；
 * 3. 后端不可达时 /api/snapshot 返回 502 JSON（backend_unreachable），不抛异常；
 * 4. planAutostart 决策矩阵（已在跑/未开自启/已 spawn/已占用/无脚本/要拉起）；
 * 5. startOrVerifyBackend 注入依赖下：探活成功不 spawn；探活失败会 spawn 且
 *    HOST/PORT/TZ/cwd 环境对齐；startupTimeout 内不健康 → timeout；autostart=false 不 spawn。
 *
 * 运行：node tests/test-node-plugin.mjs
 */
import assert from 'node:assert/strict';
import {
  apply,
  createProxyHandler,
  normalizeConfig,
  planAutostart,
  probeHealth,
  startOrVerifyBackend,
} from '../lib/index.js';

// ------------------------------------------------------------------ 假 ctx
function makeFakeCtx() {
  const routes = [];
  const effects = [];
  const injected = [];
  const webServer = {
    register(route) {
      routes.push(route);
      return () => {
        route.disposed = true;
      };
    },
  };
  const webCtx = {
    get(serviceName) {
      return serviceName === 'webServer' ? webServer : undefined;
    },
    effect(fn, label) {
      const dispose = fn();
      effects.push({ label, dispose });
      return dispose;
    },
  };
  const ctx = {
    inject(services, install) {
      injected.push(services);
      install(webCtx);
    },
    effect(fn, label) {
      const dispose = fn();
      effects.push({ label, dispose });
      return dispose;
    },
    get() {
      return undefined;
    },
  };
  return { ctx, routes, effects, injected };
}

/** 调用一次 node http handler，返回 {status, headers, body}。 */
function invoke(handler, options) {
  const method = options.method || 'GET';
  const url = options.url || '/';
  return new Promise((resolve, reject) => {
    const chunks = [];
    const response = {
      statusCode: 0,
      headers: {},
      writeHead(status, headers) {
        this.statusCode = status;
        Object.assign(this.headers, headers || {});
        return this;
      },
      setHeader(key, value) {
        this.headers[key] = value;
      },
      end(body) {
        if (body !== undefined && body !== null) chunks.push(Buffer.isBuffer(body) ? body : Buffer.from(String(body)));
        resolve({
          status: this.statusCode,
          headers: this.headers,
          body: Buffer.concat(chunks).toString('utf8'),
        });
      },
    };
    const request = { method, url, headers: {} };
    try {
      const result = handler(request, response);
      if (result && typeof result.catch === 'function') result.catch(reject);
    } catch (error) {
      reject(error);
    }
  });
}

const closedBackendConfig = normalizeConfig({
  backendUrl: 'http://127.0.0.1:18099',
  autostart: false,
  pythonBin: '/usr/bin/true',
  script: '/tmp/limitup-widget-fake-app.py',
  startupTimeoutMs: 3000,
  pollTradingMs: 60000,
  pollIdleMs: 300000,
});

// ------------------------------------------------------ 1) normalizeConfig
assert.equal(closedBackendConfig.backendUrl, 'http://127.0.0.1:18099');
assert.equal(closedBackendConfig.port, 18099);
assert.equal(closedBackendConfig.autostart, false);
assert.equal(closedBackendConfig.startupTimeoutMs, 3000);
const badConfig = normalizeConfig({ backendUrl: 'ftp://nope', autostart: 'x', startupTimeoutMs: -5 });
assert.equal(badConfig.backendUrl, 'http://127.0.0.1:8000', '非法 backendUrl 回退默认');
assert.equal(badConfig.autostart, true, 'autostart 只认显式 false');
assert.equal(badConfig.startupTimeoutMs, 1000, '过小的 startupTimeoutMs 抬到下限');
console.log('[node] normalizeConfig: ok');

// ------------------------------------------------------ 2) planAutostart 矩阵
assert.equal(planAutostart({ healthy: true, autostart: true, spawned: false, starting: false, scriptPresent: true }), 'respect');
assert.equal(planAutostart({ healthy: false, autostart: true, spawned: false, starting: false, scriptPresent: true }), 'spawn');
assert.equal(planAutostart({ healthy: false, autostart: false, spawned: false, starting: false, scriptPresent: true }), 'disabled');
assert.equal(planAutostart({ healthy: false, autostart: true, spawned: true, starting: false, scriptPresent: true }), 'skip');
assert.equal(planAutostart({ healthy: false, autostart: true, spawned: false, starting: true, scriptPresent: true }), 'busy');
assert.equal(planAutostart({ healthy: false, autostart: true, spawned: false, starting: false, scriptPresent: false }), 'no-script');
console.log('[node] planAutostart: respect/spawn/disabled/skip/busy/no-script ok');

// ------------------------------------ 3) apply：路由注册 + effect 清理
const fake = makeFakeCtx();
apply(fake.ctx, {
  backendUrl: 'http://127.0.0.1:18099',
  autostart: false,
  pythonBin: '/usr/bin/true',
  script: '/tmp/limitup-widget-fake-app.py',
  startupTimeoutMs: 2000,
});
assert.ok(
  fake.injected.some((services) => services.includes('webServer')),
  'apply 必须 inject webServer',
);
const route = fake.routes.find((item) => item.path === '/limitup-widget');
assert.ok(route, '必须注册 /limitup-widget 前缀路由');
assert.equal(route.kind, 'prefix');
assert.equal(typeof route.handler, 'function');
assert.ok(
  fake.effects.some((effect) => effect.label === 'limitup-widget: backend autostart'),
  'apply 必须用 ctx.effect 管 autostart 生命周期',
);
for (const effect of fake.effects) assert.equal(typeof effect.dispose, 'function');
console.log('[node] apply: registered prefix /limitup-widget + autostart effect');

// --------------------------------------------------- 4) 代理路径映射
const state = { child: null, spawned: false, starting: false, disposed: false, lastKickAt: 0 };
const handler = createProxyHandler(closedBackendConfig, state);

const configResponse = await invoke(handler, { url: '/limitup-widget/api/config' });
assert.equal(configResponse.status, 200);
const configBody = JSON.parse(configResponse.body);
assert.equal(configBody.backendUrl, 'http://127.0.0.1:18099');
assert.equal(configBody.backendPageUrl, '/limitup-widget/');
assert.equal(configBody.pollTradingMs, 60000);
assert.equal(configBody.pollIdleMs, 300000);
console.log('[node] GET /limitup-widget/api/config -> 200', JSON.stringify(configBody));

const redirectResponse = await invoke(handler, { url: '/limitup-widget/' });
assert.equal(redirectResponse.status, 302);
assert.equal(redirectResponse.headers.location, 'http://127.0.0.1:18099/');
console.log('[node] GET /limitup-widget/ -> 302', redirectResponse.headers.location);

const notFound = await invoke(handler, { url: '/limitup-widget/api/nope' });
assert.equal(notFound.status, 404);
assert.equal(JSON.parse(notFound.body).error, 'not_found');
console.log('[node] GET /limitup-widget/api/nope -> 404');

const badMethod = await invoke(handler, { method: 'POST', url: '/limitup-widget/api/health' });
assert.equal(badMethod.status, 405);
assert.equal(JSON.parse(badMethod.body).error, 'method_not_allowed');
console.log('[node] POST /limitup-widget/api/health -> 405');

// 18099 没有服务：代理失败必须变成 502 JSON，而不是抛异常/挂住。
const proxyFailure = await invoke(handler, { url: '/limitup-widget/api/snapshot?watch=600519' });
assert.equal(proxyFailure.status, 502);
const proxyFailureBody = JSON.parse(proxyFailure.body);
assert.equal(proxyFailureBody.ok, false);
assert.equal(proxyFailureBody.error, 'backend_unreachable');
assert.equal(proxyFailureBody.backendUrl, 'http://127.0.0.1:18099');
console.log('[node] GET /limitup-widget/api/snapshot (backend down) -> 502', printJson(proxyFailureBody));

function printJson(value) {
  return JSON.stringify({ ok: value.ok, error: value.error });
}

// ------------------------------------- 5) probeHealth（注入 fetch，不碰网络）
{
  const okProbe = await probeHealth('http://127.0.0.1:18090', {
    fetchFn: async () => ({ ok: true, json: async () => ({ ok: true, uptime_sec: 1 }) }),
  });
  assert.equal(okProbe.ok, true);
  const badProbe = await probeHealth('http://127.0.0.1:18090', {
    fetchFn: async () => {
      throw new Error('ECONNREFUSED');
    },
  });
  assert.equal(badProbe.ok, false);
  assert.match(badProbe.error, /ECONNREFUSED/);
  console.log('[node] probeHealth: ok/error paths ok');
}

// --------------------------------- 6) startOrVerifyBackend 决策/拉起/超时
function makeFakeSpawn() {
  const calls = [];
  const spawnFn = (command, args, options) => {
    const child = {
      pid: 424242,
      exitCode: null,
      killed: false,
      stdout: { on() {} },
      stderr: { on() {} },
      once() {},
      kill() {
        child.killed = true;
      },
    };
    calls.push({ command, args, options, child });
    return child;
  };
  return { calls, spawnFn };
}

function makeHealthFetch(sequence) {
  let index = 0;
  return async () => {
    const step = sequence[Math.min(index, sequence.length - 1)];
    index += 1;
    if (step === 'fail') throw new Error('ECONNREFUSED');
    return { ok: true, json: async () => ({ ok: true }) };
  };
}

// 6a) 后端已在运行 → 不 spawn
{
  const { calls, spawnFn } = makeFakeSpawn();
  const spawnState = { child: null, spawned: false, starting: false, disposed: false };
  const result = await startOrVerifyBackend(
    { ...closedBackendConfig, autostart: true, backendUrl: 'http://127.0.0.1:18090', port: 18090 },
    spawnState,
    { fetchFn: makeHealthFetch(['ok']), spawnFn, sleep: async () => {}, now: () => 0 },
  );
  assert.equal(result.status, 'respect');
  assert.equal(calls.length, 0, '后端在跑时不得 spawn');
  assert.equal(spawnState.spawned, false);
  console.log('[node] startOrVerifyBackend: healthy backend -> respect (no spawn)');
}

// 6b) 探活失败 → spawn 且环境/参数对齐，随后健康 → started
{
  const { calls, spawnFn } = makeFakeSpawn();
  const spawnState = { child: null, spawned: false, starting: false, disposed: false };
  let clock = 0;
  const result = await startOrVerifyBackend(
    {
      ...closedBackendConfig,
      autostart: true,
      backendUrl: 'http://127.0.0.1:18090',
      port: 18090,
      pythonBin: '/usr/bin/python-test',
      script: '/tmp/limitup-widget-demo/app.py',
      startupTimeoutMs: 5000,
    },
    spawnState,
    {
      fetchFn: makeHealthFetch(['fail', 'ok']),
      spawnFn,
      sleep: async () => {
        clock += 1000;
      },
      now: () => clock,
    },
  );
  assert.equal(result.status, 'started');
  assert.equal(calls.length, 1, '只允许拉起一次');
  const call = calls[0];
  assert.equal(call.command, '/usr/bin/python-test');
  assert.deepEqual(call.args, ['/tmp/limitup-widget-demo/app.py']);
  assert.equal(call.options.cwd, '/tmp/limitup-widget-demo');
  assert.equal(call.options.env.HOST, '127.0.0.1');
  assert.equal(call.options.env.PORT, '18090');
  assert.equal(call.options.env.TZ, 'Asia/Shanghai');
  assert.equal(spawnState.spawned, true);
  assert.equal(spawnState.starting, false, '结束后必须释放 starting 锁');
  console.log('[node] startOrVerifyBackend: spawn once with HOST/PORT/TZ/cwd aligned -> started');
}

// 6c) 持续不健康 → timeout；6d) autostart=false → disabled（不 spawn）
{
  const { calls, spawnFn } = makeFakeSpawn();
  const timeoutState = { child: null, spawned: false, starting: false, disposed: false };
  let clock = 0;
  const timeoutResult = await startOrVerifyBackend(
    { ...closedBackendConfig, autostart: true, backendUrl: 'http://127.0.0.1:18090', port: 18090, startupTimeoutMs: 3000 },
    timeoutState,
    {
      fetchFn: makeHealthFetch(['fail']),
      spawnFn,
      sleep: async () => {
        clock += 1000;
      },
      now: () => clock,
    },
  );
  assert.equal(timeoutResult.status, 'timeout');
  assert.equal(calls.length, 1);
  console.log('[node] startOrVerifyBackend: persistent failure -> timeout (1 spawn, bounded)');

  const disabled = makeFakeSpawn();
  const disabledState = { child: null, spawned: false, starting: false, disposed: false };
  const disabledResult = await startOrVerifyBackend(
    { ...closedBackendConfig, autostart: false, backendUrl: 'http://127.0.0.1:18090' },
    disabledState,
    { fetchFn: makeHealthFetch(['fail']), spawnFn: disabled.spawnFn, sleep: async () => {}, now: () => 0 },
  );
  assert.equal(disabledResult.status, 'disabled');
  assert.equal(disabled.calls.length, 0, 'autostart=false 不得 spawn');
  console.log('[node] startOrVerifyBackend: autostart=false -> disabled (no spawn)');
}

// ------------------------------------------------------- 7) 卸载清理
for (const effect of fake.effects) effect.dispose();
console.log('[node] dispose effects: ok');

console.log('[node] PASS — routes registered, proxy mapping, autostart decisions verified.');
