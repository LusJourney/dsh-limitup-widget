#!/usr/bin/env bash
# =============================================================================
# A股涨停直播看板 — 冒烟测试（部署后验收 / CI 可用）
#
# 用法：
#   ./smoke.sh [BASE_URL] [--wait 秒数]
#   BASE_URL 默认 http://127.0.0.1:80（也可用环境变量 SMOKE_URL 覆盖）
#   --wait N：冷启动时最多等待 N 秒，直到 /api/health 返回 200（默认 0 = 不等待）
#   SMOKE_ACCESS_CODE=xxx：服务端启用了 LIMITUP_ACCESS_CODE 时传入访问码（走 X-Limitup-Code 头）
#
# 检查项：
#   1. GET /api/health          -> 200 + JSON ok=true
#   2. GET /api/snapshot        -> 200 + JSON 结构/字段合法（trade_date 等契约字段）
#   3. GET /api/snapshot?watch= -> 200 + watchlist 结构合法
#   4. GET /                   -> 200 + text/html + 页面关键内容
#
# 退出码：0 全部通过；1 存在失败项。需要 curl + python3。
# =============================================================================
set -euo pipefail

BASE_URL="${SMOKE_URL:-http://127.0.0.1:80}"
WAIT_SECONDS="${SMOKE_WAIT:-0}"
MAX_TIME="${SMOKE_MAX_TIME:-90}"

usage() {
  sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
}

POSITIONAL=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --wait)   WAIT_SECONDS="${2:-60}"; shift 2 ;;
    --wait=*) WAIT_SECONDS="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *)        POSITIONAL+=("$1"); shift ;;
  esac
done
if [ "${#POSITIONAL[@]}" -gt 0 ]; then
  BASE_URL="${POSITIONAL[0]}"
fi
BASE_URL="${BASE_URL%/}"

# 可选：服务端启用 LIMITUP_ACCESS_CODE 时，用 SMOKE_ACCESS_CODE 传入（走 X-Limitup-Code 头）
ACCESS_CODE="${SMOKE_ACCESS_CODE:-}"

if [ -t 1 ]; then
  C_G=$'\033[1;32m'; C_R=$'\033[1;31m'; C_Y=$'\033[1;33m'; C_B=$'\033[1;34m'; C_0=$'\033[0m'
else
  C_G=''; C_R=''; C_Y=''; C_B=''; C_0=''
fi

PASS=0
FAIL=0
WARN=0

pass() { PASS=$((PASS + 1)); printf '  %s[PASS]%s %s\n' "$C_G" "$C_0" "$*"; }
fail() { FAIL=$((FAIL + 1)); printf '  %s[FAIL]%s %s\n' "$C_R" "$C_0" "$*"; }
warn() { WARN=$((WARN + 1)); printf '  %s[WARN]%s %s\n' "$C_Y" "$C_0" "$*"; }
info() { printf '%s[smoke]%s %s\n' "$C_B" "$C_0" "$*"; }

TMPDIR_SMOKE="$(mktemp -d "${TMPDIR:-/tmp}/limitup-smoke.XXXXXX")"
trap 'rm -rf "$TMPDIR_SMOKE"' EXIT

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { printf '[smoke] 缺少命令：%s\n' "$1" >&2; exit 1; }
}
need_cmd curl

PY=""
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
fi
if [ -z "$PY" ]; then
  printf '[smoke] 需要 python3 校验 JSON（服务端 Ubuntu 自带；本机请安装）\n' >&2
  exit 1
fi

# 注意：用 python 计算，避免 GNU date -d / BSD date 的平台差异
NOW_EPOCH="$("$PY" -c 'import time; print(int(time.time()))')"
WAIT_DEADLINE=$((NOW_EPOCH + WAIT_SECONDS))

info "目标服务：$BASE_URL${ACCESS_CODE:+（已提供访问码）}"

# ---------------------------------------------------------------------------
# 0) 冷启动等待 /api/health 可用
# ---------------------------------------------------------------------------
if [ "$WAIT_SECONDS" -gt 0 ]; then
  info "等待服务就绪（最多 ${WAIT_SECONDS}s）..."
  while :; do
    code="$(curl -sS -H "X-Limitup-Code: ${ACCESS_CODE}" -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/api/health" 2>/dev/null || true)"
    [ "$code" = "200" ] && break
    [ "$("$PY" -c 'import time; print(int(time.time()))')" -ge "$WAIT_DEADLINE" ] && break
    sleep 3
  done
fi

