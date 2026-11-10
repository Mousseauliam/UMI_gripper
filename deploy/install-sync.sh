#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
install -m 644 "$script_dir/git_sync.py" /usr/local/lib/umi-git-sync.py
install -m 644 "$script_dir/umi-git-sync.service" /etc/systemd/system/umi-git-sync.service
if [ ! -f /etc/umi-git-sync.json ]; then
    install -m 644 "$script_dir/git-sync.example.json" /etc/umi-git-sync.json
fi
install -m 755 "$script_dir/umi-release" "$script_dir/umi-reload" /usr/local/lib/
install -m 644 "$script_dir/umi-app.service" "$script_dir/umi-app.path" "$script_dir/umi-reload.service" "$script_dir/umi-reload.timer" /etc/systemd/system/
# Earlier installations supplied these settings as drop-ins.
rm -f /etc/systemd/system/umi-app.service.d/{release,stop}.conf
systemctl daemon-reload
systemctl enable --now umi-app.path umi-app.service umi-reload.timer umi-git-sync.service
systemctl restart umi-git-sync.service
