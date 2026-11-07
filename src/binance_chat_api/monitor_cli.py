from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.config
from contextlib import asynccontextmanager
from pathlib import Path

from .browser_bootstrap import cookie_client
from .monitor import ChatMonitor
from .monitor_config import MonitorSettings, autofill_target_session
from .monitor_store import MonitorStore
from .wecom import WeComNotifier


def monitor_log_path(database: Path) -> Path:
    return database.with_suffix(".log")


class RedactWebhookFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        prefix = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
        replacement = prefix + "<redacted>"
        record.msg = str(record.msg).replace(prefix, replacement)
        if record.args:
            record.args = tuple(
                str(arg).replace(prefix, replacement)
                for arg in record.args
            )
        return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bnchat-monitor",
        description="只读监控一个 Binance Square 群聊并推送企业微信",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("list-groups", "列出当前账号可见的群聊（不发送消息、不标记已读）"),
        ("run", "启动指定群聊监控（不监听 HTTP 端口）"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--env-file", type=Path, default=Path(".env.monitor"))
    return parser


def _settings(path: Path) -> MonitorSettings:
    return MonitorSettings.load(path)


@asynccontextmanager
async def open_client(settings: MonitorSettings):
    async with cookie_client(settings.cookie_file) as client:
        yield client


async def _list_groups(settings: MonitorSettings) -> None:
    async with open_client(settings) as client:
        response = await client.list_chats(rows=100)
        groups = []
        for chat in response.chats:
            if str(chat.get("sessionType", "")).upper() != "GROUP":
                continue
            groups.append(
                {
                    "session_id": chat.get("sessionId"),
                    "session_name": chat.get("sessionName") or chat.get("sessionId"),
                    "session_type": "GROUP",
                }
            )
        print(json.dumps(groups, ensure_ascii=False, indent=2))


async def _run(settings: MonitorSettings, env_file: Path = Path(".env.monitor")) -> None:
    settings.monitor_db.parent.mkdir(parents=True, exist_ok=True)
    store = MonitorStore.open(settings.monitor_db)
    notifier = WeComNotifier(settings.wecom_webhook_value())
    try:
        async with open_client(settings) as client:
            chats = await client.list_chats(rows=100)
            settings = autofill_target_session(
                env_file,
                chats=chats.chats,
            )
            monitor = ChatMonitor(
                client,
                store,
                notifier,
                settings.target_session_id,
                session_name=settings.target_session_name,
            )
            await monitor.run()
    finally:
        store.close()
        await notifier.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "--help":
        _parser().print_help()
        return 0
    try:
        settings = _settings(args.env_file)
        log_path = monitor_log_path(getattr(settings, "monitor_db", Path("monitor.sqlite3")))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.addFilter(RedactWebhookFilter())
        logging.basicConfig(
            level=getattr(logging, settings.monitor_log_level),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            handlers=[handler],
        )
        if args.command == "list-groups":
            asyncio.run(_list_groups(settings))
        else:
            asyncio.run(_run(settings, args.env_file))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.getLogger(__name__).error("monitor stopped: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

