"""Generate a synthetic consumer app directory for service-interface tests.

The generated app is a stdlib-only HTTP server (no pip installs at image build time) that
exposes ``POST /call-service`` — it forwards the request through the router's v2 service
proxy using its own app identity, so provider apps can be tested against a real consumer.
"""

import shutil
from pathlib import Path

import tomli_w

from compute_space.core.auth.permissions_v2 import Grant
from compute_space.core.manifest import MANIFEST_FILENAMES

_SERVER_TEMPLATE = Path(__file__).parent / "consumer_server.py"

_DOCKERFILE = """\
FROM python:3.12-alpine
COPY server.py /server.py
CMD ["python", "/server.py"]
"""


def write_consumer_app_dir(
    target_dir: Path,
    name: str,
    service: str,
    shortname: str,
    version: str,
    grants: list[Grant],
) -> None:
    """Write a deployable consumer app (ciab.toml + Dockerfile + server.py) to target_dir."""
    manifest = {
        "app": {
            "name": name,
            "version": "0.1.0",
            "description": "Synthetic service consumer (openhost test harness)",
            "hidden": True,
        },
        "runtime": {"container": {"image": "Dockerfile", "port": 5000}},
        "routing": {"health_check": "/health"},
        "resources": {"memory_mb": 64, "cpu_cores": 0.1},
        "services": {
            "v2": {
                "consumes": [
                    {
                        "service": service,
                        "shortname": shortname,
                        "version": version,
                        "grants": grants,
                    }
                ]
            }
        },
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / MANIFEST_FILENAMES[0]).write_text(tomli_w.dumps(manifest))
    (target_dir / "Dockerfile").write_text(_DOCKERFILE)
    shutil.copy(_SERVER_TEMPLATE, target_dir / "server.py")
