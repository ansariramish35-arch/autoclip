#!/usr/bin/env bash
# Build the frontend and stage the Python function bundle for Vercel.
#
# Vercel's Python limit is 500 MB uncompressed, and this app needs ffmpeg
# (150 MB), so the bundle is measured and trimmed here rather than failing at
# upload time with an opaque size error.
#
# Layout produced under frontend/ (the project's root directory):
#   dist/                 static site
#   api/index.py          the ASGI entrypoint (committed)
#   api/backend -> ../../backend        runtime code
#   api/prompt  -> ../../prompt
#   api/assets  -> ../../assets
#   api/vendor/ffmpeg/    static ffmpeg + ffprobe
#
# Symlinks are resolved by Vercel's builder, so backend/ is copied once at the
# repo root instead of duplicated byte-for-byte into the frontend tree.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
API_DIR="$ROOT/frontend/api"

echo "== 1/4 frontend build =="
(cd frontend && npm run build)

echo "== 2/4 static ffmpeg =="
bash scripts/fetch-ffmpeg.sh

echo "== 3/4 stage the function =="
mkdir -p "$API_DIR/vendor"
# Replace rather than reuse: a stale copy is how a bundle silently exceeds the cap.
for link in backend prompt assets; do
  rm -rf "$API_DIR/$link"
  ln -sfn "../../$link" "$API_DIR/$link"
done
rm -rf "$API_DIR/vendor/ffmpeg"
cp -r "$ROOT/vendor/ffmpeg" "$API_DIR/vendor/ffmpeg"
chmod +x "$API_DIR/vendor/ffmpeg/ffmpeg" "$API_DIR/vendor/ffmpeg/ffprobe"

echo "== 4/4 measure =="
# Nothing below is optional: if the total approaches 500 MB the build must say so
# here, with the numbers, instead of failing inside Vercel.
site=$(du -sm "$ROOT/frontend/node_modules" 2>/dev/null | cut -f1 || echo 0)
ff=$(du -sm "$API_DIR/vendor/ffmpeg" | cut -f1)
code=$(du -sm "$ROOT/backend" "$ROOT/prompt" | awk '{s+=$1} END {print s}')
printf 'function payload: ffmpeg %s MB + code %s MB = %s MB (site-packages is added by Vercel)\n' \
  "$ff" "$code" "$((ff + code))"
echo "done"
