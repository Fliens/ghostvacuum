#!/bin/zsh
set -eu
if ! curl -fsS --max-time 2 http://127.0.0.1:8099/api/summary >/dev/null 2>&1; then
  ssh -f -N -L 127.0.0.1:8099:172.27.30.30:8099 \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -i /Users/simon/Documents/Codex/2026-10-08/so/outputs/kontron-ssh-key \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/Users/simon/Documents/Codex/2026-10-08/so/outputs/kontron-known-hosts \
    simon@172.27.30.30
fi
open http://127.0.0.1:8099
