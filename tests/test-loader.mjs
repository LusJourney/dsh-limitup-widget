/**
 * test-loader.mjs — 纯 node 冒烟测试（无测试框架、无浏览器、无网络）。
 *
 * 证明：
 * 1. lib/client.js 是一个合法的 `window.__ModuleLoader__.load({ id, factory })`
 *    客户端模块，id 与 npm 包名一致；
 * 2. factory(require) 产出的 exports 有 apply / inject=['slots']；
 * 3. apply(fakeCtx) 会把样式注册在 ctx.effect 里，并在 shell.overlay 上
 *    register 一个 id = 'limitup-widget' 的 list 条目；
 * 4. 组件能渲染：初始“加载中”、带数据（大数字/题材/自选/过期横幅）、收起胶囊；
 * 5. 服务端字符串先经 esc() 再渲染：恶意 `<img onerror>` 不会以原始标签出现；
 * 6. effect 清理函数可安全调用（卸载清理路径不抛错）。
 *
 * 运行：node tests/test-loader.mjs
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');

/** 极简 DOM 元素（够样式注入与清理用）。 */
function makeElement(tagName) {
  return {
    tagName,
    dataset: {},
    textContent: '',
    parentNode: null,
    children: [],
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      const index = this.children.indexOf(child);
      if (index >= 0) this.children.splice(index, 1);
      child.parentNode = null;
      return child;
    },
    setAttribute() {},
  };
}

const head = makeElement('head');
const fakeDocument = {
  head,
  hidden: false,
  createElement: (tagName) => makeElement(tagName),
  querySelector: () => null,
  addEventListener() {},
  removeEventListener() {},
};

const storage = new Map();
const fakeWindow = {
  innerWidth: 1280,
  innerHeight: 800,
  localStorage: {
    getItem: (key) => (storage.has(key) ? storage.get(key) : null),
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  },
  open() {},
  addEventListener() {},
  removeEventListener() {},
};

/** 假 React：createElement 返回普通对象树；hooks 由各渲染阶段注入。 */
function makeReact(hooks) {
  return {
    createElement(type, props, ...children) {
      return { type, props: props || {}, children: flatten(children) };
    },
    useState: hooks.useState,
    useEffect: hooks.useEffect,
    useRef: hooks.useRef,
    useCallback: hooks.useCallback,
  };
}

function flatten(list) {
  const out = [];
  for (const item of list) {
    if (Array.isArray(item)) out.push(...flatten(item));
    else if (item !== null && item !== undefined && item !== false) out.push(item);
  }
  return out;
}

/** 遍历假 React 元素树，收集文本与全部字符串 attr（用于断言转义）。 */
function collectStrings(node, out = []) {
  if (node === null || node === undefined) return out;
  if (typeof node === 'string') {
    out.push(node);
    return out;
  }
  if (Array.isArray(node)) {
    for (const child of node) collectStrings(child, out);
    return out;
  }
  if (typeof node === 'object' && node.props) {
    for (const value of Object.values(node.props)) {
      if (typeof value === 'string') out.push(value);
    }
    out.push(String(node.type));
    collectStrings(node.children, out);
  }
  return out;
}

/** 载入 lib/client.js 并拿到模块导出。 */
const source = readFileSync(join(root, 'lib/client.js'), 'utf8');

function loadClientModule() {
  let registration = null;
  const sandbox = {
    window: fakeWindow,
    document: fakeDocument,
    console,
    Intl,
    Date,
    Math,
    JSON,
    Number,
    String,
    Array,
    Object,
    Promise,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
    isFinite,
    fetch: undefined,
  };
  sandbox.globalThis = sandbox;
  fakeWindow.__ModuleLoader__ = {
    load(value) {
      registration = value;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'lib/client.js' });
  assert.notEqual(registration, null, 'client.js 必须调用 window.__ModuleLoader__.load()');
  assert.equal(registration.id, 'dsh-limitup-widget', '模块 id 必须等于包名');
  assert.equal(typeof registration.factory, 'function', 'load() 必须带 factory');
  return { registration, sandbox };
}

const { registration } = loadClientModule();

// ---------------------------------------------------------------- 1) 导出面
const defaultHooks = {
  useState: (initial) => [typeof initial === 'function' ? initial() : initial, () => {}],
  useEffect: () => undefined,
  useRef: (value) => ({ current: value }),
  useCallback: (fn) => fn,
};
const clientExports = registration.factory((specifier) => {
  if (specifier === 'react') return makeReact(defaultHooks);
  throw new Error(`client.js require() 了未声明的模块: ${specifier}`);
});
assert.equal(typeof clientExports.apply, 'function', 'exports.apply 必须是函数');
assert.deepEqual(Array.from(clientExports.inject), ['slots'], "exports.inject 必须是 ['slots']");
console.log('[loader] exports: apply=function inject=', JSON.stringify(clientExports.inject));

