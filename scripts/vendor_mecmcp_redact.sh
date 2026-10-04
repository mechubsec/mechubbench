#!/usr/bin/env bash
# Builds the pinned `mecmcp-redact` CLI binary (mechubsec/mecmcp, tag in
# mecmcp-redact.pin) that mechubbench.mecmcp_redact shells out to. Run this
# once before `bench run` / pytest; MECMCP_REDACT_BIN can point elsewhere
# instead (e.g. a binary already built by CI for a pinned release).
#
# A tag ref can be force-moved to point at different content after the fact,
# so the tag alone is not a reproducible pin: mecmcp-redact.commit records
# the commit SHA the tag resolved to when it was last pinned here, and the
# checkout is verified against it below. Bumping the pin means updating both
# files together (and re-reviewing what changed).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tag="$(tr -d '[:space:]' < "$repo_root/mecmcp-redact.pin")"
pinned_commit="$(tr -d '[:space:]' < "$repo_root/mecmcp-redact.commit")"
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

resolved_commit="$(git -C "$src_dir" rev-parse HEAD)"
if [ "$resolved_commit" != "$pinned_commit" ]; then
  echo "mecmcp-redact tag $tag resolved to $resolved_commit, expected" \
    "$pinned_commit (mecmcp-redact.commit) - the tag was moved since it was" \
    "pinned; refusing to build an unverified checkout" >&2
  exit 1
fi

cargo build --release --locked --manifest-path "$src_dir/Cargo.toml" -p mecmcp-redact --features mecmcp-redact/cli
cp "$src_dir/target/release/mecmcp-redact" "$bin_dir/mecmcp-redact"

echo "Built mecmcp-redact ($tag @ $resolved_commit) -> $bin_dir/mecmcp-redact"
