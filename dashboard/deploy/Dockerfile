# syntax=docker/dockerfile:1
# =============================================================================
# A股涨停直播看板 — 生产镜像
#
# 构建上下文 = 项目根目录（docker-compose.yml 已配置 context: ..）：
#   cd deploy && docker compose build
# 或手动：
#   docker build -f deploy/Dockerfile -t ashare-limitup:local .
#
# 设计说明：
#   * 只复制 app.py / page.html / requirements.txt；.venv、data/、screenshot 不进镜像
#   * 非 root 用户（uid/gid 10001）运行；运行数据写入 /app/data（compose 挂命名卷）
#   * 单进程运行：app.py 自带后台刷新线程 + akshare 全局节流，**禁止横向扩多副本**
#   * 国内构建加速（可选）：--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
# =============================================================================
FROM python:3.12-slim AS runtime

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    HOST=0.0.0.0 \
    PORT=8000

# tzdata：app.py 用 zoneinfo("Asia/Shanghai")，slim 镜像默认没有时区数据库
# ca-certificates：akshare 走 HTTPS 数据源（东方财富 / 新浪）
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates passwd \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
 && echo "${TZ}" > /etc/timezone

WORKDIR /app

# 先装依赖：requirements.txt 不变时可复用层缓存
COPY requirements.txt ./
RUN pip install --index-url "${PIP_INDEX_URL}" -r requirements.txt

# 非 root 运行用户；/app/data 归属 app，命名卷首次创建时会继承该属主
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --home-dir /app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/data \
 && chown -R app:app /app

COPY --chown=app:app app.py page.html ./

VOLUME ["/app/data"]
USER app

EXPOSE 8000

# 健康检查：/api/health 只读内存，不触发 akshare 拉取
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/api/health', timeout=4)"]

# 保持单进程：app.py 内部已启动刷新线程，多 worker 会重复拉取 akshare（见 DEPLOY.md §1）
CMD ["python", "app.py"]
