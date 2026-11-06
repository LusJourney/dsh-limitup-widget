# A股涨停直播看板 — 公网部署指南（DEPLOY）

把本项目的 Flask 页面安全地发布到公网，给其他人用浏览器打开。所有部署文件都在 `deploy/` 目录。

> 项目本体（`app.py` / `page.html` / `tests/` 等）的运行方式见仓库根目录 `README.md`，本文件只讲**部署**。

---

## 0. 快速开始（TL;DR）

已有一台 Ubuntu 22.04/24.04 云服务器（2 核 2G 起），且本地已装 Docker 的话：

```bash
# 本地项目根目录执行：把源码推上服务器并远程部署（首次会自动装 Docker + 构建镜像）
cd deploy
cp .env.example .env        # 可选：先填 DOMAIN（域名）与可选的访问码
./deploy.sh deploy ubuntu@<服务器公网IP>
```

或者**直接在服务器上**操作：

```bash
# 服务器上执行（代码已在服务器）
cd ashare-limitup-tracker/deploy
cp .env.example .env
./deploy.sh up
```

验收：`./deploy.sh smoke`（健康检查 + 快照 JSON + 页面 HTML 全绿）。浏览器打开 `http://<公网IP>/`。

---

## 1. 架构与运行要求

```
浏览器 --80/443--> [Caddy 容器] --8000--> [app 容器: Flask + akshare] --HTTPS--> 东方财富 / 新浪
                        │                          │
                  自动 TLS 证书              data/ 命名卷（CSV 快照、交易日历、概念缓存）
```

| 组件 | 说明 |
|---|---|
| `app` | `python:3.12-slim` + 本项目，**单实例**；`/app/data` 挂命名卷 `app-data`；非 root（uid 10001）运行 |
| `caddy` | 反向代理 + 自动 HTTPS（有域名时）；gzip/zstd；安全响应头；默认只暴露 80/443 |
| `nginx`（可选） | Caddy 的替代方案，默认不启动（见 §9） |

**硬性要求**

- **不要多副本**：`app.py` 进程内有后台刷新线程和 akshare 全局节流，`--scale app=N` 或多机部署会重复拉取数据源，容易触发限流。单实例足够几十人同时浏览（Flask `threaded=True`）。
- **服务器配置**：2 核 2G 起步（pandas/akshare 内存峰值较高）；1G 实例务必开 2G swap（`cloud-init.yaml` 已包含）。磁盘 20G 起。
- **出网**：服务器需能访问东方财富、新浪等数据源（国内云直连最稳；海外机器访问东财可能慢/被限流，见 §4）。
- **公网端口**：只公开 22 / 80 / 443；**不要**把容器 8000 端口直接映射到公网。
- **时间**：容器内 `TZ=Asia/Shanghai`（镜像已装 tzdata），页面与刷新窗口都按北京时间。

首次启动：HTTP 服务立即就绪，涨停/炸板等主数据约 10–20 秒出第一版快照；概念映射后台慢慢建（当天二次启动命中缓存）。页面无数据时显示"暂无数据"，不会编造。

---

## 2. 三条路线对比

| 维度 | ① 国内轻量云 + Docker + Caddy（**推荐**） | ② 海外免费 PaaS（Render/Fly/Railway） | ③ GitHub Pages 纯静态快照 |
|---|---|---|---|
| 数据实时性 | **实时**（60s 刷新；数据源直连） | 实时但受海外出口影响，易慢/被限 | **延迟 5–15 分钟**（Actions cron 最短 5 分钟） |
| 稳定性 | 高（自己掌控，故障可排查） | 中低（免费档休眠、512MB 内存易 OOM、无状态） | 高（静态托管稳）但数据可能长时间不更新 |
| 成本 | 约 ¥100–500/年（见 §11） | 免费档：¥0；可用档约 $5–7/月 | ¥0 |
| HTTPS/域名 | Caddy 自动证书，配置最简单 | 平台自带 HTTPS | 平台自带 HTTPS |
| 国内备案 | **绑定域名 + 国内服务器需 ICP 备案** | 无需备案（域名解析到海外） | 无需备案（但国内访问 GitHub Pages 不稳定） |
| 交互功能 | 全部（自选行情、手动刷新、告警） | 全部（受休眠影响） | 仅展示（轮询、自选、告警不可用） |
| 运维 | 低（`deploy.sh` 全覆盖） | 低（改 Dockerfile 即可） | 中（要有 Actions 抓数 + 提交逻辑） |
| 适用 | 正式对外、长期使用 | 临时演示、海外用户为主 | 只读大屏/存档，不能当实时方案 |

