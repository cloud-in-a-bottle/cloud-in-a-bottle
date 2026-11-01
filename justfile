# `just` ships in the dev environment: `pixi run -e dev just <recipe>`.
# (Or install it yourself and run `just <recipe>` directly.)

_default:
    @just --list

# run a local compute space on a random port (extra args pass through, eg --port 3000 --default-apps)
local-stack *args:
    pixi run -e dev python scripts/run_local_stack.py {{args}}

# same, but wipe the persisted data dir first so setup starts over
local-stack-fresh *args:
    pixi run -e dev python scripts/run_local_stack.py --fresh {{args}}

# cut a release (eg `just release 0.3.1`): bump PR, tag, watch the build, publish. interactive.
release version:
    pixi run -e dev python scripts/release.py {{version}}

# build the component library page into scripts/component_library/ (extra args pass through, eg --repo ../other-checkout)
components *args:
    pixi run -e dev python scripts/component_library/build.py {{args}}
