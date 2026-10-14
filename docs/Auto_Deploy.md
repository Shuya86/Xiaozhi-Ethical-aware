# 一键部署（本地源码全模块）

[English](./Auto_Deploy_EN.md) | 简体中文

本工具在保留当前源码修改的前提下，自动部署 MySQL、Redis、manager-api、manager-web 和 xiaozhi-server。它不会下载或覆盖上游源码，也不会修改相邻的 `xiaozhi-esp32-main` 固件目录。

## Windows 图形界面

双击仓库根目录的 `deploy.bat`。在窗口中保留默认的 8 个阶段，然后点击 **Start**。

窗口会根据屏幕设置合适的初始尺寸，底部 **Process Log** 默认保留可读高度，
并提供横向和纵向滚动条。可以拖动控制区与日志区之间的分隔条继续放大日志，
窗口拉伸后新增空间会优先分配给日志区，便于查看各进程的完整输出。

首次运行可能需要安装或下载 Docker、JDK 21、Maven、Node.js、Miniconda、Python 依赖和 SenseVoiceSmall 模型。模型文件较大，下载时间取决于网络速度。安装系统工具后若窗口提示 PATH 尚未刷新，请关闭部署窗口并重新双击 `deploy.bat`。

部署器会自动完成以下配置：

1. 启动本地 MySQL 和 Redis 容器；
2. 启动 Java 管理后端和 Vue 管理页面；
3. 创建 Python 3.10 Conda 环境并安装音频与服务依赖；
4. 下载并校验 ASR 模型文件；
5. 从本地 MySQL 安全读取 manager-api 自动生成的 `server.secret`，写入 `main/xiaozhi-server/data/.config.yaml`；
6. 自动写入当前局域网的 `server.websocket` 与 `server.ota` 参数；
7. 启动 xiaozhi-server，并逐阶段验证端口和进程状态。

完成后仍需在智控台执行一次业务配置：

1. 打开 `http://127.0.0.1:8001`，注册第一个管理员账号；
2. 在模型配置中填写实际使用的大语言模型 API 密钥；
3. 先让 ESP32 硬件连接服务器并完成智能体配置同步；随后打开
   `http://127.0.0.1:8003/text_chat` 测试文本聊天。硬件尚未上线时，
   页面可以打开，但文本聊天会保持不可用，这是预期的安全门控行为。

## 命令行

```powershell
# 完整部署
python auto_deploy.py

# 只检查/安装前置工具
python auto_deploy.py --only prereqs

# 查看状态
python auto_deploy.py --status

# 重新同步 secret、WebSocket 和 OTA 地址
python auto_deploy.py --only secret --reconfigure

# 源码修改后重启三个应用服务
python auto_deploy.py --only api,web,server --restart

# 停止由部署器启动的服务
python auto_deploy.py --stop
```

## 日常一键启动

首次完整部署成功后，Windows 可以直接双击仓库根目录的
`run_xiaozhi-server.bat`。它会调用同一个部署器启动 Docker、数据库、
三个核心应用服务和已经安装的可选模块。每次启动都会自动识别当前局域网
IPv4；仅当 `server.ota` 或 `server.websocket` 中的 IP 已过期时才更新
数据库并清除对应 Redis 缓存，最后执行状态检查。该启动器不会安装缺失
工具；首次部署或环境不完整时请使用 `deploy.bat`。

可选的声纹和 MCP 接入点属于独立仓库，不会默认安装。已有对应目录时可执行：

```powershell
python auto_deploy.py --only voiceprint,mcp
```

## 隐私与 Git

自动识别的局域网 IP、manager-api secret、模型 API 密钥和访问令牌只应写入
运行时配置或数据库，不应提交到 Git。仓库根目录 `.gitignore` 已忽略
`main/xiaozhi-server/data/`、部署状态、日志、`*.local.*` 本地覆盖文件、
私钥和证书。需要提供配置范例时，请只提交不含真实值的 `*.example` 文件。

`.gitignore` 不能清除已经提交过的秘密。如果真实密钥曾经进入 Git 历史，
应立即在服务商处吊销并重新生成，再单独清理 Git 历史。

## 失败定位

部署器在任一阶段失败时会停止后续阶段并返回非零退出码，不再把端口超时显示为成功。常用日志：

- `main/manager-api/logs/auto_deploy_manager_api.log`
- `main/manager-web/logs/auto_deploy_manager_web.log`
- `main/xiaozhi-server/tmp/auto_deploy_server.log`

如果 Docker 显示 `daemon unavailable`，先启动 Docker Desktop，再重新运行；重复运行是安全的，已经就绪的服务会被跳过。
