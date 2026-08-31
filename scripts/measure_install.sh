#!/usr/bin/env bash
# Measure the installed size of primer-ai core and each extra.
# Usage: scripts/measure_install.sh [extra ...]   (default: core only)
# Each target builds a throwaway venv and prints the site-packages size.
# Heavy extras (huggingface) download multi-GB wheels; pass them
# explicitly only when you mean it.
set -euo pipefail
cd "$(dirname "$0")/.."
targets=("core" "$@")
printf '%-14s %s\n' TARGET SIZE
for target in "${targets[@]}"; do
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  uv venv --quiet "$tmp/venv"
  if [ "$target" = "core" ]; then spec="."; else spec=".[${target}]"; fi
  uv pip install --quiet --python "$tmp/venv/bin/python" "$spec"
  size="$(du -sh "$tmp/venv/lib/python"*/site-packages | cut -f1)"
  printf '%-14s %s\n' "$target" "$size"
  rm -rf "$tmp"
  trap - EXIT
done
