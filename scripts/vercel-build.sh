#!/usr/bin/env bash
# Vercel build for the whole app: the static frontend plus the Python function.
#
# Order matters. Vercel runs this for the project's build step, then packages
# frontend/api/ as a Serverless Function. Two things must exist before that
# packaging:
#   1. ffmpeg + ffprobe, because the Python image ships none.
#   2. the runtime trees (backend/, prompt/, assets/) where the entrypoint looks.
#
# The function's root directory is frontend/, so ../../backend is outside it.
# The trees are copied rather than symlinked: packaging runs the function dir
# through a zip-like step, and a symlink is not guaranteed to be dereferenced.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
API_DIR="$ROOT/frontend/api"

echo "== 1/4 static ffmpeg =="
bash scripts/fetch-ffmpeg.sh

echo "== 2/4 stage the function tree =="
# Replace, never reuse: a stale copy is how a bundle silently outgrows the cap.
for tree in backend prompt; do
  rm -rf "$API_DIR/$tree"
  cp -r "$ROOT/$tree" "$API_DIR/$tree"
done
# Not imported at runtime; 17 MB of the budget.
rm -rf "$API_DIR/backend/tests"
rm -rf "$API_DIR/vendor"
mkdir -p "$API_DIR/vendor"
cp -r "$ROOT/vendor/ffmpeg" "$API_DIR/vendor/ffmpeg"
chmod +x "$API_DIR/vendor/ffmpeg/ffmpeg" "$API_DIR/vendor/ffmpeg/ffprobe"

echo "== 3/4 frontend =="
(cd frontend && npm run build)

echo "== 4/4 measure the function payload =="
ff=$(du -sm "$API_DIR/vendor/ffmpeg" | cut -f1)
code=$(du -sm "$API_DIR/backend" "$API_DIR/prompt" 2>/dev/null | awk '{s+=$1} END {print s}')
printf 'ffmpeg %s MB + code %s MB; Vercel adds site-packages (~312 MB) for a ~500 MB cap\n' \
  "$ff" "$code"
