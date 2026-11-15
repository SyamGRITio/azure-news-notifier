import pytest

from local_check import ConfigurationError, load_local_environment


def test_env_file_is_read_without_overwriting_shell_environment(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "FOUNDRY_ENDPOINT=https://example.openai.azure.com\n"
        "FOUNDRY_DEPLOYMENT=from-file\n"
        "SLACK_WEBHOOK_URL=https://hooks.slack.com/services/TEST\n",
        encoding="utf-8",
    )
    for name in ("FOUNDRY_ENDPOINT", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FOUNDRY_DEPLOYMENT", "from-shell")
    load_local_environment(env)
    import os

    assert os.environ["FOUNDRY_DEPLOYMENT"] == "from-shell"
    assert os.environ["FOUNDRY_ENDPOINT"] == "https://example.openai.azure.com"
    # dotenvが設定した値も、テスト終了時に残らないよう片付けます。
    monkeypatch.delenv("FOUNDRY_ENDPOINT")
    monkeypatch.delenv("SLACK_WEBHOOK_URL")


def test_missing_settings_only_print_names_not_values(monkeypatch, tmp_path):
    for name in ("FOUNDRY_ENDPOINT", "FOUNDRY_DEPLOYMENT", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigurationError, match="FOUNDRY_ENDPOINT"):
        load_local_environment(tmp_path / "missing.env")
