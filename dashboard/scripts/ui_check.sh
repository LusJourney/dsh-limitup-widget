#!/usr/bin/env bash
# UI acceptance check for the A-share limit-up tracker (CONTRACT §5 / §6.5).
#
# Starts app.py on port 8003 with LIMITUP_DISABLE_BG=1, waits for /api/health
# (max 60s), warms one /api/refresh so the page has real data, then drives headless
# Chrome at 1600x3000 to capture a screenshot and the rendered DOM.  It greps the
# DOM for the required Chinese section labels, checks that no JS "Uncaught" /
# "SyntaxError" leaked into the DOM or Chrome stderr, then kills the server.
#
# Artifacts (all under /tmp/qa/):
#   ui_1600x3000.png          screenshot
#   dom_1600x3000.html        rendered DOM
#   dom_visible.html          DOM with <script>/<style> stripped (label check)
#   chrome_stderr.log         Chrome stderr (console errors)
#   server_8003.log           app.py stderr/stdout
#   snapshot_8003.json        snapshot JSON fetched for the page
#   ui_check.log              this script's own output
#
# Exit code: 0 = all checks PASS, 1 = at least one FAIL.
# Only the server/Chrome started here are killed — never unrelated processes.
set -u

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PORT="${PORT:-8003}"
BASE="http://127.0.0.1:${PORT}"
OUT_DIR="${OUT_DIR:-/tmp/qa}"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PY="$PROJECT_DIR/.venv/bin/python"

mkdir -p "$OUT_DIR"
OUT_LOG="$OUT_DIR/ui_check.log"
exec > >(tee "$OUT_LOG") 2>&1

PASS=0
FAIL=0
WARN=0
record_pass() { PASS=$((PASS + 1)); printf '[CHECK] PASS  %s\n' "$1"; }
record_fail() { FAIL=$((FAIL + 1)); printf '[CHECK] FAIL  %s\n' "$1"; }
record_warn() { WARN=$((WARN + 1)); printf '[CHECK] WARN  %s\n' "$1"; }
record_info() { printf '[INFO ] %s\n' "$1"; }

SERVER_PID=""
CHROME_PID=""
WATCHDOG_PID=""

cleanup() {
  if [ -n "$WATCHDOG_PID" ] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
    kill "$WATCHDOG_PID" 2>/dev/null || true
  fi
  if [ -n "$CHROME_PID" ] && kill -0 "$CHROME_PID" 2>/dev/null; then
    printf '[INFO ] killing leftover Chrome pid %s\n' "$CHROME_PID"
    pkill -P "$CHROME_PID" 2>/dev/null || true
    kill "$CHROME_PID" 2>/dev/null || true
  fi
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    printf '[INFO ] stopping app.py pid %s\n' "$SERVER_PID"
    kill "$SERVER_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 0.25
    done
    kill -9 "$SERVER_PID" 2>/dev/null || true
  fi
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    record_fail "app.py pid $SERVER_PID still alive after cleanup"
  fi
}
trap cleanup EXIT INT TERM

printf '=== limit-up tracker UI check ===\n'
printf 'project : %s\n' "$PROJECT_DIR"
printf 'url     : %s\n' "$BASE"
printf 'chrome  : %s\n' "$CHROME"
printf 'out_dir : %s\n\n' "$OUT_DIR"

# --------------------------------------------------------------------------- #
# 0. prerequisites
# --------------------------------------------------------------------------- #
if [ -f "$PROJECT_DIR/app.py" ]; then
  record_pass "app.py exists"
else
  record_fail "app.py missing at $PROJECT_DIR/app.py"
  printf '\nRESULT: FAIL (%s passed, %s failed, %s warnings)\n' "$PASS" "$FAIL" "$WARN"
  exit 1
fi
if [ -x "$PY" ]; then
  record_pass "venv python executable: $PY"
else
  record_fail "venv python missing/not executable: $PY"