**结论**：要"别人能稳定打开、数据是实时 A 股涨停"，选路线①。只为看一眼效果、不想买服务器，可试路线②（但免费档内存不够，多半还得付费）。路线③只适合存档/演示，**不要**当实时看板对外宣传。

---

## 3. 路线①：国内轻量云 + Docker + Caddy（推荐）

### 3.1 买机器 + 开放端口

1. 阿里云/腾讯云「轻量应用服务器」选 **Ubuntu 22.04 LTS** 或 **24.04 LTS**，规格 **2 核 2G / 3–5Mbps / 40G 系统盘**。
2. 控制台**防火墙/安全组**放行：`22`（SSH）、`80`（HTTP）、`443`（HTTPS）。
   > 注意：云厂商的安全组优先于机器内 ufw，两处都要放行。
3. 用 SSH 密钥登录（不要用密码登录 root；见 §8）。

### 3.2 方式 A：cloud-init 一键初始化（可选）

购买/重装系统时，在「用户数据 / 自定义数据」粘贴 `deploy/cloud-init.yaml`（把里面 `GIT_REPO` 换成你的仓库地址；没有仓库就留着占位符）。

它会在首次开机完成：装 Docker + compose 插件 → 配 ufw（22/80/443）→ 开 2G swap → 拉代码 → `deploy/deploy.sh up`。
日志：`/var/log/limitup-bootstrap.log`。首次约 3–6 分钟。

若没配 `GIT_REPO`，开机后按 3.4 从本地推代码即可。

### 3.3 方式 B：服务器上手动部署（逐步命令）

```bash
# 1) 登录服务器
ssh ubuntu@<公网IP>

# 2) 装 Docker + compose 插件（deploy.sh 也会自动装；先装可省时间）
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"     # 之后需重新登录 SSH 才生效

# 3) 拿到代码（二选一）
#    a. 有仓库：
git clone <你的仓库地址> ~/ashare-limitup-tracker
#    b. 没有仓库：在本地执行 §3.4 的 deploy.sh deploy 推送

# 4) 部署（自动构建 + 启动 + 健康检查 + 冒烟测试）
cd ~/ashare-limitup-tracker/deploy
cp .env.example .env
./deploy.sh up
```

首次构建约 3–8 分钟（akshare 依赖较大）。成功后：

```bash
./deploy.sh status        # 容器状态 + 外网探活
./deploy.sh logs app      # 应用日志（akshare 拉取/刷新）
curl http://127.0.0.1/api/health
```

浏览器打开 `http://<公网IP>/`。

### 3.4 本地推送到服务器（`deploy.sh deploy`）

本地项目根目录（需本机有 `ssh`、`rsync`）：

```bash
cd deploy
cp .env.example .env          # 可选，先填 DOMAIN / 访问码
REMOTE_DIR=ashare-limitup-tracker ./deploy.sh deploy ubuntu@<公网IP>
```

做的事情：`rsync` 同步源码（排除 `.venv/`、`data/`、`deploy/.env` 等敏感/运行时文件）→ SSH 远程执行 `deploy/deploy.sh up`。
目标目录默认 `~/ashare-limitup-tracker`（相对登录用户家目录，无需 sudo）；可用 `REMOTE_DIR=/opt/xxx` 覆盖（此时远端目录需当前用户可写）。

> 密钥安全：`.env` 不会被同步（含访问码/域名），需要在服务器上单独配置一次。修改源码后重新执行同一条命令即可增量更新（数据卷不受影响）。

### 3.5 域名 + HTTPS（Caddy 自动证书）

1. 域名 DNS 添加 **A 记录** 指向服务器公网 IP（如 `limitup.example.com`）。
2. 等 DNS 生效（`dig +short limitup.example.com` 返回正确 IP）。
3. 服务器上编辑 `deploy/.env`：

   ```ini
   DOMAIN=limitup.example.com
   ```

