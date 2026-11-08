#!/usr/bin/env bash
# Native Kubo node on a VPS, as a systemd service. Run there as the user that will own the repo:
#   bash kubo-vps.sh                       # downloads and verifies Kubo from dist.ipfs.tech
#   KUBO_BIN=~/ipfs bash kubo-vps.sh       # or install a binary you already have
# The RPC (5001) and gateway stay on 127.0.0.1; only the swarm port 4001 (tcp+udp) should be public.
set -euo pipefail

VERSION=${KUBO_VERSION:-v0.43.1}
GATEWAY_PORT=${GATEWAY_PORT:-8080}
export IPFS_PATH=${IPFS_PATH:-$HOME/.ipfs}

if [[ -z ${KUBO_BIN:-} ]]; then
  tmp=$(mktemp -d)
  tarball=kubo_${VERSION}_linux-amd64.tar.gz
  curl -fL --retry 3 -o "$tmp/$tarball" "https://dist.ipfs.tech/kubo/$VERSION/$tarball"
  curl -fL --retry 3 -o "$tmp/$tarball.sha512" "https://dist.ipfs.tech/kubo/$VERSION/$tarball.sha512"
  (cd "$tmp" && sha512sum -c "$tarball.sha512")
  tar -xzf "$tmp/$tarball" -C "$tmp"
  KUBO_BIN=$tmp/kubo/ipfs
fi
sudo install -m 755 "$KUBO_BIN" /usr/local/bin/ipfs

[[ -d $IPFS_PATH ]] || ipfs init --profile server >/dev/null
ipfs config Addresses.API /ip4/127.0.0.1/tcp/5001
ipfs config Addresses.Gateway "/ip4/127.0.0.1/tcp/$GATEWAY_PORT"

# QUIC wants larger UDP buffers than most distributions allow
printf 'net.core.rmem_max=7500000\nnet.core.wmem_max=7500000\n' | sudo tee /etc/sysctl.d/99-ipfs-quic.conf >/dev/null
sudo sysctl -q --system

sudo tee /etc/systemd/system/ipfs.service >/dev/null <<EOF
[Unit]
Description=IPFS Kubo node
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
Environment=IPFS_PATH=$IPFS_PATH
ExecStart=/usr/local/bin/ipfs daemon --migrate=true
Restart=on-failure
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ipfs

# wait for the daemon's API (not just the CLI) before using it
until [[ -f $IPFS_PATH/api ]] && ipfs id >/dev/null 2>&1; do sleep 1; done
ipfs id -f '<id>\n'