// ------------------------------------------------- 2) apply 注册 shell.overlay
const registrations = [];
const effects = [];
const injectedSlots = [];
function makeFakeCtxSlotReact() {
  return makeReact(defaultHooks);
}
// apply() 内部只使用 ctx.effect / ctx.slots.inject / ctx.slots.register
function makeCtx(reactImpl) {
  return {
    react: reactImpl,
    effect(fn, label) {
      const dispose = fn();
      effects.push({ label, dispose });
      return dispose;
    },
    slots: {
      inject(slotName, install) {
        injectedSlots.push(slotName);
        return install();
      },
      register(options, component) {
        registrations.push({ options, component });
        return () => {};
      },
    },
  };
}
const ctx = makeCtx(null);
// 重新拿一份 exports（apply 不依赖先前渲染）——直接复用即可
clientExports.apply(ctx);
/** 取最近一次 apply 注册的 shell.overlay 组件（apply 现在还注册侧边栏恢复按钮）。 */
function latestOverlay() {
  return registrations.filter((entry) => entry.options && entry.options.name === 'shell.overlay').pop().component;
}
assert.ok(injectedSlots.includes('shell.overlay'), 'apply 必须 inject shell.overlay');
const overlay = registrations.find((entry) => entry.options.name === 'shell.overlay');
assert.ok(overlay, '必须注册 shell.overlay 条目');
assert.equal(overlay.options.id, 'limitup-widget', '条目 id 必须是 limitup-widget');
const restoreEntry = registrations.find((entry) => entry.options && entry.options.name === 'sidebar.footer.action');
assert.ok(restoreEntry, '必须注册 sidebar.footer.action 恢复入口');
assert.equal(restoreEntry.options.id, 'limitup-restore', '恢复入口 id 必须是 limitup-restore');
assert.equal(typeof restoreEntry.component, 'function', '恢复入口必须携带 React 组件');
assert.equal(typeof overlay.component, 'function', '条目必须携带 React 组件');
assert.ok(head.children.some((el) => el.dataset.plugin === 'dsh-limitup-widget'), '样式必须注入 <style data-plugin>');
assert.equal(effects.length, 1, 'client apply 必须只注册样式 effect');
assert.equal(effects[0].label, 'limitup-widget: styles', '样式 effect 必须带标签');
for (const effect of effects) assert.equal(typeof effect.dispose, 'function', `effect ${effect.label} 必须返回清理函数`);
console.log('[loader] apply -> shell.overlay id=limitup-widget, effects=', effects.map((e) => e.label).join(' | '));

// ------------------------------------------------- 3) 初始渲染（无数据）
const treeLoading = overlay.component({});
assert.equal(treeLoading.props['data-limitup-widget'], 'true', '根节点必须带 data-limitup-widget');
const loadingTexts = collectStrings(treeLoading).join('\n');
assert.ok(loadingTexts.includes('涨停直播'), '标题必须是「涨停直播」');
assert.ok(loadingTexts.includes('加载中'), '无数据时应显示加载中');
assert.ok(loadingTexts.includes('自选'), '必须有自选区（输入框）');
console.log('[loader] render(initial): ok, contains 涨停直播/加载中/自选');