4. 重新部署（`deploy.sh` 会自动把 `SITE_ADDRESS` 推导成 `https://limitup.example.com`）：

   ```bash
   cd ~/ashare-limitup-tracker/deploy
   ./deploy.sh up
   ```

5. 验证：

   ```bash
   curl -I https://limitup.example.com/          # 200 + text/html
   ./deploy.sh smoke
   ```

Caddy 会自动向 Let's Encrypt 申请证书并续期（无需 certbot）。首次签发通常 10–60 秒；失败看 `./deploy.sh logs caddy`（常见原因：DNS 未生效、80/443 未放行、备案被拦截）。

### 3.6 备案说明（中国大陆服务器）

- 使用**国内服务器 + 域名 + 80/443** 对外提供网站：必须先完成 **ICP 备案**（个人可备，通常 1–3 周）。未备案域名访问 80/443 会被云厂商拦截。
- 只有 IP、没有域名：多数厂商允许 IP 直接访问（政策以云厂商为准）；仍建议尽快备案。
- 用**香港/海外服务器**：免备案，但访问东方财富数据源可能更慢/不稳定，且国内用户访问该服务器本身也慢。
- 备案不涉及本项目的技术改动；备案通过后按 §3.5 配域名即可。

---

## 4. 路线②：海外免费 PaaS（Render / Fly.io / Railway）

思路：平台直接构建本目录 `deploy/Dockerfile`，平台注入 `PORT`、要求监听 `0.0.0.0`——本项目已支持（`HOST`/`PORT` 环境变量）。

| 平台 | 免费档现实情况 | 建议 |
|---|---|---|
| **Render** | Free Web Service：512MB 内存、**15 分钟无访问会休眠**（冷启动 ~1 分钟）；akshare 依赖 pandas，512MB 有 OOM 风险 | 先免费档试；能跑则用，频繁 OOM 就升 Starter（约 $7/月，512MB→可调） |
| **Fly.io** | 免费额度政策变动频繁；默认共享 CPU 256MB 起，需加内存 | 至少 512MB–1GB；卷需付费；以官网为准 |
| **Railway** | 一次性试用额度 / 按量计费（约 $5/月起） | 只适合短期演示 |

注意：

1. **无状态**：PaaS 不保证本地磁盘持久，`/app/data` 的 CSV 快照会随重启丢失（页面功能不受影响，备份用途需挂持久卷或对象存储）。
2. **数据源风险**：海外出口访问东方财富接口可能慢、超时或被限流；表现为页面 `stale` 红色横幅、`trade_date` 旧。这是海外部署的固有短板，不是程序 bug。
3. **单实例**：Render/Fly 都要把实例数设为 1。
4. **HTTPS/域名**：平台自带；绑定自定义域名按其文档操作，无需备案（域名解析到海外）。
5. 配置环境变量：`HOST=0.0.0.0`、`TZ=Asia/Shanghai`，`PORT` 用平台注入值（不要写死；`app.py` 已读取 `PORT`）。

**不推荐**把免费 PaaS 当正式对外方案：休眠 + OOM + 数据源延迟会直接影响"实时看板"的体验。

---

## 5. 路线③：GitHub Pages 纯静态快照（非实时，仅存档/演示）

思路：GitHub Actions 定时（cron 最短每 5 分钟）在海外 runner 上用 akshare 抓 `/api/snapshot` 同构数据 → 生成 `snapshot.json` → 提交到 `gh-pages` 分支 → 静态页面直接 fetch 该 JSON。

- **延迟**：5–15 分钟（cron 调度本身可能延迟数分钟），不是实时看板。
- **功能缺失**：自选实时行情、手动 `/api/refresh`、浏览器告警都不可用；只能展示最近一份快照。
- **数据源**：runner 在海外，akshare 访问东财可能偶发失败，需要失败重试 + 保留上一份快照。
- **成本**：公开仓库 Actions 免费（私有仓库每月 2000 分钟额度）；Pages 托管免费。
- **实现要点**（如果你要做）：
  1. 复用 `app.py` 的数据函数（`python -c "import app; ..."`）或直接调 akshare 生成同结构 JSON；
  2. `actions/setup-python` + `pip install -r requirements.txt` + `python scripts/dump_snapshot.py > snapshot.json`；
  3. `peaceiris/actions-gh-pages`（或 `actions/deploy-pages`）发布 `snapshot.json` + 一个精简版 `index.html`；
  4. 页面轮询 `snapshot.json`，标注"数据延迟 N 分钟（静态快照）"。

