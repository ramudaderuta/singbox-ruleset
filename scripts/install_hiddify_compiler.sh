#!/usr/bin/env bash
set -euo pipefail

revision=3eb646a1134e144cbe0bcf466fa62af21a0d587d
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
dest=${1:?Pass a destination directory for the rules compiler}
mkdir -p -- "$dest"
dest=$(cd -- "$dest" && pwd -P)
tmp=$(mktemp -d)
trap 'find "$tmp" -depth -delete' EXIT

git -C "$tmp" init --quiet
git -C "$tmp" fetch --quiet --depth=1 https://github.com/hiddify/hiddify-sing-box.git "$revision"
git -C "$tmp" checkout --quiet --detach "$revision"
git -C "$tmp" submodule update --init --depth=1 replace/sing replace/wireguard-go

cd -- "$tmp"
CGO_ENABLED=0 GOTOOLCHAIN=local GOWORK=off go build -mod=readonly -trimpath \
  -o "$dest/hiddify-rules-compile" "$script_dir/hiddify_rules_compile.go"