# ---------------------------------------------------------------------------
# 1) /api/health
# ---------------------------------------------------------------------------
info "[1/4] GET /api/health"
code="$(curl -sS -H "X-Limitup-Code: ${ACCESS_CODE}" -o "$TMPDIR_SMOKE/health.json" -w '%{http_code}' --max-time 15 "$BASE_URL/api/health" 2>/dev/null || true)"
if [ "$code" = "200" ]; then
  if "$PY" - "$TMPDIR_SMOKE/health.json" >"$TMPDIR_SMOKE/health.txt" 2>&1 <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
assert isinstance(d, dict), "响应不是 JSON 对象"
assert d.get("ok") is True, "ok != true"
assert isinstance(d.get("uptime_sec"), (int, float)), "uptime_sec 缺失/类型错误"
print("uptime_sec=%.1fs" % float(d["uptime_sec"]))
PY
  then
    pass "GET /api/health -> 200（ok=true, $(cat "$TMPDIR_SMOKE/health.txt")）"
  else
    fail "GET /api/health -> 200 但 JSON 结构不符：$(tr '\n' ' ' <"$TMPDIR_SMOKE/health.txt")"
  fi
else
  fail "GET /api/health -> ${code:-000}（期望 200）"
fi

# ---------------------------------------------------------------------------
# 2) GET /api/snapshot —— 契约字段校验
# ---------------------------------------------------------------------------
info "[2/4] GET /api/snapshot"
meta="$(curl -sSL -H "X-Limitup-Code: ${ACCESS_CODE}" -o "$TMPDIR_SMOKE/snapshot.json" -w '%{http_code} %{content_type}' --max-time "$MAX_TIME" "$BASE_URL/api/snapshot" 2>/dev/null || true)"
code="${meta%% *}"
ctype="${meta#* }"
ctype="${ctype%%;*}"
if [ "$code" = "200" ]; then
  if "$PY" - "$TMPDIR_SMOKE/snapshot.json" >"$TMPDIR_SMOKE/snapshot.txt" 2>&1 <<'PY'
import json, re, sys
from datetime import date, datetime, timedelta

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    d = json.load(f)

errs, warns = [], []
if not isinstance(d, dict):
    print("FATAL: 顶层不是 JSON 对象")
    sys.exit(1)

if d.get("ok") is not True:
    errs.append("ok != true")

td = str(d.get("trade_date") or "")
if not re.fullmatch(r"\d{8}", td):
    errs.append("trade_date 非法（应为 YYYYMMDD，实际 %r）" % td)
else:
    try:
        dt = datetime.strptime(td, "%Y%m%d").date()
        today = date.today()
        if dt > today + timedelta(days=1):
            errs.append("trade_date 在未来: %s" % td)
        elif (today - dt).days > 30:
            errs.append("trade_date 距今 %d 天，数据明显过旧" % (today - dt).days)
        elif (today - dt).days > 10:
            warns.append("trade_date 距今 %d 天（长假/停市时属正常，请确认）" % (today - dt).days)
    except ValueError:
        errs.append("trade_date 不是合法日期: %s" % td)

ga = d.get("generated_at")
if not (isinstance(ga, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ga)):
    errs.append("generated_at 缺失或格式错误: %r" % ga)

if not isinstance(d.get("market_phase"), str) or not d.get("market_phase"):
    errs.append("market_phase 缺失")
elif d["market_phase"] not in ("pre_open", "auction", "trading", "lunch", "closed", "non_trading_day"):
    warns.append("market_phase 出现未知值: %s" % d["market_phase"])

stale = d.get("stale")
if not isinstance(stale, dict) or "is_stale" not in stale:
    errs.append("stale 结构缺失")
elif stale.get("is_stale") is True:
    warns.append("数据处于 stale 状态：since=%s reason=%s"
                 % (stale.get("since"), stale.get("reason")))

stats = d.get("stats")
if not isinstance(stats, dict):
    errs.append("stats 缺失")
else:
    for k in ("limit_up_count", "broken_count", "limit_down_count", "max_streak"):
        if not isinstance(stats.get(k), int) or isinstance(stats.get(k), bool):
            errs.append("stats.%s 不是整数: %r" % (k, stats.get(k)))
    for k in ("broken_rate", "prev_limit_up_premium", "prev_limit_up_count"):
        if stats.get(k) is not None and not isinstance(stats.get(k), (int, float)):
            errs.append("stats.%s 类型错误: %r" % (k, stats.get(k)))

for k in ("themes", "ladder", "broken", "limit_down", "timeline", "watchlist", "warnings"):
    if not isinstance(d.get(k), list):
        errs.append("%s 不是数组" % k)
if not isinstance(d.get("concepts"), dict):
    errs.append("concepts 缺失")

for w in warns:
    print("WARN: " + w)