**结论**：只适合"每天看一眼历史形态"的存档场景，**不能**对外声称实时。

---

## 6. 配置项（`deploy/.env`）

从 `deploy/.env.example` 复制。**`.env` 不要提交到 git**（`deploy/.gitignore` 已忽略），权限保持 `600`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 容器内监听地址（Docker 部署必须 0.0.0.0，否则代理访问不到） |
| `PORT` | `8000` | 容器内端口（compose 内固定映射，勿改） |
| `TZ` | `Asia/Shanghai` | 时区；页面/刷新窗口按北京时间 |
| `DOMAIN` | 空 | 访问域名。留空 = 仅 `http://<IP>/` 明文；填写 = Caddy 自动 HTTPS |
| `SITE_ADDRESS` | `:80` | 反向代理站点地址，**由 `deploy.sh` 按 `DOMAIN` 自动推导**，一般无需手改 |
| `HTTP_PORT` / `HTTPS_PORT` | `80` / `443` | 宿主机映射端口（一般不改；改了要同步云安全组/防火墙） |
| `LIMITUP_ACCESS_CODE` | 空 | **可选**：访问码。设置后首次用 `http://<域名>/?code=访问码` 打开，随后写 30 天 HttpOnly Cookie；`/api/health` 始终放行（供健康检查） |
| `LIMITUP_RATE_LIMIT` | 空 | **可选**：每 IP 每分钟请求上限（0/留空 = 不限）。启用时需同时保留 `LIMITUP_TRUST_PROXY=1`，否则会把所有访客当成同一个反代 IP |
| `LIMITUP_TRUST_PROXY` | `1`（compose 注入） | 信任 Caddy/nginx 注入的 `X-Forwarded-For` 取真实客户端 IP。仅当 app 未被直接暴露公网时安全 |
| `LIMITUP_REFRESH_COOLDOWN` | `10`（compose 注入） | `/api/refresh` 每 IP 冷却秒数，0 = 关闭。防止刷新按钮被连点刷爆数据源 |

> 可选加固说明：以上变量由主程序实现（**当前仓库 app.py 已支持**；更旧版本会忽略，不报错）。默认全部关闭/不改变行为；不清楚时保持默认即可，也可用 Caddy/nginx 层手段（§8）。

生效方式：改完 `.env` 执行 `./deploy.sh restart`（会按新配置重建容器）。

---

## 7. 运维手册（`deploy/deploy.sh`）

| 命令 | 作用 |
|---|---|
| `./deploy.sh up` | 装 Docker（如缺）→ 生成 `.env` → 构建镜像 → 启动 → 健康检查 → 冒烟测试。幂等，可重复执行 |
| `./deploy.sh deploy user@host` | 本地 rsync 源码到服务器并远程执行 `up` |
| `./deploy.sh restart` | 按当前 `.env` 重建容器（改环境变量后用） |
| `./deploy.sh rollback` | 回退到上一个镜像（需至少成功 `up` 过两次） |
| `./deploy.sh status` | `compose ps` + 外网 `/api/health` 探活 + 数据卷列表 |
| `./deploy.sh logs [app\|caddy\|nginx]` | 跟踪日志 |
| `./deploy.sh smoke` | 只跑冒烟测试（健康检查 / 快照 JSON / 页面 HTML） |
| `./deploy.sh config` | 打印 compose 最终解析配置（排查变量/端口） |
| `./deploy.sh stop` / `down` | 停止容器（`down` 删容器/网络，**保留数据卷**） |
| `./deploy.sh purge --yes` | **危险**：删容器 + 数据卷（CSV 数据全丢） |

**备份数据卷**（CSV 快照 / 缓存，可选）：

