# 腾讯云服务器部署

适用于安装 Docker Engine 和 Docker Compose v2 的 Linux CVM 或轻量应用服务器。
请上传当前工作目录的源码，包含本次修改过的中文界面；上游仓库尚未包含这些本地修改。

## 默认方式：仅自己访问

在服务器的项目根目录运行：

```sh
sh deploy/tencent-cloud/up.sh
```

脚本会构建 React 界面和 Python 后端镜像，启动服务并等待健康检查通过。
编辑器使用非 root 用户运行，文件系统只读，文档和配置保存在独立数据卷中。
服务仅映射服务器的 `127.0.0.1:8770`，无需在腾讯云安全组开放 8770。

在自己的电脑上建立 SSH 隧道，用户名、服务器地址和 SSH 端口按实际情况替换：

```sh
ssh -N -L 127.0.0.1:8870:127.0.0.1:8770 -p 22 用户名@服务器地址
```

然后在自己的浏览器访问 `http://127.0.0.1:8870`。关闭 SSH 连接会关闭隧道，服务器上的编辑器继续运行。
需要私钥时使用 SSH 的 `-i` 参数指定本机文件，不要把私钥上传到项目目录。

## 域名访问：HTTPS 和登录保护

先确认域名已解析到服务器，腾讯云安全组允许 TCP 80 和 443，且这两个端口尚未被其他网站占用。
域名在中国内地服务器上的备案要求需按腾讯云和域名的实际情况处理。

1. 复制 `deploy/tencent-cloud/.env.example` 为同目录的 `.env`，设置 `PEDIT_DOMAIN` 和 `PEDIT_USERNAME`。
2. 在服务器上运行以下命令，在交互提示中输入登录密码，并将输出的 bcrypt 哈希填入 `.env` 的
   `PEDIT_PASSWORD_HASH`。保留哈希外的单引号，避免美元符号被 Compose 展开。

   ```sh
   docker run --rm -it caddy:2-alpine caddy hash-password
   ```

3. 运行：

   ```sh
   sh deploy/tencent-cloud/up.sh https
   ```

Caddy 会自动申请 HTTPS 证书，所有页面、API、WebSocket 和 MCP 均经过登录保护。
浏览器会先要求输入用户名和密码。相同登录凭据的访问者会操作同一个编辑会话；本项目未提供多用户隔离。

如果服务器已有 Nginx、宝塔或其他代理，请复用现有网站配置，转发到 `127.0.0.1:8770` 并增加登录保护。
不要为了启动这份配置而停用已有网站。

## 文档、密钥和可选 AI 能力

- 在编辑器中将文档保存到 `/data`，素材和模型配置也保存在该数据卷中。
  未保存的内存文档不会在进程重启后恢复。
- 大模型密钥由用户在中文界面的“助手”和“AI 模型”设置中输入，保存在数据卷中，不打包进镜像。
- 默认构建包含聊天 SDK 和基础修图能力。需要 CPU 抠图和 OpenCV 时，将 `.env` 中的
  `PEDIT_EXTRAS` 改为 `chat,ai`，再次运行启动脚本；抠图模型会在首次使用时下载。
- 镜像未内置 Claude Code 和 ComfyUI。在线模型可以单独配置，无需安装 GPU 环境。
- 可通过 `PEDIT_PORT` 修改服务器本机的映射端口，相应调整 SSH 隧道目标端口。

## 查看状态、更新和停止

在 `deploy/tencent-cloud` 目录运行：

```sh
docker compose ps
docker compose logs --tail=100 editor
```

上传新源码后，再次运行启动脚本即可重新构建和更新。更新前先备份数据卷。
HTTPS 模式更新时仍使用 `sh deploy/tencent-cloud/up.sh https`。

正式发布时，应将每个版本的源码分别保存在 `/opt/stack/pedit/releases/<版本号>`，
设置对应的 `PEDIT_IMAGE_TAG`，并保留上一版本的源码、配置和镜像。Compose 项目名始终为
`pedit`，使用独立的 `pedit_pedit_data` 数据卷，不操作其他项目的服务或卷。

回滚时进入上一版本的 `deploy/tencent-cloud` 目录，确认其 `.env` 指向上一版本镜像，运行：

```sh
docker compose -f compose.yaml -f compose.https.yaml up -d --no-build --wait
```

此命令复用已保存的旧镜像和现有数据卷，不重新构建，也不删除数据。
如果新版已经改变文档格式，先从更新前的备份恢复到兼容版本；不要覆盖唯一的数据副本。

停止服务时，私有模式运行 `docker compose down`；HTTPS 模式运行
`docker compose -f compose.yaml -f compose.https.yaml down`。
不要加 `-v`，否则会删除文档、密钥和 HTTPS 证书所在的数据卷。
