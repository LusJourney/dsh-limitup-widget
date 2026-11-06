#!/usr/bin/env bash
# =============================================================================
# A股涨停直播看板 — 一键部署 / 运维脚本（本机 & 云服务器通用）
#
# 常用命令：
#   ./deploy.sh up                 # 本机/服务器一键部署：装 Docker -> 生成 .env -> 构建镜像 -> 启动 -> 健康检查 -> 冒烟测试
#   ./deploy.sh deploy user@host   # 本地把源码 rsync 到服务器并远程执行 up
#   ./deploy.sh rollback           # 回退到上一个镜像（仅当之前执行过 up 至少两次）
#   ./deploy.sh restart            # 按当前 .env 重建容器（改了环境变量后用）
#   ./deploy.sh status             # 查看容器状态 + 外网探活
#   ./deploy.sh logs [服务名]      # 跟踪日志（app / caddy / nginx）
#   ./deploy.sh smoke              # 只跑冒烟测试
#   ./deploy.sh stop | down        # 停止容器（down 会删容器/网络，保留数据卷）
#   ./deploy.sh purge --yes        # 危险：删容器 + 命名卷（数据全丢）
#   ./deploy.sh config             # 打印 compose 最终配置（排查环境变量）
#
# 说明：
#   * 幂等：重复执行 up 安全；数据卷 app-data 不会丢
#   * 首次构建约 3–8 分钟（akshare 依赖较大，取决于网络）
#   * 需要 Linux/macOS + bash；云服务器推荐 Ubuntu 22.04/24.04（见 DEPLOY.md）
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
IMAGE_NAME="ashare-limitup:local"
PREV_IMAGE="ashare-limitup:prev"
REMOTE_DIR="${REMOTE_DIR:-ashare-limitup-tracker}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

if [ -t 1 ]; then
  C_B=$'\033[1;34m'; C_Y=$'\033[1;33m'; C_R=$'\033[1;31m'; C_G=$'\033[1;32m'; C_0=$'\033[0m'
else
  C_B=''; C_Y=''; C_R=''; C_G=''; C_0=''
fi
log()  { printf '%s[deploy]%s %s\n' "$C_B" "$C_0" "$*"; }
warn() { printf '%s[deploy][warn]%s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%s[deploy][error]%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1，请先安装"; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# 带超时执行（daemon 挂死时避免脚本永久卡住；Linux 有 coreutils timeout 时优先用）
timeout_run() {
  local secs="$1" pid watcher rc
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
    return $?
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
    return $?
  fi
  "$@" &
  pid=$!
  ( sleep "$secs"; kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null ) &
  watcher=$!
  rc=0
  wait "$pid" 2>/dev/null || rc=$?
  kill "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true
  return "$rc"
}

# ---------------------------------------------------------------------------
# docker / compose 封装（自动处理 sudo 与 compose v2 插件）
# ---------------------------------------------------------------------------
NEED_SUDO=0
dk() { if [ "$NEED_SUDO" = "1" ]; then sudo docker "$@"; else docker "$@"; fi; }

detect_docker_perm() {
  command -v docker >/dev/null 2>&1 || return 1
  if timeout_run 15 docker info >/dev/null 2>&1; then
    NEED_SUDO=0
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && timeout_run 15 sudo -n docker info >/dev/null 2>&1; then
    NEED_SUDO=1
    return 0
  fi
  return 1
}

compose() {
  if dk compose version >/dev/null 2>&1; then
    dk compose --project-directory "$SCRIPT_DIR" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose --project-directory "$SCRIPT_DIR" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
  else
    die "未找到 docker compose（Docker Engine 需安装 compose 插件：apt install docker-compose-plugin）"
  fi
}

# ---------------------------------------------------------------------------
# .env 读写（不覆盖用户已填内容；按 DOMAIN 推导 SITE_ADDRESS）
# ---------------------------------------------------------------------------
env_get() {
  [ -f "$ENV_FILE" ] || { printf ''; return 0; }
  awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE"
}

env_set() {
  local k="$1" v="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$k" -v v="$v" '
    $0 ~ ("^[[:space:]]*" k "=") { if (!done) { print k "=" v; done = 1 } next }
    { print }
    END { if (!done) print k "=" v }
  ' "$ENV_FILE" >"$tmp"
  cat "$tmp" >"$ENV_FILE"
  rm -f "$tmp"
}

ensure_env() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    log "已生成 $ENV_FILE（首次部署；可编辑域名/访问码后重跑）"
  fi
  local domain site
  domain="$(env_get DOMAIN)"
  site="$(env_get SITE_ADDRESS)"
  # SITE_ADDRESS 是派生值（不要手改）：有域名 -> https://<domain>；无域名 -> :80
  if [ -n "$domain" ]; then
    if [ "$site" != "https://$domain" ]; then
      env_set SITE_ADDRESS "https://$domain"
      log "已按 DOMAIN=$domain 推导 SITE_ADDRESS=https://$domain（Caddy 将自动申请证书）"
    fi
  else
    case "$site" in
      ''|https://*)
        env_set SITE_ADDRESS ":80"
        warn "未设置 DOMAIN：本次只提供 http://<服务器公网IP>/ 明文访问（无 HTTPS）"
        ;;
      :80)
        warn "未设置 DOMAIN：本次只提供 http://<服务器公网IP>/ 明文访问（无 HTTPS）"
        ;;
      *)
        warn "未设置 DOMAIN 但 SITE_ADDRESS=$site：请确认反向代理地址配置正确"
        ;;
    esac
  fi
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}

