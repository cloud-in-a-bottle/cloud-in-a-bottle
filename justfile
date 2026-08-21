_default:
    @just --list

# run a local compute space on a random port (extra args pass through, eg --port 8080 --default-apps)
local-stack *args:
    pixi run -e dev python scripts/run_local_stack.py {{args}}

# same, but wipe the persisted data dir first so setup starts over
local-stack-fresh *args:
    pixi run -e dev python scripts/run_local_stack.py --fresh {{args}}
