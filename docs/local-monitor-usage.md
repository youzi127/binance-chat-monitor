# 指定 Binance 群聊本地监控

## 运行前准备

必须人工准备：

1. Python 3.11+ 或 Docker；
2. 已登录 Binance 浏览器导出的 Netscape Cookie 文件；
3. 企业微信群机器人 Webhook URL；
4. 目标群 ID（通过 `list-groups` 获取）。

程序自动完成：短期 WebSocket Token、动态浏览器请求头、WebSocket 连接、SQLite 去重和企业微信推送。不需要 Binance API Key/Secret。

Cookie 过期、被撤销、要求 CAPTCHA/MFA 或设备确认时，需要人工重新导出并替换 Cookie；程序不会绕过验证。

## 本地安装

```powershell
cd <项目目录>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[browser]"
Copy-Item .env.monitor.example .env.monitor
```

配置 `.env.monitor`：

```dotenv
BINANCE_CHAT_COOKIE_FILE=./data/www.binance.com_cookies.txt
BINANCE_CHAT_TARGET_SESSION_ID=12701
BINANCE_CHAT_TARGET_SESSION_NAME=丁家军军团
BINANCE_CHAT_TARGET_SESSION_TYPE=GROUP
WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=replace-me
BINANCE_CHAT_MONITOR_DB=./data/monitor.sqlite3
BINANCE_CHAT_MONITOR_LOG_LEVEL=INFO
```

```powershell
.\.venv\Scripts\bnchat-monitor.exe list-groups --env-file .env.monitor
.\.venv\Scripts\bnchat-monitor.exe run --env-file .env.monitor
```

通知格式：

```text
【聊天室-丁家军军团】1 条消息
- 发送者 · HH:MM:SS:
消息内容
```

## 服务器

将 Cookie 上传到服务器专用目录，例如 `/opt/bnchat-monitor/data/www.binance.com_cookies.txt`，权限设为仅运行用户可读；`.env.monitor` 放在 Git 工作区之外。只运行一个实例，不开放 HTTP 入站端口。
