- read the style guide in style_guide.md
- on first init, ensure pre-commit hooks are installed (`pre-commit install`). this runs ruff and mypy on commit.
- please ask before doing anything that affects low level system stuff on this machine, or anything using sudo.
- never use em dashes, in docs, code comments, commit messages, or anything else you write. rewrite the sentence with a colon, comma, semicolon, parentheses, or two sentences instead.

## project structure

```
openhost/
├── compute_space/
│   └── compute_space/    # litestar/hypercorn app: routes requests to apps, manages containers
├── compute_space_cli/    # compute space management CLI
├── ansible/              # server deployment (any VPS or bare metal)
├── apps/
├── tests/                # integration and e2e tests
└── docs/                 # design docs and specs
└── services/             # specs for certain bundled services
```

## how components connect

1. **compute_space** is a litestar app (port 8080). it reads `cloudinabottle.toml` manifests from app repos, builds images from each app's `Dockerfile` using rootless podman, and runs each app in its own user namespace.
2. it proxies incoming HTTP requests to the correct app by matching subdomain.
3. **auth** uses JWT with RS256. apps verify with the public key passed as env var.

## running and testing

**in general don't run the full test suite (or full sub-suites) locally; that's what CI is for.** it is fine to run individual tests or a few test files, but the strong default is to let CI handle the rest. this applies to the unit test suite also.

run a single test file with `-x` to fail fast, e.g.:

```
pixi run -e dev pytest -x compute_space/src/compute_space/tests/test_token_hashing.py
```

### keep test concerns out of production code

don't add test-only hooks to non-test code: no env vars, flags or branches that exist only so tests can change behaviour. patch from the test side instead: `monkeypatch` in-process, and for routers launched as subprocesses, apply the patch in `compute_space/tests/router_entrypoint.py`, which every test router runs through (via `utils.router_cmd`) before calling the real `main()`. production code may expose a plain module-level value or function as the patch point (e.g. `containers.APP_NAME_PREFIX`), as long as it doesn't mention tests.

### tests run in parallel

CI runs both suites under pytest-xdist with `--dist loadfile`: each file stays on one worker in order, but different files run at the same time on the same host. so a test must not depend on host-global state another file could touch at the same time: fixed ports, fixed paths outside `tmp_path`, or podman container/image/volume names. derive names from a uuid or `tmp_path`. app containers and images are already namespaced per worker (the root `conftest.py` sets a per-worker `APP_NAME_PREFIX`), so build them with `app_container_name()` / `app_image_tag()`, never a literal `openhost-<app>`. a podman `rm -f` / `rmi -f` by a name another file might also use can kill that file's containers mid-test.

## warnings

address deprecation warnings that surface in tests: fix the call site, don't let them accumulate. common ones and their fixes:

- `litestar.contrib.jinja` import → import from `litestar.plugins.jinja`.
- path/query params via `Parameter(...)` → `FromPath[...]` / `FromQuery[...]` (from `litestar.params`).
- name-inferred DI (`Inferred dependency field`) → annotate the param with `NamedDependency[...]`.
- httpx per-request `cookies=` → set once on the client: `client.cookies.update(...)`, then call without `cookies=`.

when a warning genuinely can't be fixed at the source, suppress it as narrowly as possible in `pyproject.toml` `filterwarnings`: pin both the specific message and the exact warning subclass (e.g. `litestar.exceptions.LitestarDeprecationWarning`, not the broad `DeprecationWarning`), and prefer `once:` over `ignore:`.

## package manager

use `pixi` for all python work in this repo.  the `dev` environment
(`pixi install -e dev`) gives you the full test/lint stack.

on mac, the `coredns` and `podman` conda packages are linux-only and
won't install via pixi.  the default test suite skips both via pytest
markers, so this is only relevant if you want to run `--run-tls` or
`--run-containers` locally on mac (install them by hand if so).