fi
if [ -x "$CHROME" ]; then
  record_pass "Chrome headless binary found"
else
  record_fail "Chrome binary not found at $CHROME"
  printf '\nRESULT: FAIL (%s passed, %s failed, %s warnings)\n' "$PASS" "$FAIL" "$WARN"
  exit 1
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  record_fail "port $PORT already in use — refusing to touch it (choose PORT=...)"
  printf '\nRESULT: FAIL (%s passed, %s failed, %s warnings)\n' "$PASS" "$FAIL" "$WARN"
  exit 1
else
  record_pass "port $PORT is free"
fi

# --------------------------------------------------------------------------- #
# 1. start server (QA mode: no background threads)
# --------------------------------------------------------------------------- #
SERVER_LOG="$OUT_DIR/server_${PORT}.log"
rm -f "$SERVER_LOG" "$OUT_DIR/health_${PORT}.json" "$OUT_DIR/snapshot_${PORT}.json"
printf '[INFO ] starting: PORT=%s LIMITUP_DISABLE_BG=1 %s app.py\n' "$PORT" "$PY"
# cd in the parent so $! is the python process itself (a subshell would orphan the
# server when cleanup kills only the subshell PID — found during QA acceptance).
cd "$PROJECT_DIR" || { record_fail "cannot cd to $PROJECT_DIR"; exit 1; }
PORT="$PORT" LIMITUP_DISABLE_BG=1 "$PY" app.py >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
record_info "app.py pid=$SERVER_PID log=$SERVER_LOG"

health_ok=0
for _ in $(seq 1 120); do  # 120 * 0.5s = 60s max (CONTRACT task: max 60s)
  if curl -fsS --max-time 2 "$BASE/api/health" -o "$OUT_DIR/health_${PORT}.json" 2>/dev/null; then
    health_ok=1
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    break
  fi
  sleep 0.5
done
if [ "$health_ok" = 1 ]; then
  record_pass "/api/health responded within 60s"
  if grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$OUT_DIR/health_${PORT}.json"; then
    record_pass "/api/health body has ok=true"
  else
    record_fail "/api/health body missing ok=true"
  fi
  if grep -Eq '"uptime_sec"[[:space:]]*:[[:space:]]*[0-9]' "$OUT_DIR/health_${PORT}.json"; then
    record_pass "/api/health has numeric uptime_sec"
  else
    record_fail "/api/health missing numeric uptime_sec"
  fi
else
  record_fail "/api/health did not respond within 60s (server log: $SERVER_LOG)"
  tail -30 "$SERVER_LOG"
fi

# --------------------------------------------------------------------------- #
# 2. warm one synchronous refresh, then fetch the snapshot the page will poll
# --------------------------------------------------------------------------- #
SNAPSHOT="$OUT_DIR/snapshot_${PORT}.json"
refresh_rc=1
for attempt in 1 2; do
  if curl -fsS --max-time 180 "$BASE/api/refresh?watch=600519,000001" -o "$SNAPSHOT" 2>/dev/null; then
    refresh_rc=0
    break
  fi
  record_warn "/api/refresh attempt $attempt failed (data source slow/offline?)"
  sleep 2
done
if [ "$refresh_rc" = 0 ]; then
  record_pass "/api/refresh returned JSON (page warmed with real data)"
else
  curl -fsS --max-time 30 "$BASE/api/snapshot?watch=600519,000001" -o "$SNAPSHOT" 2>/dev/null || true
  record_warn "/api/refresh never succeeded — page screenshot will show empty/stale state"
fi

# Optional data sanity via the project venv python (contract §6.2/§6.3 evidence).
if [ -s "$SNAPSHOT" ] && [ -x "$PY" ]; then
  PYTHONPATH="$PROJECT_DIR" "$PY" - "$SNAPSHOT" <<'PYEOF' && DATA_OK=1 || DATA_OK=0
