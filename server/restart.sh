#!/bin/bash
# Restart SuperSandbox server
pkill -f opensandbox-server 2>/dev/null
sleep 1
cd "$(dirname "$0")"
source .venv/bin/activate
opensandbox-server --config ~/.sandbox.toml
