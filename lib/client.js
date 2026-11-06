/**
 * dsh-limitup-widget — Client half（手写、无构建、无额外依赖）。
 *
 * 浏览器模块系统按 `window.__ModuleLoader__.load({ id, factory })` 装载本文件；
 * id 必须等于 npm 包名（dsh-limitup-widget）。React 由宿主模块表提供，只
 * require('react')，其余全部自带。
 *
 * 行为：
 * - 在 `shell.overlay`（frame 级浮动层）里注册 id 为 `limitup-widget` 的卡片；
 * - 卡片可拖动（右下角锚点，位置存 localStorage `limitup.widget.pos`）、可收起
 *   （`limitup.widget.collapsed`）、可关闭（✕：浮层内完全移除，恢复入口在左侧边栏底部，
 *   仅在关闭时出现的「涨停」小按钮；关闭状态只在本次页面生命周期内有效）；
 *   胶囊整颗可点击展开（拖动与点击按 4px 位移区分）；
 * - 数据只从同源 `/limitup-widget/api/*`（Host 插件代理）取，绝不复用宿主 DOM；
 * - 60s/300s 轮询（由 Host 的 /api/config 提供间隔），页面隐藏时暂停；
 * - 所有服务端字符串经 esc() 转义后再渲染（React 本身也会转义）；
 * - 定时器 / visibility 监听 / 样式标签全部在 useEffect 与 ctx.effect 中清理。
 */
