import local_check as cli


def test_default_demo_is_offline_and_does_not_save_state(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["notifier"])
    monkeypatch.setattr(
        "notifier.http_client.HttpClient.get",
        lambda *_: (_ for _ in ()).throw(AssertionError()),
    )
    assert cli.main() == 0
    assert "外部通信を行わない動作確認" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_cli_does_not_print_secret_exceptions(monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("https://hooks.slack.com/services/secret")

    monkeypatch.setattr("sys.argv", ["notifier", "--live"])
    monkeypatch.setattr(cli, "_run_live", fail)
    assert cli.main() == 1
    assert "secret" not in capsys.readouterr().err


def test_smoke_command_selects_one_article_connection_check(monkeypatch):
    calls = []
    monkeypatch.setattr("sys.argv", ["notifier", "--smoke-test"])
    monkeypatch.setattr(cli, "_run_live", lambda *args, **kwargs: calls.append(kwargs))
    assert cli.main() == 0
    assert calls == [{"smoke_test": True}]


def test_missing_configuration_is_explained_without_secret_values(monkeypatch, capsys):
    from local_check import ConfigurationError

    def fail(*args, **kwargs):
        raise ConfigurationError("app/.envに設定してください: SLACK_WEBHOOK_URL")

    monkeypatch.setattr("sys.argv", ["notifier", "--smoke-test"])
    monkeypatch.setattr(cli, "_run_live", fail)
    assert cli.main() == 1
    assert "SLACK_WEBHOOK_URL" in capsys.readouterr().err
