#!/bin/bash
# android_sdk.sh -- clone ReXGlue SDK v0.10.0 (the upstream tag the Android
# patches were written against), init submodules, apply the Android patch set
# from hells-gate-recomp-android (deivid22srk), stage SDL3's Java glue.
#
#   tools/android_sdk.sh [dest=android/sdk/rexglue-sdk]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SDK_DIR="${1:-$ROOT/android/sdk/rexglue-sdk}"
SDK_REPO="https://github.com/rexglue/rexglue-sdk.git"
SDK_TAG="v0.10.0"
PATCHES="$ROOT/android/patches"

if [ -f "$SDK_DIR/.patches-applied" ]; then
  echo "[android_sdk] SDK already set up at $SDK_DIR"
else
  rm -rf "$SDK_DIR"; mkdir -p "$(dirname "$SDK_DIR")"
  echo "[android_sdk] cloning $SDK_REPO@$SDK_TAG"
  git clone --depth 1 --branch "$SDK_TAG" "$SDK_REPO" "$SDK_DIR"
  git -C "$SDK_DIR" submodule update --init --depth 1
  for p in rexglue-sdk-v0.10.0.patch rexglue-sdk-v0.10.0-android.patch rexglue-sdk-v0.10.0-android-perf.patch rexglue-sdk-v0.10.0-tolerant-dispatch.patch rexglue-sdk-v0.10.0-android-custom-vulkan.patch rexglue-sdk-v0.10.0-fps-cap.patch rexglue-sdk-v0.10.0-android-quiet.patch rexglue-sdk-v0.10.0-signal-chain.patch; do
    echo "[android_sdk] applying $p"
    git -C "$SDK_DIR" apply "$PATCHES/$p"
  done
  touch "$SDK_DIR/.patches-applied"
fi

# libadrenotools (BSD-2): loads a user-supplied Turnip / Adreno driver on
# Qualcomm phones. Pinned commits; built as part of the app's CMake project.
AT_DIR="$(dirname "$SDK_DIR")/libadrenotools"
AT_REV="8fae8ce254dfc1344527e05301e43f37dea2df80"
LNB_REV="aa3975893d83ef1bc84c321ec60c65fbf1287887"
if [ ! -f "$AT_DIR/CMakeLists.txt" ]; then
  git clone https://github.com/bylaws/libadrenotools.git "$AT_DIR"
  git -C "$AT_DIR" checkout -q "$AT_REV"
  git clone https://github.com/bylaws/liblinkernsbypass.git "$AT_DIR/lib/linkernsbypass"
  git -C "$AT_DIR/lib/linkernsbypass" checkout -q "$LNB_REV"
  echo "[android_sdk] libadrenotools -> $AT_DIR"
fi

SDL_JAVA_SRC="$SDK_DIR/thirdparty/sdl3/android-project/app/src/main/java/org/libsdl/app"
SDL_JAVA_DST="$ROOT/android/app/src/main/java/org/libsdl/app"
mkdir -p "$SDL_JAVA_DST"
cp "$SDL_JAVA_SRC"/*.java "$SDL_JAVA_DST/"
echo "[android_sdk] SDL3 Java glue -> $SDL_JAVA_DST"
echo "[android_sdk] done"