window.__ModuleLoader__.load({
  id: 'dsh-limitup-widget',
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    var React = require('react');

    var PLUGIN_ID = 'dsh-limitup-widget';
    var API_BASE = '/limitup-widget/api';
    var PAGE_URL = '/limitup-widget/';
    /** 桌面端 dsh-app:// 协议下相对地址会被静默拒绝，必须用绝对 http 地址；
     *  真实后端地址由 Host 的 /api/config 返回（backendUrl）。 */
    var DEFAULT_BACKEND_URL = 'http://127.0.0.1:8000';
    var STORAGE_POS = 'limitup.widget.pos';
    var STORAGE_COLLAPSED = 'limitup.widget.collapsed';
    var STORAGE_WATCH = 'limitup.watchlist';
    var DEFAULT_POS = { right: 18, bottom: 18 };
    var DEFAULT_POLL_TRADING_MS = 60000;
    var DEFAULT_POLL_IDLE_MS = 300000;
    var COLORS = {
      bg: '#0d1117',
      card: '#161b22',
      border: '#30363d',
      text: '#e6edf3',
      dim: '#8b949e',
      red: '#ff4d4f',
      green: '#00b578',
      amber: '#ffa940',
    };

    var CSS = [
      '.luw-root{position:absolute;z-index:60;pointer-events:auto;user-select:none;',
      'font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:#e6edf3}',
      '.luw-card{width:328px;max-width:340px;background:#0d1117;border:1px solid #30363d;border-radius:12px;',
      'box-shadow:0 12px 32px rgba(0,0,0,.55);overflow:hidden}',
      '.luw-header{display:flex;align-items:center;gap:6px;padding:7px 10px;background:#161b22;',
      'border-bottom:1px solid #30363d;cursor:grab}',
      '.luw-dragging .luw-header{cursor:grabbing}',
      '.luw-dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:#8b949e}',
      '.luw-title{font-size:13px;font-weight:600;letter-spacing:.2px;white-space:nowrap}',
      '.luw-date{font-size:11px;color:#8b949e;white-space:nowrap}',
      '.luw-status{font-size:11px;white-space:nowrap}',
      '.luw-tag{font-size:10px;line-height:14px;padding:0 4px;border-radius:4px;background:#21262d;',
      'color:#8b949e;border:1px solid #30363d;white-space:nowrap}',
      '.luw-tag-warn{background:rgba(255,169,64,.16);color:#ffa940;border-color:rgba(255,169,64,.4)}',
      '.luw-spacer{flex:1 1 auto}',
      '.luw-icon-btn{appearance:none;background:transparent;border:none;color:#8b949e;font-size:12px;',
      'line-height:1;padding:3px 5px;border-radius:6px;cursor:pointer}',
      '.luw-icon-btn:hover{background:#21262d;color:#e6edf3}',
      '.luw-body{padding:8px 10px 6px;max-height:min(68vh,560px);overflow-y:auto;overflow-x:hidden}',
      '.luw-banner{margin:0 0 7px;padding:5px 7px;border-radius:6px;background:rgba(255,77,79,.14);',
      'border:1px solid rgba(255,77,79,.4);color:#ff7875;font-size:11px;line-height:1.4}',
      '.luw-banner-info{background:rgba(88,166,255,.12);border:1px solid rgba(88,166,255,.4);color:#79c0ff}',
      '.luw-error{display:flex;align-items:center;gap:6px;margin:0 0 7px;padding:5px 7px;border-radius:6px;',
      'background:rgba(255,77,79,.1);border:1px solid rgba(255,77,79,.35);color:#ff7875;font-size:11px}',
      '.luw-error-text{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.luw-loading,.luw-empty{padding:10px 0;text-align:center;color:#8b949e;font-size:12px}',
      '.luw-stats{display:flex;gap:6px;margin:0 0 6px}',
      '.luw-stat{flex:1 1 0;background:#161b22;border:1px solid #21262d;border-radius:8px;padding:5px 6px;text-align:center}',
      '.luw-stat-value{font-size:22px;line-height:1.15;font-weight:700;font-variant-numeric:tabular-nums}',
      '.luw-stat-label{font-size:11px;color:#8b949e;margin-top:1px}',
      '.luw-substats{display:flex;gap:6px;margin:0 0 8px}',
      '.luw-substat{flex:1 1 0;display:flex;justify-content:space-between;gap:4px;background:#0f141b;',
      'border:1px solid #21262d;border-radius:6px;padding:3px 6px;font-size:11px;color:#8b949e;white-space:nowrap}',
      '.luw-substat-value{color:#e6edf3;font-variant-numeric:tabular-nums;font-weight:600}',
      '.luw-section{margin:0 0 8px}',
      '.luw-section-title{display:flex;align-items:center;justify-content:space-between;font-size:11px;',
      'color:#8b949e;margin:0 0 3px;letter-spacing:.4px}',
      '.luw-theme-row{display:flex;align-items:center;gap:6px;padding:2px 4px;border-radius:5px;font-size:12px}',
      '.luw-theme-row:nth-child(odd){background:rgba(255,255,255,.02)}',
      '.luw-theme-name{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.luw-theme-count{color:#ff4d4f;font-weight:600;font-variant-numeric:tabular-nums}',
      '.luw-theme-early{color:#8b949e;font-size:11px;font-variant-numeric:tabular-nums;white-space:nowrap}',
      '.luw-watch-row{display:flex;align-items:center;gap:5px;padding:2px 4px;border-radius:5px;font-size:12px}',
      '.luw-watch-row:nth-child(odd){background:rgba(255,255,255,.02)}',
      '.luw-watch-name{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.luw-watch-code{color:#8b949e;font-size:10px;margin-left:4px;font-variant-numeric:tabular-nums}',
      '.luw-watch-price{font-variant-numeric:tabular-nums}',
      '.luw-watch-pct{width:62px;text-align:right;font-variant-numeric:tabular-nums}',
      '.luw-watch-error{color:#ff7875;font-size:10px}',
      '.luw-badge-limit{flex:0 0 auto;background:#ff4d4f;color:#fff;font-size:10px;line-height:14px;',
      'padding:0 4px;border-radius:3px;font-weight:600}',
      '.luw-watch-empty{color:#8b949e;font-size:11px;padding:2px 4px 4px}',
      '.luw-watch-input-row{display:flex;gap:4px;margin-top:4px}',
      '.luw-watch-input{flex:1 1 auto;min-width:0;background:#0d1117;border:1px solid #30363d;border-radius:6px;',
      'color:#e6edf3;font-size:11px;padding:3px 6px;outline:none;font-family:inherit}',
      '.luw-watch-input:focus{border-color:#58a6ff}',
      '.luw-btn{appearance:none;border:1px solid #30363d;background:#21262d;color:#e6edf3;border-radius:6px;',
      'cursor:pointer;font-family:inherit;font-size:11px;line-height:1.4;padding:3px 7px;white-space:nowrap}',
      '.luw-btn:hover:not(:disabled){background:#30363d}',
      '.luw-btn:disabled{opacity:.55;cursor:default}',
      '.luw-btn-small{font-size:11px;padding:2px 6px}',
      '.luw-btn-ghost{background:transparent}',
      '.luw-footer{display:flex;align-items:center;gap:5px;padding:6px 10px;border-top:1px solid #21262d;background:#0f141b}',
      '.luw-footer-time{font-size:11px;color:#8b949e;font-variant-numeric:tabular-nums;white-space:nowrap}',
      '.luw-pill{display:flex;align-items:center;gap:6px;padding:5px 8px 5px 10px;background:#161b22;',
      'border:1px solid #30363d;border-radius:999px;box-shadow:0 8px 22px rgba(0,0,0,.5);cursor:pointer;',
      'color:#e6edf3;font-size:12px}',
      '.luw-pill:hover{background:#1c2430;border-color:#3d4a5c}',
      '.luw-pill .luw-dot{width:7px;height:7px}',
      '.luw-pill-text{font-variant-numeric:tabular-nums;white-space:nowrap}',
      '.luw-pill-stale{color:#ff7875}',
      '.luw-pill-expand{appearance:none;border:1px solid #30363d;background:#21262d;color:#e6edf3;',
      'border-radius:999px;cursor:pointer;font-family:inherit;font-size:11px;line-height:1.4;',
      'padding:1px 8px;margin-left:2px;white-space:nowrap}',
      '.luw-pill-expand:hover{background:#30363d}',
      '.luw-close-btn:hover{background:rgba(255,77,79,.18);color:#ff7875}',
      '.luw-restore-btn{display:flex;align-items:center;gap:5px;padding:3px 8px;background:transparent;',
      'border:1px solid #30363d;border-radius:6px;color:#8b949e;cursor:pointer;font-family:inherit;',
      'font-size:12px;line-height:1.4;white-space:nowrap;appearance:none}',
      '.luw-restore-btn:hover{background:#21262d;color:#e6edf3}',
      '.luw-restore-btn .luw-dot{width:6px;height:6px}',
      '.luw-restore-text{font-variant-numeric:tabular-nums}',
    ].join('');

    // ---------------------------------------------------------------- 基础工具

    /** 注入插件样式（卸载时由 apply 的 effect 移除）。 */
    function injectStyles() {
      if (typeof document === 'undefined' || document.head === undefined) return null;
      var existing = document.querySelector('style[data-plugin="' + PLUGIN_ID + '"]');
      if (existing !== null) {
        if (existing.parentNode) existing.parentNode.removeChild(existing);
      }
      var tag = document.createElement('style');
      tag.dataset.plugin = PLUGIN_ID;
      tag.textContent = CSS;
      document.head.appendChild(tag);
      return tag;
    }

    /** HTML 转义：服务端字符串在进入渲染前必须经过这里（React 亦会转义）。 */
    function esc(value) {
      if (value === null || value === undefined) return '';
      return String(value).replace(/[&<>"]/g, function (ch) {
        return ch === '&' ? '&amp;' : ch === '<' ? '&lt;' : ch === '>' ? '&gt;' : '&quot;';
      });
    }

    /** 宽松数字：number 或数字字符串，否则 null。 */
    function num(value) {
      if (typeof value === 'number' && isFinite(value)) return value;
      if (typeof value === 'string' && /^-?\d+(\.\d+)?$/.test(value.trim())) return Number(value.trim());
      return null;
    }

    /** 数字展示；缺失显示 em dash。 */
    function fmtNum(value) {
      var n = num(value);
      return n === null ? '—' : String(Math.round(n));
    }

    /** 固定小数位展示。 */
    function fmtFixed(value, digits) {
      var n = num(value);
      return n === null ? '—' : n.toFixed(digits);
    }

    /** 带符号百分比（涨红跌绿由调用方着色）。 */
    function fmtPct(value) {
      var n = num(value);
      if (n === null) return '—';
      return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
    }

    /** 涨跌颜色：涨红、跌绿、平灰。 */
    function pctColor(value) {
      var n = num(value);
      if (n === null) return COLORS.text;
      if (n > 0) return COLORS.red;
      if (n < 0) return COLORS.green;
      return COLORS.dim;
    }

    /** ISO8601 → HH:MM（后端时间已是北京时间 +08:00，直接切字符串）。 */
    function hhmm(iso) {
      if (typeof iso !== 'string') return null;
      var match = /T(\d{2}):(\d{2})/.exec(iso);
      return match === null ? null : match[1] + ':' + match[2];
    }

    /** ISO8601 → HH:MM:SS。 */
    function hms(iso) {
      if (typeof iso !== 'string') return null;
      var match = /T(\d{2}:\d{2}:\d{2})/.exec(iso);
      return match === null ? null : match[1];
    }

    /** 当前北京时间 HH:MM:SS。 */
    function localHms() {
      try {
        return new Intl.DateTimeFormat('en-GB', {
          timeZone: 'Asia/Shanghai',
          hour12: false,
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        }).format(new Date());
      } catch (error) {
        return null;
      }
    }

    /** 当前北京时间的小时/分钟与星期（用于轮询节奏）。 */
    function beijingClock() {
      try {
        var parts = new Intl.DateTimeFormat('en-GB', {
          timeZone: 'Asia/Shanghai',
          hour12: false,
          weekday: 'short',
          hour: '2-digit',
          minute: '2-digit',
        }).formatToParts(new Date());
        var map = {};
        for (var i = 0; i < parts.length; i += 1) map[parts[i].type] = parts[i].value;
        var hour = Number.parseInt(map.hour, 10);
        var minute = Number.parseInt(map.minute, 10);
        if (!isFinite(hour) || !isFinite(minute)) return null;
        return { weekday: map.weekday, minutes: hour * 60 + minute };
      } catch (error) {
        return null;
      }
    }

    /** 是否 A 股交易时段（09:25–11:30、13:00–15:00，周末不算）。 */
    function isBeijingTradingTime() {
      var clock = beijingClock();
      if (clock === null) return false;
      if (clock.weekday === 'Sat' || clock.weekday === 'Sun') return false;
      var minutes = clock.minutes;
      return (minutes >= 9 * 60 + 25 && minutes <= 11 * 60 + 30) || (minutes >= 13 * 60 && minutes <= 15 * 60);
    }

    /** 20261105 → 11-05。 */
    function formatTradeDate(value) {
      if (typeof value !== 'string' || /^\d{8}$/.test(value) === false) return null;
      return value.slice(4, 6) + '-' + value.slice(6, 8);
    }

    /** 20261105 → 2026-11-05（title 用）。 */
    function formatTradeDateFull(value) {
      if (typeof value !== 'string' || /^\d{8}$/.test(value) === false) return null;
      return value.slice(0, 4) + '-' + value.slice(4, 6) + '-' + value.slice(6, 8);
    }

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value));
    }

    // ------------------------------------------------------------ 本地存储

    function readStorage(key) {
      try {
        if (typeof window === 'undefined' || window.localStorage === undefined) return null;
        return window.localStorage.getItem(key);
      } catch (error) {
        return null;
      }
    }

    function writeStorage(key, value) {
      try {
        if (typeof window === 'undefined' || window.localStorage === undefined) return;
        window.localStorage.setItem(key, value);
      } catch (error) {
        /* 隐私模式/配额满时静默降级 */
      }
    }

    /** 读取拖动位置（右下角锚点 px）。 */
    function readPos() {
      var raw = readStorage(STORAGE_POS);
      if (raw === null || raw === '') return null;
      try {
        var parsed = JSON.parse(raw);
        var right = num(parsed.right);
        var bottom = num(parsed.bottom);
        if (right === null || bottom === null) return null;
        return { right: Math.max(0, right), bottom: Math.max(0, bottom) };
      } catch (error) {
        return null;
      }
    }

    function readCollapsed() {
      return readStorage(STORAGE_COLLAPSED) === '1';
    }

    // ---- 关闭/恢复共享状态：浮窗与「侧边栏恢复按钮」跨 slot 同步。
    // 只在页面生命周期内有效：刷新/重启 DSH 后看板会重新出现，不会「永久关没」。
    var visibility = { hidden: false, listeners: [] };

    function getHidden() {
      return visibility.hidden === true;
    }

    function setHiddenShared(next) {
      var value = next === true;
      if (visibility.hidden === value) return;
      visibility.hidden = value;
      var listeners = visibility.listeners.slice();
      for (var i = 0; i < listeners.length; i++) {
        try {
          listeners[i](value);
        } catch (error) {
          /* 单个订阅者异常不影响其它组件 */
        }
      }
    }

    function subscribeHidden(listener) {
      if (typeof listener !== 'function') return function () {};
      visibility.listeners.push(listener);
      return function () {
        var index = visibility.listeners.indexOf(listener);
        if (index >= 0) visibility.listeners.splice(index, 1);
      };
    }

    /** 逗号/空格/分号分隔的代码串 → 归一化 6 位代码数组。 */
    function normalizeCodes(text) {
      if (typeof text !== 'string') return [];
      var seen = {};
      var codes = [];
      var parts = text.split(/[,，;；\s]+/);
      for (var i = 0; i < parts.length; i += 1) {
        var raw = parts[i].trim().replace(/^(sh|sz|bj)/i, '');
        if (/^\d{6}$/.test(raw) && seen[raw] !== true) {
          seen[raw] = true;
          codes.push(raw);
        }
      }
      return codes;
    }

    function readCodes() {
      return normalizeCodes(readStorage(STORAGE_WATCH) || '');
    }

    // ------------------------------------------------------------- 数据请求

    /** 行情快照（轮询用；同源代理）。 */
    function requestSnapshot(codes) {
      var query = codes.length > 0 ? '?watch=' + encodeURIComponent(codes.join(',')) : '';
      if (typeof fetch !== 'function') return Promise.reject(new Error('fetch unavailable'));
      return fetch(API_BASE + '/snapshot' + query, {
        cache: 'no-store',
        headers: { accept: 'application/json' },
      }).then(function (response) {
        return response
          .json()
          .catch(function () {
            return null;
          })
          .then(function (body) {
            return { response: response, body: body };
          });
      });
    }

    /** 强制后端刷新（手动刷新用；可能耗时约 30s）。 */
    function requestRefresh(codes) {
      var query = codes.length > 0 ? '?watch=' + encodeURIComponent(codes.join(',')) : '';
      if (typeof fetch !== 'function') return Promise.reject(new Error('fetch unavailable'));
      return fetch(API_BASE + '/refresh' + query, {
        cache: 'no-store',
        headers: { accept: 'application/json' },
      }).then(function (response) {
        return response
          .json()
          .catch(function () {
            return null;
          })
          .then(function (body) {
            return { response: response, body: body };
          });
      });
    }

    /** 从 {response, body} 里取快照，失败抛中文可读错误。 */
    function unwrapSnapshot(result) {
      if (!result.response.ok || result.body === null || result.body === undefined) {
        var statusText = 'HTTP ' + result.response.status;
        if (result.body && typeof result.body.error === 'string') throw new Error(result.body.error);
        throw new Error(statusText);
      }
      if (result.body.ok === false) {
        throw new Error(typeof result.body.error === 'string' ? result.body.error : '后端返回错误');
      }
      return result.body;
    }

    /** 状态点：盘中/收盘/过期/未连接。 */
    function statusOf(snapshot, error, stale) {
      if (stale) return { text: '过期', color: COLORS.red };
      if (snapshot === null || snapshot === undefined) {
        return error ? { text: '未连接', color: COLORS.red } : { text: '连接中', color: COLORS.dim };
      }
      var phase = snapshot.market_phase;
      if (phase === 'trading' || phase === 'auction') return { text: '盘中', color: COLORS.green };
      if (phase === 'lunch') return { text: '午休', color: COLORS.dim };
      if (phase === 'pre_open') return { text: '盘前', color: COLORS.dim };
      if (phase === 'non_trading_day') return { text: '休市', color: COLORS.dim };
      return { text: '收盘', color: COLORS.dim };
    }

    // ------------------------------------------------------------ 可视化组件

    /** 小按钮：点击阻止冒泡（避免触发拖动/收起）。 */
    function actionButton(label, onClick, props) {
      var extra = props || {};
      return React.createElement(
        'button',
        {
          type: 'button',
          className: 'luw-btn ' + (extra.className || ''),
          disabled: extra.disabled === true,
          title: extra.title || '',
          'aria-label': extra['aria-label'] || label,
          onClick: function (event) {
            if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
            if (extra.disabled !== true) onClick();
          },
        },
        label,
      );
    }

    /** 三块大数字中的一块。 */
    function statBlock(label, value, color) {
      return React.createElement(
        'div',
        { className: 'luw-stat', key: 'stat-' + label },
        React.createElement('div', { className: 'luw-stat-value', style: { color: color } }, value),
        React.createElement('div', { className: 'luw-stat-label' }, label),
      );
    }

    /** 次级指标（炸板率 / 连板高度 / 昨日溢价）。 */
    function subStat(label, value, color) {
      return React.createElement(
        'div',
        { className: 'luw-substat', key: 'sub-' + label },
        React.createElement('span', null, label),
        React.createElement('span', { className: 'luw-substat-value', style: { color: color } }, value),
      );
    }

    /** 自选行：名称/代码/价格/涨跌幅；涨停红底徽章；失败显示“失败”。 */
    function watchRow(row, index) {
      var code = esc(row && row.code ? row.code : '');
      var name = esc(row && row.name ? row.name : code).slice(0, 24);
      var price = num(row && row.price);
      var pct = num(row && row.pct);
      var isLimitUp = Boolean(row && row.is_limit_up);
      var failed = Boolean(row && row.ok === false);
      var key = 'watch-' + (code || String(index));
      var color = pctColor(pct);
      return React.createElement(
        'div',
        { className: 'luw-watch-row', key: key, title: code },
        isLimitUp ? React.createElement('span', { className: 'luw-badge-limit' }, '涨停') : null,
        React.createElement(
          'span',
          { className: 'luw-watch-name' },
          name,
          React.createElement('span', { className: 'luw-watch-code' }, code),
        ),
        React.createElement('span', { className: 'luw-watch-price', style: { color: color } }, price === null ? '—' : price.toFixed(2)),
        React.createElement(
          'span',
          { className: 'luw-watch-pct', style: { color: color } },
          pct === null ? '—' : fmtPct(pct),
        ),
        failed ? React.createElement('span', { className: 'luw-watch-error' }, '失败') : null,
      );
    }

    /**
     * 浮动小窗本体。
     * @param {object} props 宿主注入的 slot props（本组件不使用，保持占位兼容）。
     */
    function Widget(props) {
      void props;
      var useState = React.useState;
      var useEffect = React.useEffect;
      var useRef = React.useRef;
      var useCallback = React.useCallback;

      var initialPos = readPos();
      var posState = useState(initialPos === null ? DEFAULT_POS : initialPos);
      var pos = posState[0];
      var setPos = posState[1];

      var collapsedState = useState(readCollapsed());
      var collapsed = collapsedState[0];
      var setCollapsed = collapsedState[1];

      var hiddenState = useState(getHidden());
      var hidden = hiddenState[0];
      var setHidden = hiddenState[1];
      useEffect(function () {
        return subscribeHidden(setHidden);
      }, []);

      var draggingState = useState(false);
      var dragging = draggingState[0];
      var setDragging = draggingState[1];

      var snapshotState = useState(null);
      var snapshot = snapshotState[0];
      var setSnapshot = snapshotState[1];

      var errorState = useState(null);
      var error = errorState[0];
      var setError = errorState[1];

      var refreshingState = useState(false);
      var refreshing = refreshingState[0];
      var setRefreshing = refreshingState[1];

      var lastRefreshState = useState(null);
      var lastRefresh = lastRefreshState[0];
      var setLastRefresh = lastRefreshState[1];

      var codesState = useState(readCodes);
      var codes = codesState[0];
      var setCodes = codesState[1];

      var inputState = useState(function () {
        return readCodes().join(',');
      });
      var inputValue = inputState[0];
      var setInputValue = inputState[1];

      var aliveRef = useRef(true);
      var posRef = useRef(pos);
      var codesRef = useRef(codes);
      var dragRef = useRef(null);
      var movedRef = useRef(false);
      var suppressClickRef = useRef(false);
      var configRef = useRef({
        pollTradingMs: DEFAULT_POLL_TRADING_MS,
        pollIdleMs: DEFAULT_POLL_IDLE_MS,
        backendUrl: DEFAULT_BACKEND_URL,
      });
      var noticeState = useState(null);
      var notice = noticeState[0];
      var setNotice = noticeState[1];
      var noticeTimerRef = useRef(null);

      codesRef.current = codes;

      /** 应用一份快照。 */
      var applySnapshot = useCallback(function (body) {
        setSnapshot(body);
        setError(null);
        setLastRefresh(hms(body.refreshed_at) || localHms());
      }, []);

      /** 拉一次快照（watch 参数取当前自选）。 */
      var load = useCallback(
        function (codesOverride) {
          var activeCodes = Array.isArray(codesOverride) ? codesOverride : codesRef.current;
          return requestSnapshot(activeCodes)
            .then(function (result) {
              if (!aliveRef.current) return;
              var body = unwrapSnapshot(result);
              codesRef.current = activeCodes;
              applySnapshot(body);
            })
            .catch(function (reason) {
              if (!aliveRef.current) return;
              setError(reason && reason.message ? reason.message : String(reason));
            });
        },
        [applySnapshot],
      );

      // 轮询：交易时段 60s、其余 300s（可由 Host /api/config 覆盖）；隐藏时暂停。
      useEffect(
        function () {
          aliveRef.current = true;
          var stopped = false;
          var timer = null;

          function schedule() {
            if (stopped) return;
            var interval =
              isBeijingTradingTime() === true ? configRef.current.pollTradingMs : configRef.current.pollIdleMs;
            timer = setTimeout(run, interval);
          }

          function run() {
            if (stopped) return;
            load().then(function () {
              if (!stopped) schedule();
            });
          }

          run();

          function onVisibilityChange() {
            if (typeof document === 'undefined' || document.hidden === true) return;
            if (timer !== null) {
              clearTimeout(timer);
              timer = null;
            }
            run();
          }

          if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
            document.addEventListener('visibilitychange', onVisibilityChange);
          }

          return function () {
            stopped = true;
            aliveRef.current = false;
            if (timer !== null) {
              clearTimeout(timer);
              timer = null;
            }
            if (typeof document !== 'undefined' && typeof document.removeEventListener === 'function') {
              document.removeEventListener('visibilitychange', onVisibilityChange);
            }
          };
        },
        [load],
      );

      // 轮询节奏由 Host 配置提供（失败则用默认值）。
      useEffect(function () {
        var cancelled = false;
        if (typeof fetch !== 'function') return undefined;
        fetch(API_BASE + '/config', { cache: 'no-store' })
          .then(function (response) {
            return response.ok ? response.json() : null;
          })
          .then(function (body) {
            if (cancelled || body === null || body === undefined) return;
            var trading = num(body.pollTradingMs);
            var idle = num(body.pollIdleMs);
            if (trading !== null) configRef.current.pollTradingMs = trading;
            if (idle !== null) configRef.current.pollIdleMs = idle;
            var backend = body.backendUrl;
            if (typeof backend === 'string' && /^https?:\/\//i.test(backend)) {
              configRef.current.backendUrl = backend.replace(/\/+$/, '');
            }
          })
          .catch(function () {
            /* 保留默认节奏 */
          });
        return function () {
          cancelled = true;
        };
      }, []);

      /** 手动强制刷新（后端 /api/refresh，可能耗时）。 */
      function refreshNow() {
        if (refreshing === true) return;
        setRefreshing(true);
        var activeCodes = codesRef.current;
        requestRefresh(activeCodes)
          .then(function (result) {
            if (!aliveRef.current) return;
            applySnapshot(unwrapSnapshot(result));
          })
          .catch(function (reason) {
            if (!aliveRef.current) return;
            setError(reason && reason.message ? reason.message : String(reason));
          })
          .then(function () {
            if (aliveRef.current) setRefreshing(false);
          });
      }

      /** 保存自选并立即刷新。 */
      function saveWatchlist() {
        var nextCodes = normalizeCodes(inputValue);
        writeStorage(STORAGE_WATCH, nextCodes.join(','));
        codesRef.current = nextCodes;
        setCodes(nextCodes);
        setInputValue(nextCodes.join(','));
        load(nextCodes);
      }

      function onInputChange(event) {
        setInputValue(event && event.target ? event.target.value : '');
      }

      function onInputKeyDown(event) {
        if (event && (event.key === 'Enter' || event.keyCode === 13)) {
          event.preventDefault();
          saveWatchlist();
        }
      }

      function toggleCollapsed() {
        var next = collapsed !== true;
        setCollapsed(next);
        writeStorage(STORAGE_COLLAPSED, next ? '1' : '0');
      }

      /** 关闭看板：整个卡片收起为右下角一个小胶囊按钮，点击可随时恢复。 */
      /** 关闭看板：浮层里什么都不再渲染；恢复入口在左侧边栏底部（仅在关闭时出现）。 */
      function hideWidget() {
        setHiddenShared(true);
      }

      /** 卡片内临时提示（4.5 秒后自动消失）；重复调用会重置计时。 */
      function notify(text) {
        if (noticeTimerRef.current !== null) {
          clearTimeout(noticeTimerRef.current);
          noticeTimerRef.current = null;
        }
        setNotice(text);
        noticeTimerRef.current = setTimeout(function () {
          noticeTimerRef.current = null;
          setNotice(null);
        }, 4500);
      }

      /**
       * 打开完整看板。
       * 桌面端渲染进程跑在 dsh-app:// 下，window.open('/limitup-widget/') 会被解析成
       * dsh-app 链接，Electron 主进程只把 http/https 交给系统浏览器、其余静默拒绝
       * —— 表现就是「点了没反应」。因此这里必须用绝对 http 地址（Host 的 /api/config
       * 返回 backendUrl，主进程会把 http(s) 的 window.open 交给系统默认浏览器）。
       */
      function openDashboard() {
        var backendUrl =
          typeof configRef.current.backendUrl === 'string' ? configRef.current.backendUrl : DEFAULT_BACKEND_URL;
        var target = backendUrl.replace(/\/+$/, '') + '/';
        var opened = null;
        try {
          if (typeof window !== 'undefined' && typeof window.open === 'function') {
            opened = window.open(target, '_blank', 'noopener');
          }
        } catch (error) {
          opened = null;
        }
        // Electron 桌面端：主进程用 shell.openExternal 打开后返回 deny，window.open 返回 null，
        // 这里按「已打开」处理（否则会误报被拦截）。
        var isDesktop = typeof globalThis !== 'undefined' && globalThis !== null && 'dshDesktop' in globalThis;
        if (opened || isDesktop) {
          notify('已在系统浏览器打开完整看板');
          return;
        }
        // 普通浏览器里弹窗被拦：复制地址并提示
        try {
          if (
            typeof navigator !== 'undefined' &&
            navigator.clipboard &&
            typeof navigator.clipboard.writeText === 'function'
          ) {
            navigator.clipboard.writeText(target);
            notify('弹窗被拦截，看板地址已复制：' + target);
            return;
          }
        } catch (error) {
          /* 忽略，走下面的兜底提示 */
        }
        notify('看板地址：' + target);
      }

      // ---- 拖动（右下角锚点：向右拖 right 变小、向上拖 bottom 变大）
      // 注意：绝对不能在 pointerdown 里 setPointerCapture —— 它会把整棵子树的
      // click 劫持到捕获元素上，子按钮的 onClick 永远不触发（历史 bug：收起后
      // 胶囊里的「展开」按钮点不动）。这里改用 window 级监听 + 4px 位移阈值：
      // 位移不足视为点击（按钮照常工作），超过阈值才进入拖动并持久化位置。
      function cleanupDragListeners() {
        if (typeof window === 'undefined') return;
        window.removeEventListener('pointermove', onWindowPointerMove);
        window.removeEventListener('pointerup', onWindowPointerUp);
        window.removeEventListener('pointercancel', onWindowPointerUp);
      }

      function onPointerDown(event) {
        if (event.button !== undefined && event.button !== 0) return;
        var target = event.target;
        // 点在按钮/输入框/链接上时不做拖动，避免吞掉它们的点击
        if (target && typeof target.closest === 'function' &&
            target.closest('button, input, textarea, select, a')) {
          return;
        }
        cleanupDragListeners();
        dragRef.current = {
          startX: event.clientX,
          startY: event.clientY,
          right: posRef.current.right,
          bottom: posRef.current.bottom,
          moved: false,
        };
        movedRef.current = false;
        if (typeof window !== 'undefined') {
          window.addEventListener('pointermove', onWindowPointerMove, { passive: false });
          window.addEventListener('pointerup', onWindowPointerUp);
          window.addEventListener('pointercancel', onWindowPointerUp);
        }
      }

      function onWindowPointerMove(event) {
        var drag = dragRef.current;
        if (drag === null) return;
        var dx = event.clientX - drag.startX;
        var dy = event.clientY - drag.startY;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
        drag.moved = true;
        movedRef.current = true;
        setDragging(true);
        if (typeof event.preventDefault === 'function' && event.cancelable !== false) event.preventDefault();
        var viewportWidth = typeof window !== 'undefined' && window.innerWidth ? window.innerWidth : 1200;
        var viewportHeight = typeof window !== 'undefined' && window.innerHeight ? window.innerHeight : 800;
        var next = {
          right: Math.round(clamp(drag.right - dx, 4, Math.max(4, viewportWidth - 90))),
          bottom: Math.round(clamp(drag.bottom - dy, 4, Math.max(4, viewportHeight - 60))),
        };
        posRef.current = next;
        setPos(next);
      }

      function onWindowPointerUp() {
        cleanupDragListeners();
        var drag = dragRef.current;
        dragRef.current = null;
        setDragging(false);
        if (drag !== null && drag.moved) {
          writeStorage(STORAGE_POS, JSON.stringify(posRef.current));
          // 拖动松开后浏览器紧跟着的 click 不应触发「展开/收起」
          suppressClickRef.current = true;
          setTimeout(function () { suppressClickRef.current = false; }, 0);
        }
      }

      function onPillClick() {
        if (suppressClickRef.current) { suppressClickRef.current = false; return; }
        toggleCollapsed();
      }

      function onPillKeyDown(event) {
        if (!event) return;
        if (event.key === 'Enter' || event.key === ' ' || event.keyCode === 13 || event.keyCode === 32) {
          if (typeof event.preventDefault === 'function') event.preventDefault();
          onPillClick();
        }
      }

      // 卸载时移除 window 上的拖动监听、清理提示定时器（组件被卸载也不会泄漏）
      useEffect(function () {
        return function () {
          cleanupDragListeners();
          if (noticeTimerRef.current !== null) {
            clearTimeout(noticeTimerRef.current);
            noticeTimerRef.current = null;
          }
        };
      }, []);

      var rootStyle = {
        right: pos.right + 'px',
        bottom: pos.bottom + 'px',
        pointerEvents: 'auto',
      };

      var status = statusOf(snapshot, error, Boolean(snapshot && snapshot.stale && snapshot.stale.is_stale));
      var stats = snapshot && snapshot.stats ? snapshot.stats : null;
      var stale = Boolean(snapshot && snapshot.stale && snapshot.stale.is_stale);
      var themes = snapshot && Array.isArray(snapshot.themes) ? snapshot.themes.slice(0, 5) : [];
      var watchRows = snapshot && Array.isArray(snapshot.watchlist) ? snapshot.watchlist : [];
      var tradeDateLabel = formatTradeDate(snapshot ? snapshot.trade_date : null);
      var tradeDateTitle = formatTradeDateFull(snapshot ? snapshot.trade_date : null);
      var dotStyle = { background: status.color };

      // ---- 已关闭：浮层内不渲染任何东西（恢复入口在侧边栏底部，见 RestoreButton）
      if (hidden === true) {
        return null;
      }

      // ---- 收起态：小胶囊（整颗胶囊可点击展开；按住拖动可移动）
      if (collapsed === true) {
        return React.createElement(
          'div',
          {
            'data-limitup-widget': 'true',
            'data-collapsed': 'true',
            className: 'luw-root luw-pill' + (dragging ? ' luw-dragging' : '') + (stale ? ' luw-pill-stale' : ''),
            style: rootStyle,
            title: '涨停直播（点击展开；按住拖动可移动）',
            role: 'button',
            tabIndex: 0,
            onClick: onPillClick,
            onKeyDown: onPillKeyDown,
            onPointerDown: onPointerDown,
          },
          React.createElement('span', { className: 'luw-dot', style: dotStyle }),
          React.createElement(
            'span',
            { className: 'luw-pill-text' },
            '涨停 ' + fmtNum(stats ? stats.limit_up_count : null) + ' · 炸板 ' + fmtNum(stats ? stats.broken_count : null),
          ),
          error ? React.createElement('span', { className: 'luw-pill-stale' }, '未连接') : null,
          actionButton('展开 ▸', onPillClick, { className: 'luw-pill-expand', 'aria-label': '展开', title: '展开完整卡片' }),
        );
      }

      var headerChildren = [
        React.createElement('span', { className: 'luw-dot', style: dotStyle, key: 'dot' }),
        React.createElement('span', { className: 'luw-title', key: 'title' }, '涨停直播'),
        tradeDateLabel !== null
          ? React.createElement(
              'span',
              { className: 'luw-date', key: 'date', title: tradeDateTitle === null ? '' : '数据日 ' + tradeDateTitle },
              tradeDateLabel,
            )
          : null,
        snapshot && snapshot.is_fallback
          ? React.createElement(
              'span',
              {
                className: 'luw-tag luw-tag-warn',
                key: 'fallback',
                title: esc(snapshot.fallback_note || '非当前交易日，已回退到最近交易日'),
              },
              '回退',
            )
          : null,
        React.createElement('span', { className: 'luw-spacer', key: 'spacer' }),
        React.createElement('span', { className: 'luw-status', style: { color: status.color }, key: 'status' }, status.text),
        actionButton('▾', toggleCollapsed, { className: 'luw-icon-btn', 'aria-label': '收起', title: '收起为小胶囊' }),
        actionButton('✕', hideWidget, {
          className: 'luw-icon-btn luw-close-btn',
          'aria-label': '关闭',
          title: '关闭看板（缩成右下角小按钮，点击可恢复）',
        }),
      ];

      var bodyChildren = [];
      if (stale) {
        bodyChildren.push(
          React.createElement(
            'div',
            { className: 'luw-banner', key: 'stale', title: esc(snapshot.stale && snapshot.stale.reason ? snapshot.stale.reason : '') },
            '数据已过期：自 ' + (hhmm(snapshot.stale && snapshot.stale.since) || '--:--') + ' 起未更新',
          ),
        );
      }
      if (notice !== null) {
        bodyChildren.push(
          React.createElement(
            'div',
            { className: 'luw-banner luw-banner-info', key: 'notice', title: esc(notice) },
            esc(notice),
          ),
        );
      }
      if (error !== null) {
        bodyChildren.push(
          React.createElement(
            'div',
            { className: 'luw-error', key: 'error' },
            React.createElement('span', { className: 'luw-error-text', title: esc(error) }, '后端未连接：' + esc(error)),
            actionButton('重试', function () {
              load();
            }, { className: 'luw-btn-small' }),
          ),
        );
      }
      if (snapshot === null) {
        bodyChildren.push(
          React.createElement('div', { className: 'luw-loading', key: 'loading' }, error === null ? '加载中…' : '等待后端…'),
        );
      } else if (snapshot.refreshed_at === null || snapshot.refreshed_at === undefined) {
        bodyChildren.push(
          React.createElement('div', { className: 'luw-empty', key: 'empty' }, '暂无数据（等待后端首次采集）'),
        );
      } else if (stats === null) {
        bodyChildren.push(React.createElement('div', { className: 'luw-empty', key: 'empty' }, '暂无数据'));
      } else {
        bodyChildren.push(
          React.createElement(
            'div',
            { className: 'luw-stats', key: 'stats' },
            statBlock('涨停', fmtNum(stats.limit_up_count), COLORS.red),
            statBlock('炸板', fmtNum(stats.broken_count), COLORS.amber),
            statBlock('跌停', fmtNum(stats.limit_down_count), COLORS.green),
          ),
        );
        var brokenRateValue = num(stats.broken_rate);
        bodyChildren.push(
          React.createElement(
            'div',
            { className: 'luw-substats', key: 'substats' },
            subStat('炸板率', brokenRateValue === null ? '—' : (brokenRateValue * 100).toFixed(1) + '%', COLORS.amber),
            subStat('连板高度', fmtNum(stats.max_streak) + '板', COLORS.text),
            subStat('昨涨停溢价', fmtPct(stats.prev_limit_up_premium), pctColor(stats.prev_limit_up_premium)),
          ),
        );
      }

      if (stats !== null && themes.length > 0) {
        bodyChildren.push(
          React.createElement(
            'div',
            { className: 'luw-section', key: 'themes' },
            React.createElement('div', { className: 'luw-section-title' }, '热门题材'),
            React.createElement(
              'div',
              null,
              themes.map(function (theme, index) {
                var rawName = theme && theme.name ? String(theme.name) : '未分类';
                var name = esc(rawName);
                var count = fmtNum(theme ? theme.count : null);
                var earliest = esc(theme && theme.earliest_first_seal ? theme.earliest_first_seal : '');
                return React.createElement(
                  'div',
                  { className: 'luw-theme-row', key: 'theme-' + String(index) },
                  React.createElement(
                    'span',
                    { className: 'luw-theme-name', title: esc(rawName) },
                    name,
                    theme && theme.is_industry_fallback
                      ? React.createElement('span', { className: 'luw-tag luw-tag-dim' }, '行业')
                      : null,
                  ),
                  React.createElement('span', { className: 'luw-theme-count' }, '×' + count),
                  React.createElement(
                    'span',
                    { className: 'luw-theme-early' },
                    earliest !== '' ? '首封 ' + earliest : '—',
                  ),
                );
              }),
            ),
          ),
        );
      }

      var watchChildren = [];
      if (watchRows.length > 0) {
        for (var wi = 0; wi < watchRows.length; wi += 1) watchChildren.push(watchRow(watchRows[wi], wi));
      } else {
        watchChildren.push(
          React.createElement(
            'div',
            { className: 'luw-watch-empty', key: 'watch-empty' },
            codes.length > 0 ? '自选数据获取中…' : '在下方输入 6 位代码添加自选',
          ),
        );
      }
      watchChildren.push(
        React.createElement(
          'div',
          { className: 'luw-watch-input-row', key: 'watch-input' },
          React.createElement('input', {
            className: 'luw-watch-input',
            value: inputValue,
            placeholder: '600519, 000001',
            spellCheck: false,
            'aria-label': '自选代码，逗号分隔',
            onChange: onInputChange,
            onKeyDown: onInputKeyDown,
          }),
          actionButton('保存', saveWatchlist, { className: 'luw-btn-small', title: '保存自选并刷新' }),
        ),
      );
      bodyChildren.push(
        React.createElement(
          'div',
          { className: 'luw-section', key: 'watchlist' },
          React.createElement('div', { className: 'luw-section-title' }, '自选'),
          React.createElement('div', null, watchChildren),
        ),
      );

      var footerChildren = [
        React.createElement('span', { className: 'luw-footer-time', key: 'time' }, '最后刷新 ' + (lastRefresh === null ? '—' : lastRefresh)),
        React.createElement('span', { className: 'luw-spacer', key: 'spacer' }),
        actionButton(refreshing ? '刷新中…' : '刷新', refreshNow, {
          className: 'luw-btn-small',
          disabled: refreshing,
          title: '向后端请求一次强制刷新',
        }),
        actionButton('完整看板', openDashboard, { className: 'luw-btn-small luw-btn-ghost', title: '在新标签页打开完整看板' }),
        actionButton('收起', toggleCollapsed, { className: 'luw-btn-small luw-btn-ghost', title: '收起为小胶囊' }),
      ];

      return React.createElement(
        'div',
        {
          'data-limitup-widget': 'true',
          'data-collapsed': 'false',
          className: 'luw-root' + (dragging ? ' luw-dragging' : ''),
          style: rootStyle,
        },
        React.createElement(
          'div',
          { className: 'luw-card' },
          React.createElement(
            'div',
            {
              className: 'luw-header',
              onPointerDown: onPointerDown,
            },
            headerChildren,
          ),
          React.createElement('div', { className: 'luw-body' }, bodyChildren),
          React.createElement('div', { className: 'luw-footer' }, footerChildren),
        ),
      );
    }

    /**
     * 侧边栏底部的恢复按钮：只在看板被「✕ 关闭」时出现。
     * 不占用浮层（shell.overlay），所以关闭后页面上没有任何悬浮残留。
     */
    function RestoreButton() {
      var hiddenState = React.useState(getHidden());
      var hidden = hiddenState[0];
      var setHidden = hiddenState[1];
      React.useEffect(function () {
        return subscribeHidden(setHidden);
      }, []);
      if (hidden !== true) return null;
      return React.createElement(
        'button',
        {
          type: 'button',
          'data-limitup-restore': 'true',
          className: 'luw-restore-btn',
          title: '打开涨停直播看板',
          'aria-label': '打开涨停直播看板',
          onClick: function () {
            setHiddenShared(false);
          },
        },
        React.createElement('span', { className: 'luw-dot', style: { background: '#8b949e' } }),
        React.createElement('span', { className: 'luw-restore-text' }, '涨停'),
      );
    }

    // --------------------------------------------------------------- 插件入口

    var inject = ['slots'];

    /**
     * Client 插件入口：注入样式 + 在 shell.overlay 注册浮动卡片。
     * @param {any} ctx
     */
    function apply(ctx) {
      ctx.effect(function () {
        var styleTag = injectStyles();
        return function () {
          if (styleTag !== null && styleTag.parentNode) styleTag.parentNode.removeChild(styleTag);
        };
      }, 'limitup-widget: styles');

      ctx.slots.inject('shell.overlay', function () {
        return ctx.slots.register(
          {
            name: 'shell.overlay',
            id: 'limitup-widget',
            order: 100,
          },
          Widget,
        );
      });

      // 关闭后的恢复入口：左侧边栏底部（仅关闭时出现，不占浮层）
      ctx.slots.inject('sidebar.footer.action', function () {
        return ctx.slots.register(
          {
            name: 'sidebar.footer.action',
            id: 'limitup-restore',
            order: 100,
          },
          RestoreButton,
        );
      });
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
