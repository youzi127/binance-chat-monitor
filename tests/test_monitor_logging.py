def test_monitor_log_path_is_local_and_stable(tmp_path):
    from binance_chat_api.monitor_cli import monitor_log_path

    path = monitor_log_path(tmp_path / "monitor.sqlite3")
    assert path == tmp_path / "monitor.log"
