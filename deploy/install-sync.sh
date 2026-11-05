#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install -m 644 "$script_dir/git_sync.py" /usr/local/lib/umi-git-sync.py
install -m 644 "$script_dir/umi-git-sync.service" /etc/systemd/system/umi-git-sync.service
if [ ! -f /etc/umi-git-sync.json ]; then
    install -m 644 "$script_dir/git-sync.example.json" /etc/umi-git-sync.json
fi
systemctl daemon-reload
systemctl enable --now umi-git-sync.service