http_port() {
  local p
  p="$(env_get HTTP_PORT)"
  printf '%s' "${p:-80}"
}

# 冒烟测试/外网访问目标地址
smoke_url() {
  local site domain
  site="$(env_get SITE_ADDRESS)"
  domain="$(env_get DOMAIN)"
  if [ -n "$domain" ]; then
    printf 'https://%s' "$domain"
    return 0
  fi
  case "${site:-}" in
    https://*|http://*) printf '%s' "$site" ;;
    :*)                 printf 'http://127.0.0.1%s' "$site" ;;
    '')                 printf 'http://127.0.0.1:%s' "$(http_port)" ;;
    *)                  printf 'https://%s' "$site" ;;
  esac
}

# ---------------------------------------------------------------------------
# Docker 安装 / 就绪检查
# ---------------------------------------------------------------------------
ensure_docker() {
  local os
  os="$(uname -s)"
  if command -v docker >/dev/null 2>&1; then
    if detect_docker_perm && dk compose version >/dev/null 2>&1; then
      log "Docker 就绪：$(dk --version 2>/dev/null | head -n1)"
      return 0
    fi
    if [ "$os" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
      log "Docker 已安装但服务未运行，尝试启动..."
      as_root systemctl start docker >/dev/null 2>&1 || true
      sleep 2
      if detect_docker_perm && dk compose version >/dev/null 2>&1; then
        log "Docker 服务已启动"
        return 0
      fi
    fi
    if ! dk compose version >/dev/null 2>&1 && [ "$os" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
      log "安装 docker compose 插件..."
      as_root apt-get update -qq || true
      as_root apt-get install -y -qq docker-compose-plugin || true
      if detect_docker_perm && dk compose version >/dev/null 2>&1; then
        return 0
      fi
    fi
    if [ "$os" != "Linux" ]; then
      die "Docker 已安装但 daemon 不可用：请先启动 Docker Desktop 再重跑本脚本"
    fi
    if ! detect_docker_perm; then
      die "当前用户无 docker 权限：请退出 SSH 重新登录（或用 sudo 运行）后重跑 ./deploy.sh up"
    fi
    die "docker compose 插件不可用：请执行 apt install docker-compose-plugin（见 DEPLOY.md §10）"
  fi

  [ "$os" = "Linux" ] || die "未检测到 Docker：macOS/Windows 请先安装并启动 Docker Desktop"
  need_cmd curl
  log "安装 Docker Engine + compose 插件（官方便捷脚本 https://get.docker.com）..."
  curl -fsSL https://get.docker.com | as_root sh || die "Docker 安装失败，请参考 DEPLOY.md §3 手动安装"
  if command -v systemctl >/dev/null 2>&1; then
    as_root systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  if [ "$(id -u)" -ne 0 ]; then
    as_root usermod -aG docker "$(id -un)" >/dev/null 2>&1 || true
  fi
  NEED_SUDO=0
  if ! detect_docker_perm; then
    die "Docker 已安装，但当前用户权限未生效：请重新登录 SSH（或执行 newgrp docker）后重跑 ./deploy.sh up"
  fi
  dk compose version >/dev/null 2>&1 || die "docker compose 插件缺失：请安装 docker-compose-plugin 后重跑"
  log "Docker 安装完成：$(dk --version 2>/dev/null | head -n1)"
}

# ---------------------------------------------------------------------------
# 启动 / 健康检查 / 冒烟
# ---------------------------------------------------------------------------
wait_healthy() {
  local timeout="${1:-180}" waited=0 cid status
  log "等待 app 容器健康检查通过（最长 ${timeout}s）..."
  while [ "$waited" -lt "$timeout" ]; do
    cid="$(compose ps -q app 2>/dev/null || true)"
    if [ -n "$cid" ]; then
      status="$(dk inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
      case "$status" in
        healthy) log "app 容器健康（${waited}s）"; return 0 ;;
        unhealthy|exited|dead) compose logs --tail=40 app >&2 || true; die "app 容器状态异常：$status" ;;
      esac
    fi
    sleep 5
    waited=$((waited + 5))
  done
  compose logs --tail=60 app >&2 || true
  die "等待健康检查超时（${timeout}s）：见上面 app 日志，或 ./deploy.sh logs app 排查"
}

