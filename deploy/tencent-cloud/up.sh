#!/bin/sh
# 启动腾讯云上的编辑器，默认使用仅本机可访问的模式。
set -eu

deploy_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mode=${1:-private}
case "$mode" in
    private|https) ;;
    *) echo '用法：sh deploy/tencent-cloud/up.sh [private|https]' >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
    echo '仅接受 private 或 https 一个参数。' >&2
    exit 2
fi
if ! docker compose version >/dev/null 2>&1; then
    echo '请先安装 Docker Engine 和 Docker Compose v2。' >&2
    exit 1
fi
if [ ! -f "$deploy_dir/.env" ]; then
    (umask 077; cp "$deploy_dir/.env.example" "$deploy_dir/.env")
fi
set -- --project-directory "$deploy_dir" --env-file "$deploy_dir/.env" -f "$deploy_dir/compose.yaml"
if [ "$mode" = https ]; then
    set -- "$@" -f "$deploy_dir/compose.https.yaml"
fi
docker compose "$@" config --quiet
docker compose "$@" up -d --build --wait --wait-timeout 180
docker compose "$@" ps
