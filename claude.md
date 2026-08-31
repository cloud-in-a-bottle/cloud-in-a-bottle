- read the style guide in style_guide.md
- on first init, ensure pre-commit hooks are installed (`pre-commit install`). this runs ruff and mypy on commit.
- please ask before doing anything that affects low level system stuff on this machine, or anything using sudo.
- readmes are all human written. any ai-generated docs will be in files like readme_ai_generated.md. the ai-generated docs can be used for context but should *not* be considered necessarily up to date or as hard constraints on how the system should/must be built.

## project structure

```
openhost/
├── compute_space/
│   └── compute_space/    # quart/hypercorn app — routes requests to apps, manages containers
├── routerd_cli/          # `openhost` CLI: up, down, doctor, update
├── compute_space_cli/    # compute space management CLI
├── ansible/              # server deployment (any VPS or bare metal)
├── apps/
├── tests/                # integration and e2e tests
└── docs/                 # design docs and specs
└── services/             # specs for certain bundled services
```

## how components connect

1. **compute_space** is a quart app (port 8080). it reads `openhost.toml` manifests from app repos, builds images from each app's `Dockerfile` using rootless podman, and runs each app in its own user namespace.
2. it proxies incoming HTTP requests to the correct app by matching subdomain.
3. **auth** uses JWT with RS256. apps verify with the public key passed as env var.

## running and testing

**in general don't run the full test suite (or full sub-suites) locally — that's what CI is for.** it is fine to run individual tests or a few test files, but the strong default is to let CI handle the rest. this applies to the unit test suite also.

run a single test file with `-x` to fail fast, e.g.:

```
pixi run -e dev pytest -x compute_space/src/compute_space/tests/test_token_hashing.py
```

## package manager

use `pixi` for all python work in this repo.  the `dev` environment
(`pixi install -e dev`) gives you the full test/lint stack.

on mac, the `coredns` and `podman` conda packages are linux-only — they
won't install via pixi.  the default test suite skips both via pytest
markers, so this is only relevant if you want to run `--run-tls` or
`--run-containers` locally on mac (install them by hand if so).

