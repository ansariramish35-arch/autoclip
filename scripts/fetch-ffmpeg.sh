#!/usr/bin/env bash
# Put a static ffmpeg + ffprobe into vendor/ffmpeg for the Vercel function.
# Binaries are NOT committed (157 MB of binary in git is not acceptable);
# the Vercel build runs this and the result is bundled into the function.
set -euo pipefail
cd "$(dirname "$0")/.."
dest="vendor/ffmpeg"
mkdir -p "$dest"
if [[ -x "$dest/ffmpeg" && -x "$dest/ffprobe" ]]; then
  echo "ffmpeg already present in $dest"
  exit 0
fi

# ffmpeg-binaries ships a static ffmpeg + ffprobe for manylinux x86_64.
python3 -m pip download --no-deps --no-cache-dir -d /tmp/ffdl ffmpeg-binaries==1.1.0 >/dev/null
wheel=$(ls /tmp/ffdl/ffmpeg_binaries-*.whl | head -1)
unzip -o -q "$wheel" 'ffmpeg/binaries/ffmpeg' 'ffmpeg/binaries/ffprobe' -d /tmp/ffdl/x
cp /tmp/ffdl/x/ffmpeg/binaries/ffmpeg /tmp/ffdl/x/ffmpeg/binaries/ffprobe "$dest/"
chmod +x "$dest/ffmpeg" "$dest/ffprobe"
"$dest/ffmpeg" -version | head -1
echo "installed $(du -sh "$dest" | cut -f1) into $dest"
