#!/usr/bin/env bash
# Regenerate pixi.lock with the pinned pixi version so `pixi install/run --locked` (CI + deploy)
# stays reproducible; a different local pixi resolves a lock CI's pinned pixi then rejects.
set -euo pipefail

PINNED=0.70.2
cd "$(dirname "$0")/.."

if [ "$(pixi --version 2>/dev/null)" = "pixi $PINNED" ]; then
    exec pixi lock
fi
exec pixi exec --spec "pixi=$PINNED" -- pixi lock
