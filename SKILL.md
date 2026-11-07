---
name: binance-chat-monitor-deploy
description: Use when installing or deploying the read-only Binance Square Chat monitor for one group chat with WeCom notifications; not for trading, Binance account APIs, or sending Binance messages.
---

# Binance Chat Monitor Deployment

This repository contains a read-only monitor. It does not expose an HTTP server, use Binance API keys, send Binance messages, or mark chats read.

## Prepare manually

1. Python 3.11+ or Docker.
2. A Netscape Cookie export from a browser already logged in to Binance.
3. A WeCom robot Webhook URL.
4. The target group ID, discovered with `list-groups`.

Do not prepare a Binance API Key, API Secret, WebSocket token, `device-info`, `fvideo-token`, or `fvideo-id`: the monitor obtains the short-lived WebSocket token at runtime and its browser bootstrap generates dynamic headers in the current environment.

## Automatic behavior

- Validates the Cookie file and Binance session;
- Starts a temporary headless Chromium when dynamic headers need refresh;
- Obtains the short-lived WebSocket token in memory;
- Monitors one `GROUP` session and sends WeCom Markdown notifications;
- Persists deduplication state in SQLite.

If Binance expires/revokes the Cookie or requires CAPTCHA/MFA/device confirmation, update the Cookie file manually. Do not bypass those checks.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[browser]'
Copy-Item .env.monitor.example .env.monitor
```

Set `BINANCE_CHAT_COOKIE_FILE`, `BINANCE_CHAT_TARGET_SESSION_ID`, `BINANCE_CHAT_TARGET_SESSION_NAME`, `WECOM_WEBHOOK_URL`, and `BINANCE_CHAT_MONITOR_DB` in `.env.monitor`. Run:

```powershell
.\.venv\Scripts\bnchat-monitor.exe list-groups --env-file .env.monitor
.\.venv\Scripts\bnchat-monitor.exe run --env-file .env.monitor
```

## Server setup

Store the Cookie at `/opt/bnchat-monitor/data/www.binance.com_cookies.txt` with permissions readable only by the monitor user. Keep `.env.monitor` outside Git. Run exactly one container or systemd instance for a Cookie file; do not expose an HTTP port.