```bash
cd ~/ashare-limitup-tracker/deploy
VOL=$(docker volume ls --format '{{.Name}}' | grep 'app-data' | head -n1)
docker run --rm -v "$VOL":/data -v "$PWD":/backup alpine \
  sh -c 'tar czf /backup/limitup-data-$(date +%F).tgz -C /data .'
```

**恢复**：把 `.tgz` 解回同名卷（先 `docker compose stop app`，解压后 `./deploy.sh up`）。

**升级代码**：本地改完 → `./deploy.sh deploy user@host`（或服务器上 `git pull` 后 `./deploy.sh up`）。数据卷保留。
**升级依赖**：`docker compose build --no-cache`（akshare 接口常变，遇到取数异常优先重建镜像拿最新版）。
**卸载**：`./deploy.sh down` 保留数据；彻底删除用 `./deploy.sh purge --yes`。

---

## 8. 安全建议（公开访问前务必过一遍）

本页面是**只读展示**，但接口仍可能被滥用，逐项检查：

1. **不要暴露 8000 到公网**：compose 只把 Caddy 的 80/443 映射到宿主机；app 的 8000 仅容器内网。不要额外 `-p 8000:8000`。
2. **访问码**：`LIMITUP_ACCESS_CODE=你的访问码` 可挡住随机访问（首次带 `?code=` 打开，之后 Cookie 记住；API 也可带 `X-Limitup-Code` 头）。注意 `/api/health` 永远放行（监控需要）。也可叠加 Caddy `basic_auth` / nginx `auth_basic`（见注释）。
3. **限流**：`LIMITUP_RATE_LIMIT=120`（每 IP 每分钟上限）启用后**必须同时保留 `LIMITUP_TRUST_PROXY=1`**（compose 已默认注入），否则限流会退化成"所有访客共用一个名额"。也可用 nginx `limit_req`（见注释）。
4. **限制 `/api/refresh`**：该接口会立即触发一次 akshare 拉取，被刷会打爆免费数据源。程序层默认有每 IP 10 秒冷却（`LIMITUP_REFRESH_COOLDOWN`）；公开部署建议直接在代理层 403（`Caddyfile` 有注释示例），代价是页面"刷新"按钮不可用（自动刷新不受影响）。
5. **容器加固**：镜像非 root（uid 10001）、`no-new-privileges`、依赖最小化；只挂载数据卷，不挂 docker.sock。
6. **主机加固**：仅 SSH 密钥登录、禁用 root 密码登录、`ufw` 只放行 22/80/443、开启自动安全更新；`.env` 权限 `600`。
7. **不放密钥**：仓库里没有任何 key/token；`.env` 只存域名/访问码，不要提交。
8. **日志**：Caddy/nginx 默认记录访问 IP；若对公众开放，按当地法规评估日志保留/告知义务。
9. **单实例**：不要多副本（见 §1），既是稳定性也是数据源礼貌。
10. **合规**：见 §12（法律与免责声明），公开页面上建议注明数据来源与"非投资建议"。

---

## 9. 用 nginx 替代 Caddy（可选）

Caddy 是默认方案（自动 HTTPS 最省事）。若你已有 nginx 习惯/证书体系：

```bash
cd ~/ashare-limitup-tracker/deploy
docker compose stop caddy
docker compose --profile nginx up -d nginx
./deploy.sh smoke
```

- 反向代理 / gzip / 安全头 / `proxy_read_timeout 120s` 已配好（`nginx.conf`）。
- **HTTPS**：nginx 不自动签证书。用 certbot 或云厂商证书，填 `nginx.conf` 末尾注释的 443 server 段并把证书挂进容器。
- **Basic Auth**：
  ```bash
  docker run --rm httpd:alpine htpasswd -nbB admin 'your-password' > .htpasswd
  # 然后取消 docker-compose.yml 里 .htpasswd 挂载 + nginx.conf 里 auth_basic 两行注释
  docker compose --profile nginx up -d nginx
  ```
- 切回 Caddy：`docker compose --profile nginx down`（或 `stop nginx`）后 `docker compose up -d caddy`。

---

## 10. 故障排查

先看三样：`./deploy.sh status`、`./deploy.sh logs app`、`./deploy.sh logs caddy`。

