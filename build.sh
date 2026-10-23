#!/bin/sh
# build.sh -- rebuild the Showmo WinEye thingino image from source.
# Usage: ./build.sh [work-dir]          (default: ./thingino-firmware)
# Needs a Linux build host with the thingino build dependencies and ~40 GB free.
# The result contains no vendor file: the sensor calibration (IQ) is copied
# from each camera by install.py.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${1:-$HERE/thingino-firmware}
BASE=9925f46f73b9c39ec8d2a65134b18c94e6fdfb97  # thingino-firmware master, 2026-10-20
CAMERA=showmo_prj007_t32nq_gc8602_aic8800d80

for tool in git make gcc lzop python3; do
	command -v "$tool" >/dev/null || { echo "missing build tool: $tool" >&2; exit 1; }
done

if [ ! -d "$WORK/.git" ]; then
	git clone https://github.com/themactep/thingino-firmware "$WORK"
fi
cd "$WORK"
git checkout -q "$BASE"
git submodule update --init --recursive
git checkout -q -B showmo-wineye "$BASE"
git am -q "$HERE"/patches/*.patch

make GROUP=exp CAMERA=$CAMERA

img=$(ls output/*/${CAMERA}-*/images/thingino-${CAMERA}.bin)
cp "$img" "$HERE/showmo-wineye-thingino.bin"
echo
echo "Image: $HERE/showmo-wineye-thingino.bin"
sha256sum "$HERE/showmo-wineye-thingino.bin"