import json, sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    snap = json.load(fh)
print("[INFO ] snapshot: ok=%r trade_date=%r requested_date=%r is_fallback=%r phase=%r"
      % (snap.get("ok"), snap.get("trade_date"), snap.get("requested_date"),
         snap.get("is_fallback"), snap.get("market_phase")))
stats = snap.get("stats") or {}
print("[INFO ] stats: limit_up=%r broken=%r limit_down=%r broken_rate=%r max_streak=%r premium=%r"
      % (stats.get("limit_up_count"), stats.get("broken_count"),
         stats.get("limit_down_count"), stats.get("broken_rate"),
         stats.get("max_streak"), stats.get("prev_limit_up_premium")))
print("[INFO ] themes=%d ladder=%d broken=%d timeline=%d limit_down=%d watchlist=%d"
      % (len(snap.get("themes") or []), len(snap.get("ladder") or []),
         len(snap.get("broken") or []), len(snap.get("timeline") or []),
         len(snap.get("limit_down") or []), len(snap.get("watchlist") or [])))
sys.exit(0)
PYEOF
  if [ "${DATA_OK:-0}" = 1 ]; then
    record_pass "/api/snapshot is valid JSON parsable by python"
  else
    record_fail "/api/snapshot is not valid JSON (see $SNAPSHOT)"
  fi
else
  record_warn "no snapshot JSON captured — skipping data parse check"
fi

# --------------------------------------------------------------------------- #
# 3. headless Chrome: screenshot 1600x3000 + rendered DOM
# --------------------------------------------------------------------------- #
SHOT="$OUT_DIR/ui_1600x3000.png"
DOM="$OUT_DIR/dom_1600x3000.html"
DOM_VISIBLE="$OUT_DIR/dom_visible.html"
CHROME_ERR="$OUT_DIR/chrome_stderr.log"
CHROME_PROFILE="$OUT_DIR/chrome-profile-$$"
rm -f "$SHOT" "$DOM" "$DOM_VISIBLE" "$CHROME_ERR"
rm -rf "$CHROME_PROFILE"

printf '[INFO ] chrome: screenshot 1600x3000 + dump-dom from %s\n' "$BASE/"
(
  "$CHROME" \
    --headless=new \
    --disable-gpu \
    --hide-scrollbars \
    --no-first-run \
    --no-default-browser-check \
    --disable-extensions \
    --user-data-dir="$CHROME_PROFILE" \
    --window-size=1600,3000 \
    --virtual-time-budget=10000 \
    --enable-logging=stderr \
    --screenshot="$SHOT" \
    --dump-dom \
    "$BASE/"
) >"$DOM" 2>"$CHROME_ERR" &
CHROME_PID=$!
# Watchdog: never let headless Chrome hang this check (60s).
(
  sleep 60
  if [ -n "$CHROME_PID" ] && kill -0 "$CHROME_PID" 2>/dev/null; then
    printf '[WARN ] Chrome exceeded 60s — killing pid %s\n' "$CHROME_PID"
    kill "$CHROME_PID" 2>/dev/null || true
  fi
) &
WATCHDOG_PID=$!
wait "$CHROME_PID"
CHROME_RC=$?
kill "$WATCHDOG_PID" 2>/dev/null || true
WATCHDOG_PID=""

if [ "$CHROME_RC" -eq 0 ]; then
  record_pass "headless Chrome exited cleanly"
else
  record_fail "headless Chrome exit code $CHROME_RC (stderr: $CHROME_ERR)"
fi
if [ -s "$SHOT" ]; then
  shot_bytes=$(stat -f%z "$SHOT" 2>/dev/null || echo 0)
  if [ "$shot_bytes" -gt 10000 ]; then
    record_pass "screenshot ui_1600x3000.png non-trivial (${shot_bytes} bytes)"
  else
    record_fail "screenshot ui_1600x3000.png suspiciously small (${shot_bytes} bytes)"
  fi
else
  record_fail "screenshot ui_1600x3000.png was not created"
