from __future__ import annotations

import json
from contextlib import asynccontextmanager

from binance_chat_api import monitor_cli


class FakeClient:
    async def list_chats(self, **_kwargs):
        return type(
            "Chats",
            (),
            {
                "chats": [
                    {"sessionId": "g-1", "sessionName": "目标群", "sessionType": "GROUP"},
                    {"sessionId": "p-1", "sessionName": "私聊", "sessionType": "PRIVATE"},
                ]
            },
        )()


def fake_client_context(_settings):
    @asynccontextmanager
    async def context():
        yield FakeClient()

    return context()


class FakeSettings:
    cookie_file = "cookies.txt"
    monitor_log_level = "INFO"


def fake_settings():
    return FakeSettings()


def test_list_groups_outputs_only_group_sessions(monkeypatch, capsys):
    monkeypatch.setattr(monitor_cli, "open_client", fake_client_context)
    monkeypatch.setattr(monitor_cli, "_settings", lambda _: fake_settings())

    assert monitor_cli.main(["list-groups", "--env-file", "test.env"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == [
        {"session_id": "g-1", "session_name": "目标群", "session_type": "GROUP"}
    ]


def test_help_has_only_read_only_commands(capsys):
    try:
        monitor_cli.main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out
    assert "list-groups" in output
    assert "run" in output
    assert "send" not in output

