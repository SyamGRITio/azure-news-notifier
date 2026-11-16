import json
from contextlib import contextmanager

import pytest

import function_app
from notifier.models import DeliveryState, Digest
from notifier.state_store import AlreadyRunningError, LeaseLostError


def test_timer_is_daily_and_disabled_at_startup():
    functions = function_app.app.get_functions()
    assert len(functions) == 1
    binding = json.loads(functions[0].get_function_json())["bindings"][0]
    assert binding["type"] == "timerTrigger"
    assert binding["schedule"] == "0 0 23 * * *"
    assert binding["runOnStartup"] is False
    assert binding["useMonitor"] is True


def test_timer_does_not_expose_exception_secrets(monkeypatch, caplog):
    def fail():
        raise RuntimeError("secret webhook URL")

    monkeypatch.setattr(function_app, "run_azure", fail)
    with pytest.raises(RuntimeError) as error:
        function_app.daily_news(None)
    assert "secret webhook" not in str(error.value)
    assert "secret webhook" not in caplog.text


def test_timer_skips_concurrent_run(monkeypatch):
    def busy():
        raise AlreadyRunningError("busy")

    monkeypatch.setattr(function_app, "run_azure", busy)
    function_app.daily_news(None)


@pytest.mark.parametrize("lost", [False, True])
def test_azure_wiring_uses_identity_blob_and_checks_lock_before_send(monkeypatch, lost):
    events = []
    monkeypatch.setenv("DELIVERY_START_AT", "2026-11-16T00:00:00+09:00")
    monkeypatch.setenv("FOUNDRY_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("FOUNDRY_DEPLOYMENT", "gpt-5-nano")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/TEST")
    monkeypatch.setenv("STATE_BLOB_URL", "https://example.blob.core.windows.net/state/state.json")

    class Credential:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_token(self, scope):
            assert scope == "https://cognitiveservices.azure.com/.default"
            return type("Token", (), {"token": "fake-token"})()

    class Blob:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @classmethod
        def from_blob_url(cls, url, credential):
            assert url.endswith("/state/state.json")
            return cls()

    class Store:
        def __init__(self, blob):
            pass

        @contextmanager
        def locked(self):
            yield self

        def load(self):
            return DeliveryState()

        def save(self, state):
            events.append("save")

        def ensure_lock(self):
            events.append("lock-check")
            if lost:
                raise LeaseLostError("lost")

    class Translator:
        def __init__(self, endpoint, deployment, token, http):
            assert token() == "fake-token"

        def translate(self, article):
            return article

    class Slack:
        def __init__(self, webhook, http):
            pass

        def send(self, digest):
            events.append("send")

    def workflow(feeds, fetch, translate, send, state, month, save, *, start_at):
        assert start_at.isoformat() == "2026-11-15T15:00:00+00:00"
        assert len(feeds) == 9
        send(None)
        save(state)
        return Digest((), is_summary=True)

    monkeypatch.setattr(function_app, "ManagedIdentityCredential", Credential)
    monkeypatch.setattr(function_app, "BlobClient", Blob)
    monkeypatch.setattr(function_app, "BlobStateStore", Store)
    monkeypatch.setattr(function_app, "FoundryTranslator", Translator)
    monkeypatch.setattr(function_app, "SlackNotifier", Slack)
    monkeypatch.setattr(function_app, "run", workflow)
    if lost:
        with pytest.raises(LeaseLostError):
            function_app.run_azure()
        assert events == ["lock-check"]
    else:
        function_app.run_azure()
        assert events == ["lock-check", "send", "save"]


@pytest.mark.parametrize("value", ["", "not-a-date", "2026-11-16", "2026-11-16T00:00:00"])
def test_invalid_start_setting_stops_before_azure_access(monkeypatch, value):
    monkeypatch.setenv("DELIVERY_START_AT", value)
    monkeypatch.setattr(
        function_app, "ManagedIdentityCredential", lambda: pytest.fail("Azure access")
    )
    with pytest.raises(ValueError, match="DELIVERY_START_AT"):
        function_app.run_azure()
