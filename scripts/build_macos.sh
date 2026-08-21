#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf '%s\n' "macOS is required to build the macOS bundle." >&2
  exit 1
fi

if ! xcrun --find xcodebuild >/dev/null 2>&1; then
  printf '%s\n' "Full Xcode is required. Install it, then run: xcode-select --switch /Applications/Xcode.app/Contents/Developer" >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
if command -v flet >/dev/null 2>&1; then
  flet_bin="$(command -v flet)"
elif [[ -x "$repo_root/.venv/bin/flet" ]]; then
  flet_bin="$repo_root/.venv/bin/flet"
else
  printf '%s\n' "flet-cli is missing. Install with: python -m pip install -e '.[build]'" >&2
  exit 1
fi

build_version="${BUILD_VERSION:-0.1.0}"
build_number="${BUILD_NUMBER:-1}"
macos_arch="${MACOS_ARCH:-}"
if [[ -z "$macos_arch" ]]; then
  case "$(uname -m)" in
    arm64) macos_arch="arm64" ;;
    x86_64) macos_arch="x64" ;;
    *)
      printf 'Unsupported macOS architecture: %s\n' "$(uname -m)" >&2
      exit 1
      ;;
  esac
fi

"$flet_bin" build macos \
  --arch "$macos_arch" \
  --artifact portfwd \
  --product portfwd \
  --bundle-id com.portfwd.desktop \
  --build-version "$build_version" \
  --build-number "$build_number" \
  --output build/macos \
  "$@"

# flet/Flutter 生成的 icns 最大只有 256px，用 iconutil 从 1024 源图重新生成
# 完整多尺寸 icns（16–512），并重新 ad-hoc 签名
app_bundle="$repo_root/build/macos/portfwd.app"
iconset_dir="$(mktemp -d)/icon.iconset"
mkdir -p "$iconset_dir"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$repo_root/assets/icon_macos.png" \
    --out "$iconset_dir/icon_${size}x${size}.png" >/dev/null
  sips -z "$((size * 2))" "$((size * 2))" "$repo_root/assets/icon_macos.png" \
    --out "$iconset_dir/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset_dir" -o "$app_bundle/Contents/Resources/AppIcon.icns"

# Flet also embeds the generated app icon in Assets.car. CFBundleIconName makes
# macOS prefer that stale asset over the AppIcon.icns replaced above.
info_plist="$app_bundle/Contents/Info.plist"
if /usr/libexec/PlistBuddy -c "Print :CFBundleIconName" "$info_plist" >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$info_plist"
fi
codesign --force --sign - "$app_bundle"