fi
if [ -s "$DOM" ]; then
  record_pass "rendered DOM captured ($(wc -c <"$DOM" | tr -d ' ') bytes)"
else
  record_fail "rendered DOM is empty"
fi

# Visible markup = DOM with <script> blocks stripped, so labels must be real page
# content, not just strings inside the inlined JavaScript bundle.
if [ -s "$DOM" ]; then
  perl -0777 -pe 's{<script\b[^>]*>.*?</script>}{}gsi; s{<style\b[^>]*>.*?</style>}{}gsi' "$DOM" >"$DOM_VISIBLE" || true
  if [ -s "$DOM_VISIBLE" ]; then
    record_pass "visible DOM (script/style stripped) extracted"
  else
    record_fail "could not strip script/style from DOM"
  fi
else
  : >"$DOM_VISIBLE"
fi

for label in 涨停 炸板 题材 连板梯队 时间线 自选; do
  if grep -qF "$label" "$DOM_VISIBLE" 2>/dev/null; then
    record_pass "visible DOM contains section label: $label"
  elif grep -qF "$label" "$DOM" 2>/dev/null; then
    record_warn "label $label only found inside <script> (not rendered as text)"
  else
    record_fail "visible DOM missing required section label: $label"
  fi
done

# Chart.js canvas rendered (CONTRACT §5.5 / §6.5).
if grep -q "<canvas" "$DOM" 2>/dev/null; then
  record_pass "Chart.js canvas present in DOM"
else
  record_warn "no <canvas> in DOM — Chart.js CDN may be unreachable offline (placeholder allowed, §5)"
fi

# Console errors must not surface anywhere we can see them (§5 page has no console
# errors; §6.5 no console error).
if grep -Eq "Uncaught|SyntaxError" "$DOM" 2>/dev/null; then
  record_fail "Uncaught/SyntaxError found in rendered DOM"
else
  record_pass "no Uncaught/SyntaxError in rendered DOM"
fi
if grep -Eq "Uncaught|SyntaxError" "$CHROME_ERR" 2>/dev/null; then
  record_fail "Uncaught/SyntaxError found in Chrome stderr log"
else
  record_pass "no Uncaught/SyntaxError in Chrome stderr log"
fi

# --------------------------------------------------------------------------- #
# 4. post-checks on server log (informational)
# --------------------------------------------------------------------------- #
if [ -s "$SERVER_LOG" ]; then
  if grep -q "Traceback (most recent call last)" "$SERVER_LOG"; then
    record_warn "server log contains a Python traceback (check if expected/forced failure)"
  fi
  startup_count=$(grep -c "\[startup\]" "$SERVER_LOG" 2>/dev/null || echo 0)
  if [ "$startup_count" -ge 1 ]; then
    record_pass "server logged [startup] line (§4.6)"
  else
    record_warn "no [startup] line in server log"
  fi
fi

# --------------------------------------------------------------------------- #
# 5. cleanup (trap also runs) + summary
# --------------------------------------------------------------------------- #
if kill -0 "$SERVER_PID" 2>/dev/null; then
  kill "$SERVER_PID" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 0.25
  done
  kill -9 "$SERVER_PID" 2>/dev/null || true
fi
if kill -0 "$SERVER_PID" 2>/dev/null; then
  record_fail "app.py did not shut down after SIGTERM"
else
  record_pass "app.py terminated cleanly"
fi
rm -rf "$CHROME_PROFILE"

printf '\nartifacts:\n'
for artifact in "$SHOT" "$DOM" "$DOM_VISIBLE" "$CHROME_ERR" "$SERVER_LOG" "$SNAPSHOT" "$OUT_LOG"; do
  if [ -e "$artifact" ]; then
    printf '  %s\n' "$artifact"
  fi
done
printf '\nRESULT: %s (%s passed, %s failed, %s warnings)\n' \
  "$([ "$FAIL" -eq 0 ] && echo PASS || echo FAIL)" "$PASS" "$FAIL" "$WARN"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
