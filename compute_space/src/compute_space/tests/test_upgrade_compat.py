"""Boot-time domain guard: the router fails loud when nothing seeded the DB `domains` table, and
boots fine once a domain is present.  (Old-instance config.toml → DB capture is a system-agent
migration, tested in openhost_system_agent.)"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from compute_space.core.domains import Domain
from compute_space.core.domains import effective_domains
from compute_space.core.domains import seed_domains
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config
from compute_space.tests.conftest import open_db
from compute_space.web.start import _require_configured_domain


def test_boot_fails_loud_when_no_domain_configured(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path, seed_primary=False)  # empty domains table
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        domains = effective_domains(db)  # nothing seeded it — the misconfiguration the guard catches
    assert domains == ()
    with pytest.raises(RuntimeError, match="No domain configured"):
        _require_configured_domain(domains)


def test_boot_guard_passes_for_seeded_instance(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path, seed_primary=False)
    init_db(cfg.db_path)
    with closing(open_db(cfg)) as db:
        seed_domains(db, Domain("host.example.com", tls=True), [])
        domains = effective_domains(db)
    _require_configured_domain(domains)  # a seeded instance boots fine (no raise)
