"""Alerting for unattended jobs.

The property that matters is not "an alert was sent". It is that a *failed*
backup does not send a heartbeat — because the heartbeat is what an external
monitor uses to decide everything is fine. A job that pings on the way out
regardless of outcome is a dead-man's switch wired to always say "alive", which
is worse than none: it actively suppresses the alarm.
"""

from __future__ import annotations

import pytest

from app.services import alerting


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(alerting.settings, "backup_heartbeat_url", "")
    monkeypatch.setattr(alerting.settings, "alert_webhook_url", "")
    monkeypatch.setattr(alerting.settings, "sentry_dsn", "")
    yield


def test_nothing_configured_is_not_an_error(monkeypatch):
    """Alerting is optional; a missing URL must not break the job."""
    assert alerting.heartbeat("backup") is False
    # Still returns cleanly, and the message is logged either way.
    assert alerting.alert("backup", "failed") is False


def test_a_dead_endpoint_never_raises(monkeypatch):
    """An alerting call that throws turns a failed backup into a crashed one,
    and loses the diagnostic that was being reported."""
    monkeypatch.setattr(
        alerting.settings, "backup_heartbeat_url", "http://127.0.0.1:1/nope"
    )
    monkeypatch.setattr(
        alerting.settings, "alert_webhook_url", "http://127.0.0.1:1/nope"
    )
    assert alerting.heartbeat("backup") is False
    assert alerting.alert("backup", "failed") is False


def test_the_webhook_body_suits_slack_and_discord_unchanged(monkeypatch):
    """Slack renders `text`, Discord renders `content`. Sending both means one
    payload works with either without a per-provider setting."""
    sent = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            sent.update(json)
            return type("R", (), {"raise_for_status": lambda self: None})()

    monkeypatch.setattr(alerting.settings, "alert_webhook_url", "http://example/hook")
    monkeypatch.setattr(alerting.httpx, "Client", lambda **kw: FakeClient())

    alerting.alert("backup", "verification failed", detail="truncated")

    assert sent["text"] == sent["content"] == "[RentSafe] backup: verification failed"
    assert sent["status"] == "failed"
    assert sent["detail"] == "truncated"


def test_the_heartbeat_is_only_sent_on_success():
    """A backup that fails must leave the monitor waiting.

    This is a static check on purpose: the ordering in scripts/backup.py is the
    thing being asserted, and it is the kind of line somebody later moves for
    tidiness without realising it disarms the alarm.
    """
    import inspect

    from scripts import backup

    source = inspect.getsource(backup.main)
    fail_at = source.index("raise SystemExit(1)")
    beat_at = source.index("alerting.heartbeat")
    assert fail_at < beat_at, (
        "the heartbeat must come after the failure exit, or a broken backup "
        "still tells the monitor everything is fine"
    )


def test_a_verification_failure_raises_an_alert():
    """The reason has to reach a human, not just the exit code."""
    import inspect

    from scripts import backup

    source = inspect.getsource(backup.main)
    assert 'alerting.alert("backup", "verification failed"' in source


def test_an_unhandled_crash_still_alerts():
    """The loudest failures are otherwise the quietest: no non-zero exit anyone
    reads, and no heartbeat missing yet."""
    import inspect

    from scripts import backup

    source = inspect.getsource(backup)
    assert 'alerting.alert("backup", "crashed"' in source