run_smoke() {
  local url="$1" code
  code="$(env_get LIMITUP_ACCESS_CODE)"
  if [ -n "$code" ]; then
    export SMOKE_ACCESS_CODE="$code"
    log "已从 .env 读取访问码：冒烟测试将携带 X-Limitup-Code 头"
  fi
  log "运行冒烟测试：$url"
  if bash "$SCRIPT_DIR/smoke.sh" "$url" --wait 60; then
    log "冒烟测试通过"
    return 0
  fi
  warn "冒烟测试未通过。常见原因：首次拉数据需 10–20s、域名 DNS/证书未就绪、数据源暂时不可达。"
  warn "可稍后重跑：./deploy.sh smoke   （日志：./deploy.sh logs app）"
  return 1
}

tag_prev_image() {
  if dk image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    dk tag "$IMAGE_NAME" "$PREV_IMAGE" >/dev/null 2>&1 || true
    log "当前镜像已标记为 $PREV_IMAGE（供 ./deploy.sh rollback 回退）"
  fi
}

print_access() {
  local domain site
  domain="$(env_get DOMAIN)"
  site="$(env_get SITE_ADDRESS)"
  printf '\n'
  log "部署完成"
  if [ -n "$domain" ]; then
    printf '  访问地址：https://%s\n' "$domain"
    printf '  HTTPS：由 Caddy 自动申请/续期（首次约 10–60s；失败请查 ./deploy.sh logs caddy）\n'
  else
    printf '  访问地址：http://<服务器公网IP>:%s/  （未配置域名，无 HTTPS）\n' "$(http_port)"
    printf '  配置域名：编辑 %s 的 DOMAIN=... 后执行 ./deploy.sh up\n' "$ENV_FILE"
  fi
  printf '  反向代理站点地址：%s\n' "${site:-:80}"
  printf '  数据卷：app-data（CSV 快照写在容器 /app/data）\n'
  if [ -n "$(env_get LIMITUP_ACCESS_CODE)" ]; then
    printf '  访问码已开启：首次访问在网址后加 ?code=<你的访问码>（之后 Cookie 记住 30 天）\n'
  fi
  printf '  常用命令：./deploy.sh status | logs | restart | rollback | smoke | stop\n'
  printf '  安全提示：公网只暴露 80/443；如需访问码/限流，改 .env 后 ./deploy.sh restart\n'
}

# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
cmd_init() {
  ensure_env
  ensure_docker
  log ".env 与 Docker 已就绪；执行 ./deploy.sh up 开始部署"
}

cmd_up() {
  ensure_env
  ensure_docker
  log "构建镜像 $IMAGE_NAME（首次约 3–8 分钟）..."
  tag_prev_image
  compose build
  compose up -d --remove-orphans
  wait_healthy "$HEALTH_TIMEOUT"
  if ! run_smoke "$(smoke_url)"; then
    exit 1
  fi
  print_access
}

cmd_restart() {
  ensure_env
  ensure_docker
  log "按当前 .env 重建并重启容器..."
  compose up -d --force-recreate --remove-orphans
  wait_healthy "$HEALTH_TIMEOUT"
  run_smoke "$(smoke_url)" || exit 1
}

