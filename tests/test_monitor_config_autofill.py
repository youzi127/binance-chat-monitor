from pathlib import Path

from binance_chat_api.monitor_config import autofill_target_session


def test_autofill_name_from_id(tmp_path: Path):
    env = tmp_path / ".env.monitor"
    (tmp_path / "cookies.txt").write_text("x", encoding="utf-8")
    env.write_text(
        "BINANCE_CHAT_COOKIE_FILE=./cookies.txt\n"
        "BINANCE_CHAT_TARGET_SESSION_ID=12701\n"
        "WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x\n",
        encoding="utf-8",
    )
    result = autofill_target_session(
        env,
        chats=[{"sessionId": "12701", "sessionName": "丁家军军团", "sessionType": "GROUP"}],
    )
    assert result.target_session_name == "丁家军军团"
    assert "BINANCE_CHAT_TARGET_SESSION_NAME=丁家军军团" in env.read_text(encoding="utf-8")
    assert str(tmp_path / "cookies.txt") in env.read_text(encoding="utf-8")


def test_autofill_id_from_unique_name(tmp_path: Path):
    env = tmp_path / ".env.monitor"
    (tmp_path / "cookies.txt").write_text("x", encoding="utf-8")
    env.write_text(
        "BINANCE_CHAT_COOKIE_FILE=./cookies.txt\n"
        "BINANCE_CHAT_TARGET_SESSION_NAME=丁家军军团\n"
        "WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x\n",
        encoding="utf-8",
    )
    result = autofill_target_session(
        env,
        chats=[{"sessionId": "12701", "sessionName": "丁家军军团", "sessionType": "GROUP"}],
    )
    assert result.target_session_id == "12701"
    assert "BINANCE_CHAT_TARGET_SESSION_ID=12701" in env.read_text(encoding="utf-8")
