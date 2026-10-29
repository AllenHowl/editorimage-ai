# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
# 在带 HTTPS 代理的构建环境中，可临时挂载公共 CA；普通服务器无需提供。
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --strict-ssl=true
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/data \
    PEDIT_HOME=/data/.pedit \
    PEDIT_SEGNALAZIONI=/data/SEGNALAZIONI.md
WORKDIR /app
# 安装中文字体；代理证书仅在构建步骤中使用，不写入镜像。
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      printf 'Acquire::https::CaInfo "/run/secrets/proxy_ca";\n' > /etc/apt/apt.conf.d/99pedit-build-ca; \
    fi; \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates fonts-dejavu-core fonts-noto-cjk && \
    rm -f /etc/apt/apt.conf.d/99pedit-build-ca && \
    rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE ./
COPY backend/ backend/
COPY --from=frontend /build/frontend/dist/ backend/pedit/webui/
# 默认安装聊天 SDK；需要 CPU 抠图及 OpenCV 时，构建参数可设为 chat,ai。
ARG PEDIT_EXTRAS=chat
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install ".[${PEDIT_EXTRAS}]"
RUN groupadd --gid 10001 pedit && \
    useradd --uid 10001 --gid 10001 --home-dir /data --no-create-home pedit && \
    mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/api/project', timeout=3).read()"
CMD ["pedit", "ui", "--host", "0.0.0.0", "--port", "8770", "--no-browser"]
