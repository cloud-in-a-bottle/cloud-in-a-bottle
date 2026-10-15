from __future__ import annotations

import pytest

from compute_space_cli.helpers import exit_if_reload_refused


def test_accepted_reload_passes() -> None:
    exit_if_reload_refused({"ok": True}, "myapp")


def test_refused_update_exits_with_the_diff(capsys: pytest.CaptureFixture[str]) -> None:
    body = {
        "ok": False,
        "review_required": True,
        "settings_changed": [{"group": "Services", "label": "Services provided", "old": "(none)", "new": "x"}],
        "permissions_required": [{"service_url": "github.com/x/secrets", "grant": {"key": "API_KEY"}}],
        "error": "This update changes the app's settings; review and approve them before it can be applied.",
    }
    with pytest.raises(SystemExit) as exc:
        exit_if_reload_refused(body, "myapp")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "myapp was not reloaded: This update changes" in err
    assert "Services provided: (none) -> x" in err
    assert "github.com/x/secrets" in err