print("trade_date=%s requested_date=%s phase=%s limit_up=%s broken=%s limit_down=%s max_streak=%s themes=%d ladder=%d"
      % (td, d.get("requested_date"), d.get("market_phase"), stats.get("limit_up_count"),
         stats.get("broken_count"), stats.get("limit_down_count"), stats.get("max_streak"),
         len(d.get("themes") or []), len(d.get("ladder") or [])))
if errs:
    print("JSON 结构不符：" + "；".join(errs))
    sys.exit(1)
sys.exit(0)
PY
  then
    pass "GET /api/snapshot -> 200（JSON 契约通过）"
    sed 's/^/          /' "$TMPDIR_SMOKE/snapshot.txt"
  else
    fail "GET /api/snapshot -> 200 但 JSON 契约不符"
    sed 's/^/          /' "$TMPDIR_SMOKE/snapshot.txt"
  fi
else
  fail "GET /api/snapshot -> ${code:-000}（期望 200；首次启动请等待 10–20s 后再试）"
fi
if [ "$code" = "200" ] && [ "$ctype" != "application/json" ]; then
  warn "GET /api/snapshot Content-Type=${ctype:-未知}（期望 application/json）"
fi

# ---------------------------------------------------------------------------
# 3) GET /api/snapshot?watch= —— 自选股结构
# ---------------------------------------------------------------------------
info "[3/4] GET /api/snapshot?watch=600519,000001"
meta="$(curl -sSL -H "X-Limitup-Code: ${ACCESS_CODE}" -o "$TMPDIR_SMOKE/watch.json" -w '%{http_code}' --max-time "$MAX_TIME" "$BASE_URL/api/snapshot?watch=600519,000001" 2>/dev/null || true)"
code="${meta%% *}"
if [ "$code" = "200" ]; then
  if "$PY" - "$TMPDIR_SMOKE/watch.json" >"$TMPDIR_SMOKE/watch.txt" 2>&1 <<'PY'
import json, re, sys
with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
errs = []
if d.get("ok") is not True:
    errs.append("ok != true")
wl = d.get("watchlist")
if not isinstance(wl, list):
    errs.append("watchlist 不是数组")
else:
    if not wl:
        print("WARN: watchlist 为空（首次拉取自选行情可能需 20–40s，可稍后重试）")
    for item in wl[:5]:
        if not isinstance(item, dict):
            errs.append("watchlist 元素不是对象")
            break
        if not re.fullmatch(r"\d{6}", str(item.get("code") or "")):
            errs.append("watchlist.code 非法: %r" % item.get("code"))
        if "name" not in item:
            errs.append("watchlist 缺少 name 字段")
        if not isinstance(item.get("ok"), bool):
            errs.append("watchlist.ok 不是布尔: %r" % item.get("ok"))
print("watchlist=%d" % len(wl))
if errs:
    print("结构不符：" + "；".join(errs))
    sys.exit(1)
PY
  then
    pass "GET /api/snapshot?watch= -> 200（watchlist 结构通过）"
    sed 's/^/          /' "$TMPDIR_SMOKE/watch.txt"
  else
    fail "GET /api/snapshot?watch= -> 200 但 watchlist 结构不符"
    sed 's/^/          /' "$TMPDIR_SMOKE/watch.txt"
  fi
else
  fail "GET /api/snapshot?watch= -> ${code:-000}（期望 200）"
fi

# ---------------------------------------------------------------------------
# 4) GET / —— 页面 HTML
# ---------------------------------------------------------------------------
info "[4/4] GET /"
meta="$(curl -sSL -H "X-Limitup-Code: ${ACCESS_CODE}" -o "$TMPDIR_SMOKE/index.html" -w '%{http_code} %{content_type} %{size_download}' --max-time 30 "$BASE_URL/" 2>/dev/null || true)"
code="${meta%% *}"
rest="${meta#* }"
ctype="${rest%% *}"
size="${rest##* }"
ctype="${ctype%%;*}"
if [ "$code" = "200" ] && [ "$ctype" = "text/html" ] && [ "${size:-0}" -gt 5000 ] \
   && grep -q "api/snapshot" "$TMPDIR_SMOKE/index.html" && grep -q "涨停" "$TMPDIR_SMOKE/index.html"; then
  pass "GET / -> 200（text/html, ${size} 字节，含页面关键内容）"
else
  fail "GET / -> ${code:-000}（content-type=${ctype:-?}, size=${size:-0}；期望 200 + text/html + 页面内容）"
fi

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
printf '\n[smoke] 结果：通过 %d 项，失败 %d 项，警告 %d 项\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -gt 0 ]; then
  printf '%s[smoke] 未通过 ✘%s（服务未就绪 / 数据源不可用 / 反向代理配置有误，见 DEPLOY.md §10）\n' "$C_R" "$C_0"
  exit 1
fi
printf '%s[smoke] 全部通过 ✔%s\n' "$C_G" "$C_0"