| 现象 | 排查 / 解决 |
|---|---|
| `docker compose` 报 command not found | 缺 compose 插件：`sudo apt install docker-compose-plugin`；或重新登录让 docker 组生效 |
| `permission denied ... docker.sock` | 当前用户不在 docker 组：`sudo usermod -aG docker $USER` 后**重新登录 SSH** |
| 容器一直 `starting`/`unhealthy` | `logs app` 看启动异常；`docker inspect` 看 healthcheck 输出；确认 `HOST=0.0.0.0`、端口 8000 没被改坏 |
| 页面 502 / 504 | app 未 healthy 或反代超时。首次拉数需 10–20s，等 30s 再试；`/api/refresh` 最长 30s，`proxy_read_timeout` 已设 120s；查 `logs app` 是否在拉数 |
| `/api/health` 200 但页面无数据 | 数据源失败：`logs app` 有 `akshare` 报错；页面应显示红色 stale 横幅。盘前/非交易日回退到上一交易日是正常行为（`is_fallback=true`） |
| akshare 拉不动 | ① 容器 DNS：`docker compose exec app getent hosts push2.eastmoney.com`；② 服务器出网/防火墙；③ akshare 版本旧/东财接口改版：`docker compose build --no-cache` 重建；④ 海外服务器被限流：换国内机器或稍后重试 |
| 时间不对（差 8 小时） | 容器内应为 CST：`docker compose exec app date`；确认 `TZ=Asia/Shanghai` 且镜像含 tzdata（本镜像已装），改 `.env` 后 `./deploy.sh restart` |
| 80/443 被占用 | `sudo ss -lntp \| grep -E ':(80\|443)'`，停掉占用的 nginx/apache/面板；或改 `.env` 的 `HTTP_PORT`/`HTTPS_PORT` 并同步安全组 |
| 域名打不开/证书失败 | `dig +short <域名>` 是否指向本机；安全组 80/443 是否放行；国内是否完成备案；`logs caddy` 看 ACME 报错；DNS 刚改可等 10 分钟再 `./deploy.sh restart` |
| 国内 IP 访问超时 | 云厂商安全组没放行；或未备案被拦截（换 IP+端口访问或先备案） |
| 容器 OOMKilled | `docker inspect -f '{{.State.OOMKilled}}' <容器>`；升 2C2G 或加 swap（cloud-init 已带 2G swap） |
| 磁盘满 | `data/` 每天增长约几百 KB–1MB：定期清理/备份旧 CSV；`docker system prune -f` 清理未用镜像；日志已设 10MB×3 轮转 |
| 重建后 CSV 没了 | 用了 `docker compose down -v` 或手删卷。正常 `down`/`up` 不丢；重要数据按 §7 备份 |
| 冒烟测试 watchlist 为空 | 首次拉自选行情需 20–40s，稍后重跑 `./deploy.sh smoke` |
| 页面提示 401（需要访问码） | 用 `http://<域名>/?code=你的访问码` 打开一次（会写 30 天 HttpOnly Cookie）；API 可带 `X-Limitup-Code` 头；确认 `.env` 的 `LIMITUP_ACCESS_CODE` 与输入一致；`./deploy.sh smoke` 会自动读取 `.env` 里的访问码 |
| 页面/接口提示 429（请求过于频繁） | 触发了 `LIMITUP_RATE_LIMIT` 或 `/api/refresh` 冷却：调大/清空 `.env` 后 `./deploy.sh restart`；限流场景务必确认 `LIMITUP_TRUST_PROXY=1` |
| 页面提示 401/403（Basic Auth） | 启用了 Caddy `basic_auth` / nginx `auth_basic`：输入代理层账号密码；或改配置关闭后重启 |
| 改了 `.env` 不生效 | 必须 `./deploy.sh restart`（重建容器）才会重新注入环境变量 |
| Caddy 反复重启 / 日志报 `site address is empty` | `SITE_ADDRESS` 为空才会出现（它不是手填项）：直接 `./deploy.sh up` 会自动按 `DOMAIN` 推导；或删掉 `.env` 重新生成 |
| `./deploy.sh` 像卡住 | 脚本对 `docker info` 探测有 15s 超时；Mac 需先启动 Docker Desktop，Linux 查 `sudo systemctl status docker`。`./deploy.sh config` 不需要 daemon，可先跑它确认配置 |