// ------------------------------------------------- 4) 带数据渲染 + 转义
const MALICIOUS = '<img src=x onerror="alert(1)">';
const fixtureSnapshot = {
  ok: true,
  generated_at: '2026-11-05T14:56:10+08:00',
  refreshed_at: '2026-11-05T14:56:03+08:00',
  trade_date: '20261105',
  requested_date: '20261105',
  is_fallback: false,
  fallback_note: null,
  market_phase: 'trading',
  refreshing: false,
  stale: { is_stale: false, since: null, reason: null },
  stats: {
    limit_up_count: 64,
    broken_count: 33,
    limit_down_count: 2,
    broken_rate: 0.3402,
    max_streak: 4,
    prev_limit_up_premium: 5.101,
    prev_limit_up_count: 118,
  },
  themes: [
    { name: '光伏概念', count: 29, earliest_first_seal: '09:25:00', is_industry_fallback: false },
    { name: MALICIOUS, count: 3, earliest_first_seal: '10:02:11', is_industry_fallback: true },
  ],
  watchlist: [
    { code: '600519', name: '贵州茅台', price: 1440.4, pct: -0.42, is_limit_up: false, is_limit_down: false, in_limit_up_pool: false, streak: null, ok: true, error: null },
    { code: '002578', name: '闽发铝业', price: 9.9, pct: 10.01, is_limit_up: true, is_limit_down: false, in_limit_up_pool: true, streak: 4, ok: true, error: null },
  ],
};
const presetStates = [
  { right: 18, bottom: 18 }, // pos
  false, // collapsed
  false, // hidden
  false, // dragging
  fixtureSnapshot, // snapshot
  null, // error
  false, // refreshing
  '14:56:03', // lastRefresh
  ['600519', '002578'], // codes
  '600519,002578', // inputValue
];
let stateCursor = 0;
const presetHooks = {
  useState(initial) {
    const value = presetStates[stateCursor];
    stateCursor += 1;
    return [value === undefined ? (typeof initial === 'function' ? initial() : initial) : value, () => {}];
  },
  useEffect: () => undefined,
  useRef: (value) => ({ current: value }),
  useCallback: (fn) => fn,
};
const expandedComponent = (() => {
  stateCursor = 0;
  const exportsWithHooks = registration.factory((specifier) => {
    if (specifier === 'react') return makeReact(presetHooks);
    throw new Error(`client.js require() 了未声明的模块: ${specifier}`);
  });
  stateCursor = 0;
  const ctx2 = makeCtx(null);
  exportsWithHooks.apply(ctx2);
  return latestOverlay();
})();
const treeData = expandedComponent({});
const texts = collectStrings(treeData);
const joined = texts.join('\n');
assert.ok(joined.includes('涨停直播'), '带数据渲染仍要有标题');
assert.ok(joined.includes('64'), '应显示涨停数量 64');
assert.ok(joined.includes('33'), '应显示炸板数量 33');
assert.ok(joined.includes('涨停'), '涨停标签存在');
assert.ok(joined.includes('光伏概念'), '题材名应渲染');
assert.ok(joined.includes('×29'), '题材数量应渲染为 ×29');
assert.ok(joined.includes('昨涨停溢价'), '应有昨日涨停溢价指标');
assert.ok(joined.includes('✕'), '展开态头部应有「✕」关闭按钮');
assert.ok(joined.includes('▾'), '展开态头部应有「▾」收起按钮');
assert.ok(joined.includes('贵州茅台'), '自选名应渲染');
assert.ok(joined.includes('1440.40'), '自选价格应格式化为两位小数');
assert.ok(joined.includes('-0.42%'), '自选跌幅应带符号与百分号');
assert.ok(!joined.includes('<img'), '恶意 HTML 不得以原始标签出现（XSS 防护）');
assert.ok(joined.includes('&lt;img'), '服务端字符串必须先经 esc() 转义');
console.log('[loader] render(snapshot): stats/themes/watchlist ok, esc() applied (no raw <img>)');

// ------------------------------------------------- 5) 收起态胶囊渲染
stateCursor = 0;
presetStates[1] = true; // collapsed
const pillExports = registration.factory((specifier) => {
  if (specifier === 'react') return makeReact(presetHooks);
  throw new Error(`client.js require() 了未声明的模块: ${specifier}`);
});
stateCursor = 0;
const ctx3 = makeCtx(null);
pillExports.apply(ctx3);
const treePill = latestOverlay()({});
assert.equal(treePill.props['data-limitup-widget'], 'true', '收起态根节点也要带 data-limitup-widget');
assert.ok(String(treePill.props.className).includes('luw-pill'), '收起态应渲染小胶囊');
const pillText = collectStrings(treePill).join('\n');
assert.ok(pillText.includes('涨停'), '胶囊应显示涨停摘要');
console.log('[loader] render(collapsed): pill ok');

// ------------------------------------------------- 5b) 关闭态：浮层不渲染任何东西
presetStates[1] = false; // collapsed
presetStates[2] = true; // hidden
const hiddenExports = registration.factory((specifier) => {
  if (specifier === 'react') return makeReact(presetHooks);
  throw new Error(`client.js require() 了未声明的模块: ${specifier}`);
});
stateCursor = 0;
const ctxHidden = makeCtx(null);
hiddenExports.apply(ctxHidden);
const treeHidden = latestOverlay()({});
assert.equal(treeHidden, null, '关闭态：浮层必须什么都不渲染（不许残留小挂件）');
console.log('[loader] render(hidden): overlay renders null (no floating remnant)');
presetStates[2] = false; // hidden

// ------------------------------------------------- 6) 卸载清理不抛错
for (const effect of effects) effect.dispose();
console.log('[loader] dispose effects: ok');

console.log('[loader] PASS — lib/client.js loads, registers shell.overlay#limitup-widget, renders & escapes.');
