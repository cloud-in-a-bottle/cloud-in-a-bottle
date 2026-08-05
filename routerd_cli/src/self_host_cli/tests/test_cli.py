"""Tests for the openhost CLI.

These tests exercise argument parsing, doctor checks, config generation,
and the down command -- all without actually starting services.
"""

import os
from argparse import Namespace

import pytest

from self_host_cli import config_gen
from self_host_cli.doctor import _check_container_runtime
from self_host_cli.doctor import _check_pixi
from self_host_cli.doctor import _check_port
from self_host_cli.doctor import _check_python
from self_host_cli.doctor import _check_router_code
from self_host_cli.doctor import run_doctor
from self_host_cli.down import _cleanup_pidfile
from self_host_cli.down import _is_alive
from self_host_cli.down import _read_pid
from self_host_cli.main import _build_parser
from self_host_cli.up import _claim_token_path
from self_host_cli.up import _detect_public_ip
from self_host_cli.up import _provision_claim_token
from self_host_cli.up import _resolve_zone_domain

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestGenerateConfig:
    def _run(self, tmp_path, monkeypatch, domain="host.example.com"):
        cfg = tmp_path / "config.toml"
        monkeypatch.setattr(config_gen, "_CONFIG_PATH", str(cfg))
        config_gen.generate_config(domain=domain, data_dir=str(tmp_path))
        return cfg, tmp_path / "first_boot.toml"

    def test_domain_seeds_via_first_boot_not_config(self, tmp_path, monkeypatch):
        cfg, first_boot = self._run(tmp_path, monkeypatch)
        # The domain lives in first_boot.toml, not config.toml.
        assert "zone_domain" not in cfg.read_text()
        fb = first_boot.read_text()
        assert 'domain = "host.example.com"' in fb
        assert "tls = false" in fb  # local mode is HTTP-only

    def test_first_boot_is_write_once(self, tmp_path, monkeypatch):
        _, first_boot = self._run(tmp_path, monkeypatch, domain="first.example.com")
        # A second `up` with a different domain must not clobber the consumed seed.
        self._run(tmp_path, monkeypatch, domain="second.example.com")
        assert 'domain = "first.example.com"' in first_boot.read_text()

    def test_bare_up_seeds_localhost(self, tmp_path, monkeypatch):
        # Bare `openhost up` (no domain) must still seed a domain so the router boots on localhost.
        _, first_boot = self._run(tmp_path, monkeypatch, domain="")
        assert 'domain = "localhost"' in first_boot.read_text()

    def test_blank_seed_is_reseeded(self, tmp_path, monkeypatch):
        # A first_boot.toml left blank by an aborted run must be re-seeded, not preserved (else boot
        # stays wedged: read_first_boot ignores the blank domain and nothing seeds the DB).
        first_boot = tmp_path / "first_boot.toml"
        first_boot.write_text('domain = ""\ntls = false\n')
        self._run(tmp_path, monkeypatch, domain="host.example.com")
        assert 'domain = "host.example.com"' in first_boot.read_text()


