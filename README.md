# Binance Chat Monitor

只读监控一个 Binance Square 群聊，并用企业微信机器人推送新入站文本消息。

## 能力

- Binance Chat WebSocket 实时事件；
- 指定 `GROUP` 会话过滤；
- `self` 缺失时按发送者 UID 判断自己/他人消息；
- SQLite 去重和首次启动基线；
- 企业微信 Markdown 推送：`【聊天室-群名】1 条消息`；
- 不启动 HTTP 服务、不开放端口、不使用 Binance 交易 API Key；
- 不发送 Binance 消息、不标记已读、不读取账户余额或订单。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[browser]"
Copy-Item .env.monitor.example .env.monitor
```

将已登录 Binance 浏览器导出的 Cookie 文件放到 `data/www.binance.com_cookies.txt`，编辑 `.env.monitor` 设置目标群和企业微信 Webhook，然后：

```powershell
.\.venv\Scripts\bnchat-monitor.exe list-groups --env-file .env.monitor
.\.venv\Scripts\bnchat-monitor.exe run --env-file .env.monitor
```

完整准备清单和服务器部署说明见 [SKILL.md](SKILL.md) 和 [docs/local-monitor-usage.md](docs/local-monitor-usage.md)。

## 安全

不要提交 `.env.monitor`、Cookie、Webhook URL、SQLite 数据库或日志。Cookie 代表 Binance 登录态，应通过 SSH/SCP 或云服务器 Secret 传输。

本项目是非官方客户端，依赖 Binance 私有网页协议；请只监控你本人有权访问的聊天室。

配置也支持 BINANCE_CHAT_TARGET_SESSION_NAME；缺少群 ID 或群名时，程序会通过 chat-list 自动补全并回写 .env.monitor。

