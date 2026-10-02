#!/usr/bin/env bash
# Builds the pinned `mecmcp-redact` CLI binary (mechubsec/mecmcp, tag in
# mecmcp-redact.pin) that mechubbench.mecmcp_redact shells out to. Run this
# once before `bench run` / pytest; MECMCP_REDACT_BIN can point elsewhere
# instead (e.g. a binary already built by CI for a pinned release).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tag="$(tr -d '[:space:]' < "$repo_root/mecmcp-redact.pin")"
vendor_dir="$repo_root/.mecmcp-redact"
src_dir="$vendor_dir/src"
bin_dir="$vendor_dir/bin"

mkdir -p "$bin_dir"

if [ ! -d "$src_dir/.git" ]; then
  rm -rf "$src_dir"
  git clone --depth 1 --branch "$tag" https://github.com/mechubsec/mecmcp.git "$src_dir"
else
  git -C "$src_dir" fetch --depth 1 origin "refs/tags/$tag:refs/tags/$tag"
  git -C "$src_dir" checkout "$tag"
fi

cargo build --release --manifest-path "$src_dir/Cargo.toml" -p mecmcp-redact --features mecmcp-redact/cli
cp "$src_dir/target/release/mecmcp-redact" "$bin_dir/mecmcp-redact"

echo "Built mecmcp-redact ($tag) -> $bin_dir/mecmcp-redact"