cmd_rollback() {
  ensure_env
  ensure_docker
  dk image inspect "$PREV_IMAGE" >/dev/null 2>&1 || die "没有可回退的镜像（$PREV_IMAGE）。只有执行过至少两次 ./deploy.sh up 才会产生。"
  log "回退镜像：$PREV_IMAGE -> $IMAGE_NAME"
  dk tag "$PREV_IMAGE" "$IMAGE_NAME"
  compose up -d --force-recreate --remove-orphans
  wait_healthy "$HEALTH_TIMEOUT"
  run_smoke "$(smoke_url)" || warn "回退后冒烟未通过，请检查 ./deploy.sh logs"
  log "回退完成。注意：确认问题修复后再 ./deploy.sh up 升级（会重新构建并覆盖 prev 标记）。"
}

cmd_stop() {
  ensure_env
  ensure_docker
  log "停止容器（保留数据卷）..."
  compose stop
  log "已停止。启动：./deploy.sh up"
}

cmd_down() {
  ensure_env
  ensure_docker
  log "停止并删除容器/网络（保留数据卷 app-data）..."
  compose down --remove-orphans
}

cmd_purge() {
  ensure_env
  ensure_docker
  log "删除容器 + 数据卷（data/CSV 快照将永久丢失）..."
  compose down -v --remove-orphans
  log "已清空。"
}

cmd_status() {
  ensure_env
  ensure_docker
  compose ps
  printf '\n'
  local url
  url="$(smoke_url)"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 10 "$url/api/health" >/dev/null 2>&1; then
      log "外网探活正常：$url/api/health"
    else
      warn "外网探活失败：$url/api/health（容器未启动 / 域名证书未就绪 / 防火墙未放行）"
    fi
  fi
  printf '\n数据卷：\n'
  dk volume ls 2>/dev/null | grep -E 'app-data|caddy' || true
}

cmd_logs() {
  ensure_env
  ensure_docker
  local svc="${1:-}"
  if [ -n "$svc" ]; then
    compose logs -f --tail=200 "$svc"
  else
    compose logs -f --tail=200
  fi
}

cmd_smoke() {
  ensure_env
  run_smoke "$(smoke_url)"
}

cmd_config() {
  ensure_env
  compose config
}

cmd_deploy() {
  local remote="${1:-}"
  [ -n "$remote" ] || die "用法：./deploy.sh deploy <user@host>（例如 deploy ubuntu@1.2.3.4）"
  need_cmd rsync
  need_cmd ssh
  [ -f "$PROJECT_DIR/app.py" ] || die "项目根目录缺少 app.py：$PROJECT_DIR"
  log "同步源码 -> ${remote}:~/${REMOTE_DIR}（排除 .venv/data/.env 等）"
  rsync -az --delete \
    --exclude '.venv/' \
    --exclude 'data/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude '.git/' \
    --exclude 'screenshot.png' \
    --exclude 'deploy/.env' \
    --exclude '.DS_Store' \
    --exclude '*.log' \
    --exclude '*.pyc' \
    "$PROJECT_DIR/" "${remote}:${REMOTE_DIR}/"
  log "远程安装/构建/启动（首次会安装 Docker 并构建镜像，约 3–8 分钟）..."
  ssh "$remote" "cd '$REMOTE_DIR' && chmod +x deploy/deploy.sh deploy/smoke.sh && ./deploy/deploy.sh up"
  log "远程部署完成：请把域名解析到该服务器，并在服务器上编辑 $REMOTE_DIR/deploy/.env 的 DOMAIN 后重跑 ./deploy.sh up"
}

usage() {
  sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
  local cmd="${1:-help}"
  [ "$#" -gt 0 ] && shift
  case "$cmd" in
    up|install|update|start) cmd_up ;;
    deploy)                 cmd_deploy "${1:-}" ;;
    rollback)               cmd_rollback ;;
    restart)                cmd_restart ;;
    stop)                   cmd_stop ;;
    down)                   cmd_down ;;
    purge)
      if [ "${1:-}" = "--yes" ] || [ "${FORCE:-0}" = "1" ]; then
        cmd_purge
      else
        die "purge 会删除数据卷（不可恢复）。确认请执行：./deploy.sh purge --yes"
      fi
      ;;
    logs)                   cmd_logs "${1:-}" ;;
    status|ps)              cmd_status ;;
    smoke)                  cmd_smoke ;;
    config)                 cmd_config ;;
    init)                   cmd_init ;;
    help|-h|--help)         usage ;;
    *)                      usage; die "未知命令：$cmd" ;;
  esac
}

main "$@"
