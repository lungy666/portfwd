#!/usr/bin/env bash
# 仅刷新已构建/已安装 app 的图标（不改代码、不重新构建）。
# 用法:
#   scripts/refresh_app_icon.sh [app路径]
#   默认更新 /Applications/portfwd.app；也可以传 build/macos/portfwd.app。
# 前提: assets/icon_macos.png 已是最新（由 assets/icon_macos.svg 渲染）。
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
app="${1:-/Applications/portfwd.app}"
src_png="$repo_root/assets/icon_macos.png"

if [[ ! -d "$app" ]]; then
  printf 'App bundle not found: %s\n' "$app" >&2
  exit 1
fi
if [[ ! -r "$src_png" ]]; then
  printf 'Icon source not found: %s\n' "$src_png" >&2
  exit 1
fi

iconset_dir="$(mktemp -d)/icon.iconset"
mkdir -p "$iconset_dir"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$src_png" \
    --out "$iconset_dir/icon_${size}x${size}.png" >/dev/null
  sips -z "$((size * 2))" "$((size * 2))" "$src_png" \
    --out "$iconset_dir/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset_dir" -o "$app/Contents/Resources/AppIcon.icns"

# CFBundleIconName points at the icon embedded in Assets.car and takes
# precedence over CFBundleIconFile. Remove it so the refreshed ICNS is used.
info_plist="$app/Contents/Info.plist"
if /usr/libexec/PlistBuddy -c "Print :CFBundleIconName" "$info_plist" >/dev/null 2>&1; then
  /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$info_plist"
fi
codesign --force --sign - "$app"
touch "$app"
printf 'Updated icon for: %s\n' "$app"
printf 'Dock 若未刷新，可执行: killall Dock\n'
