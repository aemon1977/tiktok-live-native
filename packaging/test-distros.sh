#!/bin/bash
# Runs the AppImage's --selftest in clean containers of several distros. Each gets only what any desktop install
# already has (fonts, Mesa, Wayland, PipeWire client, D-Bus, CA certs); everything else must come from the AppImage.
# Audio check uses the host's PipeWire-Pulse socket. Usage: packaging/test-distros.sh [distro...]
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; IMG=/src/dist/TikTok-LIVE-Native-x86_64.AppImage
PULSE="/run/user/$(id -u)/pulse/native"
# Use the host's audio server when there is one (desktop); CI runners have none -> the audio check is skipped.
if [ -S "$PULSE" ]; then AUDIO=(-v "$PULSE:/tmp/pulse.sock" -e PULSE_SERVER=unix:/tmp/pulse.sock); else AUDIO=(-e TTLN_NO_AUDIO=1); fi
declare -A IMAGE=([ubuntu24]=ubuntu:24.04 [debian13]=debian:trixie [fedora]=fedora:latest [arch]=archlinux:latest [opensuse]=opensuse/tumbleweed)
declare -A SETUP=(
 [ubuntu24]="apt-get update -qq && apt-get install -y -qq libfontconfig1 libegl1 libgl1 libwayland-client0 libwayland-egl1 libwayland-cursor0 libpipewire-0.3-0t64 libdbus-1-3 libudev1 ca-certificates fonts-dejavu-core >/dev/null"
 [debian13]="apt-get update -qq && apt-get install -y -qq libfontconfig1 libegl1 libgl1 libwayland-client0 libwayland-egl1 libwayland-cursor0 libpipewire-0.3-0t64 libdbus-1-3 libudev1 ca-certificates fonts-dejavu-core >/dev/null"
 [fedora]="dnf -y -q install fontconfig mesa-libEGL mesa-libGL libwayland-client libwayland-egl libwayland-cursor pipewire-libs dbus-libs systemd-libs ca-certificates dejavu-sans-fonts >/dev/null"
 [arch]="pacman -Sy --noconfirm -q fontconfig mesa libglvnd wayland libpipewire dbus systemd-libs ca-certificates ttf-dejavu >/dev/null"
 [opensuse]="zypper -q -n in fontconfig Mesa-libEGL1 Mesa-libGL1 libwayland-client0 libwayland-egl1 libwayland-cursor0 libpipewire-0_3-0 libdbus-1-3 libudev1 ca-certificates dejavu-fonts >/dev/null"
)
for d in "${@:-ubuntu24 debian13 fedora arch opensuse}"; do
  for d in $d; do
    echo "===== $d (${IMAGE[$d]})"
    docker run --rm -v "$ROOT:/src:ro" "${AUDIO[@]}" -e APPIMAGE_EXTRACT_AND_RUN=1 \
      "${IMAGE[$d]}" bash -c "${SETUP[$d]} && grep -m1 PRETTY_NAME /etc/os-release && ldd --version | head -1 && cd /tmp && $IMG --selftest 2>&1 | grep -E '=(PASS|FAIL)'"
  done
done