class TestArgParsing:
    def test_no_args_is_none_command(self):
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_up_defaults(self):
        parser = _build_parser()
        args = parser.parse_args(["up"])
        assert args.command == "up"
        assert args.domain == ""
        assert args.zone_domain == ""
        assert args.email == ""
        assert args.port == 8080
        assert args.foreground is False

    def test_up_domain(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--domain", "example.com"])
        assert args.domain == "example.com"

    def test_up_zone_domain(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--zone-domain", "example.com"])
        assert args.zone_domain == "example.com"

    def test_up_domain_and_zone_domain(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--domain", "example.com", "--zone-domain", "example.com"])
        assert args.domain == "example.com"
        assert args.zone_domain == "example.com"

    def test_up_email(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--domain", "example.com", "--email", "admin@example.com"])
        assert args.domain == "example.com"
        assert args.email == "admin@example.com"

    def test_up_custom_port(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--port", "9090"])
        assert args.port == 9090

    def test_up_foreground(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--foreground"])
        assert args.foreground is True

    def test_up_claim_token(self):
        parser = _build_parser()
        args = parser.parse_args(["up", "--claim-token", "secret123"])
        assert args.claim_token == "secret123"

    def test_up_claim_token_default(self):
        parser = _build_parser()
        args = parser.parse_args(["up"])
        assert args.claim_token is None

    def test_down(self):
        parser = _build_parser()
        args = parser.parse_args(["down"])
        assert args.command == "down"

    def test_doctor(self):
        parser = _build_parser()
        args = parser.parse_args(["doctor"])
        assert args.command == "doctor"

    def test_update(self):
        parser = _build_parser()
        args = parser.parse_args(["update"])
        assert args.command == "update"


# ---------------------------------------------------------------------------
# Doctor checks
# ---------------------------------------------------------------------------


class TestDoctorChecks:
    def test_python_version(self):
        c = _check_python()
        assert c.ok is True

    def test_pixi_check(self):
        c = _check_pixi()
        assert c.ok is True

    def test_router_code_present(self):
        c = _check_router_code()
        assert c.ok is True

    def test_port_check_high_port(self):
        """A random high port should typically be available."""
        c = _check_port(59123)
        assert c.ok is True

    def test_container_runtime_check_returns_check(self):
        c = _check_container_runtime()
        assert hasattr(c, "ok")
        assert hasattr(c, "name")

    def test_run_doctor_returns_bool(self, capsys):
        result = run_doctor()
        assert isinstance(result, bool)
        out = capsys.readouterr().out
        assert "Checks" in out


# ---------------------------------------------------------------------------
# Down (pidfile helpers)
# ---------------------------------------------------------------------------


class TestDownHelpers:
    def test_read_pid_missing_file(self, tmp_path):
        assert _read_pid(str(tmp_path / "nope.pid")) is None

    def test_read_pid_valid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        assert _read_pid(str(pid_file)) == 12345

    def test_read_pid_invalid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not-a-number")
        assert _read_pid(str(pid_file)) is None

    def test_is_alive_current_process(self):
        assert _is_alive(os.getpid()) is True

    def test_is_alive_nonexistent(self):
        assert _is_alive(1 << 30) is False

    def test_cleanup_pidfile(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("123")
        _cleanup_pidfile(str(pid_file))
        assert not pid_file.exists()

    def test_cleanup_pidfile_missing(self, tmp_path):
        _cleanup_pidfile(str(tmp_path / "nope.pid"))


# ---------------------------------------------------------------------------
# Up (unit-testable helpers)
# ---------------------------------------------------------------------------


class TestUpHelpers:
    def test_detect_public_ip_returns_string(self):
        result = _detect_public_ip()
        assert isinstance(result, str)

    def test_detect_public_ip_respects_env(self, monkeypatch):
        monkeypatch.setenv("PUBLIC_IP", "1.2.3.4")
        assert _detect_public_ip() == "1.2.3.4"

    def test_detect_public_ip_returns_empty_on_error(self, monkeypatch):
        def fake_run(*args, **kwargs):
            raise OSError("hostname not found")

        def fake_socket(*args, **kwargs):
            raise OSError("no route")

        monkeypatch.setattr("self_host_cli.up.subprocess.run", fake_run)
        monkeypatch.setattr("self_host_cli.up.socket.socket", fake_socket)
        monkeypatch.delenv("PUBLIC_IP", raising=False)
        assert _detect_public_ip() == ""

    def test_resolve_zone_domain_prefers_domain(self):
        args = Namespace(domain="example.com", zone_domain="")
        assert _resolve_zone_domain(args) == "example.com"

    def test_resolve_zone_domain_uses_zone_domain(self):
        args = Namespace(domain="", zone_domain="example.com")
        assert _resolve_zone_domain(args) == "example.com"

    def test_resolve_zone_domain_rejects_mismatch(self):
        args = Namespace(domain="example.com", zone_domain="other.com")
        with pytest.raises(SystemExit):
            _resolve_zone_domain(args)


class TestClaimToken:
    def test_supplied_token_is_written(self, tmp_path, capsys):
        _provision_claim_token(str(tmp_path), supplied_token="my-secret", port=8080)
        assert _claim_token_path(str(tmp_path)).read_text() == "my-secret"
        assert "claim=my-secret" in capsys.readouterr().out

    def test_supplied_token_overwrites_existing(self, tmp_path):
        path = _claim_token_path(str(tmp_path))
        path.parent.mkdir(parents=True)
        path.write_text("stale")
        _provision_claim_token(str(tmp_path), supplied_token="fresh", port=8080)
        assert path.read_text() == "fresh"

    def test_existing_token_preserved_when_none_supplied(self, tmp_path, capsys):
        path = _claim_token_path(str(tmp_path))
        path.parent.mkdir(parents=True)
        path.write_text("keep-me")
        _provision_claim_token(str(tmp_path), supplied_token=None, port=8080)
        assert path.read_text() == "keep-me"
        assert "claim=keep-me" in capsys.readouterr().out

    def test_random_token_generated_when_absent(self, tmp_path, capsys):
        # No --claim-token and no file on disk: generate a URL-safe random token.
        _provision_claim_token(str(tmp_path), supplied_token=None, port=8080)
        path = _claim_token_path(str(tmp_path))
        token = path.read_text()
        # token_urlsafe(32) yields ~43 chars of urlsafe-base64.
        assert len(token) >= 32
        assert f"claim={token}" in capsys.readouterr().out

    def test_supplied_token_rejects_non_url_safe(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            _provision_claim_token(str(tmp_path), supplied_token="has spaces", port=8080)
        assert "URL-safe" in capsys.readouterr().err

    def test_supplied_token_rejects_special_chars(self, tmp_path):
        for bad in ("a/b", "a?b", "a&b", "a%b", "a=b", "a#b", ""):
            with pytest.raises(SystemExit):
                _provision_claim_token(str(tmp_path), supplied_token=bad, port=8080)

    def test_existing_token_rejected_if_not_url_safe(self, tmp_path, capsys):
        path = _claim_token_path(str(tmp_path))
        path.parent.mkdir(parents=True)
        path.write_text("not safe!")
        with pytest.raises(SystemExit):
            _provision_claim_token(str(tmp_path), supplied_token=None, port=8080)
        assert "URL-safe" in capsys.readouterr().err

    def test_existing_token_with_metadata_suffix(self, tmp_path, capsys):
        # setup_app parses content.split(":", 1)[0] so trailing metadata is fine
        # as long as the token portion is URL-safe.
        path = _claim_token_path(str(tmp_path))
        path.parent.mkdir(parents=True)
        path.write_text("abc123:some-metadata-here")
        _provision_claim_token(str(tmp_path), supplied_token=None, port=8080)
        assert "claim=abc123" in capsys.readouterr().out
