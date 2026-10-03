#!/bin/bash
# Runs inside ubuntu:24.04 (see build-appimage.sh). Oldest base with GTK4 + libadwaita>=1.5 + WebKitGTK 6 + GStreamer>=1.22,
# so the AppImage needs glibc>=2.39: Ubuntu 24.04+, Mint 22+, Debian 13, Fedora 40+, Arch, openSUSE Tumbleweed.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# OBS: official OBS Project build (Ubuntu's 30.0.2 lacks the PipeWire/Wayland screen capture plugin)
apt-get install -y -qq --no-install-recommends software-properties-common gpg-agent >/dev/null
add-apt-repository -y ppa:obsproject/obs-studio >/dev/null && apt-get update -qq
apt-get install -y -qq --no-install-recommends obs-studio qt6-wayland >/dev/null
apt-get install -y -qq --no-install-recommends \
  python3 python3-gi python3-requests gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 gir1.2-soup-3.0 \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-pipewire gstreamer1.0-pulseaudio \
  pulseaudio-utils glib-networking libglib2.0-bin librsvg2-common adwaita-icon-theme ca-certificates wget file >/dev/null
rm -rf /build && mkdir -p /build /src/dist
python3 /src/packaging/assemble.py /build/AppDir /src
cd /build
wget -q https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage -O appimagetool
chmod +x appimagetool
VERSION="$(python3 -c 'import re;print(re.search(r"VERSION = \"(.+?)\"", open("/src/engine.py").read())[1])')" ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 ./appimagetool -n /build/AppDir /src/dist/.building.AppImage
# rename instead of overwrite: the previous AppImage may be running ("Text file busy")
mv -f /src/dist/.building.AppImage /src/dist/TikTok-LIVE-Native-x86_64.AppImage
chown -R "${HOST_UID:-0}:${HOST_GID:-0}" /src/dist
