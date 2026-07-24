"""Agent-side config.toml scrub (delegated by the router as the single privileged config writer)."""

from __future__ import annotations

import os
from pathlib import Path

from openhost_system_agent.config_edit import scrub_zone_domain


def test_removes_only_the_zone_domain_line(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        "[openhost]\n"
        'zone_domain = "host.example.com"\n'
        'host = "127.0.0.1"\n'
        "tls_enabled = true\n"
        "[[openhost.domains]]\n"
        'name = "second.example.com"\n'
    )
    result = scrub_zone_domain(str(p))
    assert result.ok is True and result.scrubbed is True
    text = p.read_text()
    assert "zone_domain" not in text
    # everything else preserved
    assert 'host = "127.0.0.1"' in text
    assert "[[openhost.domains]]" in text and 'name = "second.example.com"' in text


def test_idempotent_when_line_absent(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    body = '[openhost]\nhost = "127.0.0.1"\n'
    p.write_text(body)
    result = scrub_zone_domain(str(p))
    assert result.scrubbed is False
    assert p.read_text() == body  # untouched


def test_missing_file_is_a_noop(tmp_path: Path) -> None:
    result = scrub_zone_domain(str(tmp_path / "does-not-exist.toml"))
    assert result.ok is True and result.scrubbed is False


def test_preserves_owner_and_mode(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[openhost]\nzone_domain = "host.example.com"\nhost = "127.0.0.1"\n')
    os.chmod(p, 0o640)
    before = p.stat()
    scrub_zone_domain(str(p))
    after = p.stat()
    assert after.st_mode & 0o777 == 0o640
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
