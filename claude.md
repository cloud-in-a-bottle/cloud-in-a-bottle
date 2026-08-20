- read the style guide in style_guide.md
- on first init, ensure pre-commit hooks are installed (`pre-commit install`). this runs ruff and mypy on commit.
- please ask before doing anything that affects low level system stuff on this machine, or anything using sudo.
- readmes are all human written. any ai-generated docs will be in files like readme_ai_generated.md. the ai-generated docs can be used for context but should *not* be considered necessarily up to date or as hard constraints on how the system should/must be built.

## project structure

```
openhost/
├── compute_space/
│   └── compute_space/    # litestar/hypercorn app — routes requests to apps, manages containers
├── routerd_cli/          # `openhost` CLI: up, down, doctor, update
├── compute_space_cli/    # compute space management CLI
├── ansible/              # server deployment (any VPS or bare metal)
├── apps/
├── tests/                # integration and e2e tests
└── docs/                 # design docs and specs
└── services/             # specs for certain bundled services
```

## how components connect

1. **compute_space** is a litestar app (port 8080). it reads `ciab.toml` manifests from app repos, builds images from each app's `Dockerfile` using rootless podman, and runs each app in its own user namespace.
2. it proxies incoming HTTP requests to the correct app by matching subdomain.
3. **auth** uses JWT with RS256. apps verify with the public key passed as env var.

## running and testing

always run tests with -x to fail quickly.

- **all lightweight tests**: `pixi run -e dev pytest -x` (from project root)
- **everything**: `pixi run -e dev pytest -x --run-containers`
- **compute_space tests**: `pixi run -e dev pytest -x compute_space/tests/`

## warnings

address deprecation warnings that surface in tests — fix the call site, don't let them accumulate. common ones and their fixes:

- `litestar.contrib.jinja` import → import from `litestar.plugins.jinja`.
- path/query params via `Parameter(...)` → `FromPath[...]` / `FromQuery[...]` (from `litestar.params`).
- name-inferred DI (`Inferred dependency field`) → annotate the param with `NamedDependency[...]`.
- httpx per-request `cookies=` → set once on the client: `client.cookies.update(...)`, then call without `cookies=`.

when a warning genuinely can't be fixed at the source, suppress it as narrowly as possible in `pyproject.toml` `filterwarnings`: pin both the specific message and the exact warning subclass (e.g. `litestar.exceptions.LitestarDeprecationWarning`, not the broad `DeprecationWarning`), and prefer `once:` over `ignore:`.

## package manager

use `pixi` for all python work in this repo.  the `dev` environment
(`pixi install -e dev`) gives you the full test/lint stack.

on mac, the `coredns` and `podman` conda packages are linux-only — they
won't install via pixi.  the default test suite skips both via pytest
markers, so this is only relevant if you want to run `--run-tls` or
`--run-containers` locally on mac (install them by hand if so).