---

## 11. 成本估算

国内轻量云（路线①，以 2026 年常见价格区间估算，**以各家官网为准**）：

| 项目 | 费用 |
|---|---|
| 轻量应用服务器 2C2G（阿里云/腾讯云） | 新用户促销约 **¥99/年**；常规价约 **¥300–500/年**（按活动波动） |
| 域名（.com/.cn，可选） | 约 **¥30–90/年** |
| ICP 备案 | 免费（个人/企业自行提交，周期 1–3 周） |
| TLS 证书 | 免费（Let's Encrypt，Caddy 自动续期） |
| 流量 | 轻量套餐一般含 1–3TB/月，本页面 JSON 压缩后每次约几十 KB，够用 |
| **合计** | **约 ¥100–600/年**；无域名纯 IP 访问则只需服务器费用 |

海外 PaaS（路线②）：免费档 ¥0（但内存/休眠受限）；可用档约 **$5–7/月（≈¥450–600/年）**。
GitHub Pages（路线③）：¥0（公开仓库），私有仓库受 Actions 免费分钟数限制。

> 省钱提示：数据源完全免费、无需任何 API key；主要成本就是那台小服务器。

---

## 12. 法律与数据免责声明

- **数据来源**：本项目通过开源库 **akshare** 聚合东方财富、新浪等**公开免费接口**的数据，未使用任何付费/授权数据源。数据版权归原始数据源所有。
- **用途限制**：仅供**学习、研究、个人观察**使用，**不构成任何投资建议**。据此操作，风险自负。
- **准确性**：数据可能延迟、缺失或出错（免费接口无 SLA）；程序遵循"取不到就显示暂无数据 / 保留上一份好数据并标 stale，绝不编造"的原则，但**不保证**准确性、完整性和及时性。
- **数据源合规**：请遵守数据源服务条款，**禁止**高频抓取、批量转售或商业再分发。本项目已做单实例 + 60 秒刷新 + 全局 ≥1 秒调用节流；请不要多副本部署或关闭节流。
- **公开访问**：页面公开时应显著标注"数据来自公开接口，非投资建议，不保证准确性"。
- **备案**：中国大陆服务器 + 域名对外提供网站需 ICP 备案；未备案请勿用域名对公网提供服务。香港/海外服务器免备案，但需自行确认当地法律要求。
- **隐私**：本程序无账号体系，不收集用户信息；浏览器仅用 `localStorage` 保存自选股；反向代理会记录访问 IP（日志），对公众开放请按需评估告知/保留策略。
- **执业资质**：不得将本站用于荐股、代客理财等**证券投资咨询业务**（需相应牌照）。

---

## 13. 本次交付的验证状态（2026-11-05）

| 验证项 | 结果 |
|---|---|
| `bash -n deploy.sh` / `bash -n smoke.sh` | ✅ 语法通过 |
| `docker compose config`（`deploy.sh config`，默认 `.env` 与 `DOMAIN` 两种场景） | ✅ 配置可解析 |
| 应用实跑（项目 venv，端口 18000，空 data 目录） | ✅ 见 `/tmp/deploy-verify.log` |
| `deploy/smoke.sh` 对 18000 实跑 | ✅ health 200 / snapshot JSON 契约通过 / 页面 200 |
| 可选加固实测（venv 端口 18000：`LIMITUP_ACCESS_CODE` / `LIMITUP_RATE_LIMIT` / `LIMITUP_TRUST_PROXY`） | ✅ 无码 401、带码 200、超限 429、不同 `X-Forwarded-For` 互不影响（见 `/tmp/deploy-verify.log`） |
| `cloud-init.yaml` YAML 解析 | ✅ |
| **Docker 镜像构建 / 容器运行 / Caddy 端到端** | ⏳ **本次环境 Docker daemon 无响应，未验证**（Dockerfile 与 compose 已按本机可用 CLI 做离线校验；请在服务器上首次 `./deploy.sh up` 时确认） |

> 说明：`python:3.12-slim` 安装 akshare 依赖预计镜像约 700MB–1GB（pandas/numpy/lxml/mini-racer 等），首次构建 3–8 分钟。若 `pip install` 慢，用国内镜像：
> `docker compose build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple`
