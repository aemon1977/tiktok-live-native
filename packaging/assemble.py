#!/usr/bin/env python3
"""Builds the AppDir inside an Ubuntu 24.04 container (run by packaging/inside.sh). Usage: assemble.py <AppDir> <src>

Bundles Python, GTK4/libadwaita/WebKitGTK 6, the GStreamer plugins the app uses and pactl, with their ldd closure.
Host-provided on purpose (must match the running system): glibc, GPU/Wayland stack, libstdc++, fontconfig/freetype,
PipeWire client (must match the server), p11-kit (distro TLS trust), dbus, udev.
"""
import os, re, shutil, stat, subprocess, sys
from pathlib import Path

APPDIR, SRC = Path(sys.argv[1]), Path(sys.argv[2])
U = APPDIR / "usr"; L = U / "lib/x86_64-linux-gnu"; SYSL = Path("/usr/lib/x86_64-linux-gnu")
WK_DIR = "/usr/lib/x86_64-linux-gnu/webkitgtk-6.0"           # compiled into libwebkitgtk: where helper processes live
WK_LINK = "/tmp/.ttln-webkitgtk6-" + "x" * (len(WK_DIR) - 22)  # same length, symlinked to the AppDir copy by AppRun
assert len(WK_LINK) == len(WK_DIR)

# libOpenGL (glvnd's thin GL entry point, needed by OBS) IS bundled: Debian/Ubuntu/Fedora ship it in an optional
# package (libopengl0 / libglvnd-opengl); it dispatches to the host's GL like the rest of glvnd.
# Prefix match on purpose: libwayland-client/-egl, libGLX_mesa, libGLESv2, libdrm_amdgpu... must all come from the host,
# or the host's Mesa ends up resolving against our older copies (seen: wl_display_create_queue_with_name missing).
HOST = re.compile(r"^(ld-linux|libc\.|libm\.|libdl\.|libpthread|librt\.|libresolv|libutil\.|libnsl|libanl|libmvec|libBrokenLocale|"
                  r"libthread_db|libGL|libEGL|libgbm|libdrm|libwayland-(client|egl|cursor)|libxcb-dri|libxshmfence|"
                  r"libstdc\+\+|libgcc_s|libfontconfig|libfreetype|libexpat|libpipewire-0\.3|libdbus-1|libudev)")

ELEMENTS = ("pipewiresrc v4l2src pulsesrc videotestsrc audiotestsrc compositor videoconvertscale videoconvert videorate audiomixer "
            "audioconvert audioresample x264enc avenc_aac h264parse aacparse flvmux rtmp2sink matroskamux filesink tee queue "
            "appsink jpegdec decodebin typefind fakesink capsfilter mp4mux level "
            # WebKit's own media/WebAudio stack (the chat page) aborts its web process without these
            "autoaudiosink autovideosink volume appsrc playbin3 uridecodebin3 pulsesink").split()


def sh(*a): return subprocess.run(a, capture_output=True, text=True).stdout


def deps(path):
    return [Path(m) for m in re.findall(r"=> (/\S+)", sh("ldd", str(path)))]


def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst, follow_symlinks=True)


def copytree(src, dst, ignore=None):
    shutil.copytree(src, dst, symlinks=False, ignore=ignore, dirs_exist_ok=True)


# ---- Python + bindings + app
copy("/usr/bin/python3.12", U / "bin/python3")
copytree("/usr/lib/python3.12", U / "lib/python3.12",
         ignore=shutil.ignore_patterns("test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3", "__pycache__"))
copytree("/usr/lib/python3/dist-packages", U / "lib/python3/dist-packages", ignore=shutil.ignore_patterns("__pycache__"))
app = U / "share/tiktok-live-native"
for f in ("app.py", "engine.py", "tiktok.py", "chat_overlay.py", "obs_launcher.py", "av_test.py", "selftest.py", "README.md"): copy(SRC / f, app / f)
copy(SRC / "packaging/smoke.py", app / "smoke.py")
copy(Path(sh("which", "pactl").strip()), U / "bin/pactl")

# ---- GObject data
copytree(SYSL / "girepository-1.0", L / "girepository-1.0")
copytree("/usr/share/glib-2.0/schemas", U / "share/glib-2.0/schemas")
subprocess.run(["glib-compile-schemas", str(U / "share/glib-2.0/schemas")], check=True)
for theme in ("Adwaita", "hicolor"): copytree(f"/usr/share/icons/{theme}", U / f"share/icons/{theme}")
copytree(SYSL / "gdk-pixbuf-2.0", L / "gdk-pixbuf-2.0")
for m in ("libgiognutls.so",): copy(SYSL / "gio/modules" / m, L / "gio/modules" / m)
copytree(WK_DIR, L / "webkitgtk-6.0")

# ---- OBS (integrated alternative engine): binary, libobs, the plugins a TikTok scene needs, data, Qt platform plugins.
# Left out: browser source (CEF, ~250 MB), pro capture cards (AJA/DeckLink), NVENC/QSV, VST, VLC, JACK, and
# frontend-tools (Python/Lua scripting: its interpreters expect unbundled modules and OBS segfaulted at startup).
OBS_PLUGINS = ("image-source linux-capture linux-pipewire linux-pulseaudio linux-v4l2 obs-ffmpeg obs-filters "
               "obs-outputs obs-transitions obs-websocket obs-x264 rtmp-services text-freetype2").split()
for b in ("obs", "obs-ffmpeg-mux"): copy(f"/usr/bin/{b}", U / "bin" / b)
for lib in SYSL.glob("libobs*.so*"): copy(lib, L / lib.name)
for pl in OBS_PLUGINS: copy(SYSL / "obs-plugins" / f"{pl}.so", L / "obs-plugins" / f"{pl}.so")
copytree("/usr/share/obs/libobs", U / "share/obs/libobs"); copytree("/usr/share/obs/obs-studio", U / "share/obs/obs-studio")
for pl in OBS_PLUGINS:
    if Path(f"/usr/share/obs/obs-plugins/{pl}").is_dir(): copytree(f"/usr/share/obs/obs-plugins/{pl}", U / f"share/obs/obs-plugins/{pl}")
for d in ("platforms", "wayland-decoration-client", "wayland-graphics-integration-client", "wayland-shell-integration",
          "xcbglintegrations", "imageformats", "iconengines", "platforminputcontexts", "egldeviceintegrations", "tls", "generic"):
    if (SYSL / "qt6/plugins" / d).is_dir(): copytree(SYSL / "qt6/plugins" / d, L / "qt6/plugins" / d)

# ---- GStreamer: only the plugins providing the elements we use (+ discoverer), and the registry scanner
gi_env = "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None)\n"
files = sh("python3", "-c", gi_env + f"print('\\n'.join(sorted({{Gst.ElementFactory.find(e).get_plugin().get_filename() for e in {ELEMENTS!r}}})))").split()
files += [str(SYSL / "gstreamer-1.0" / f) for f in ("libgstplayback.so", "libgsttypefindfunctions.so", "libgstapp.so")]
for f in set(files): copy(f, L / "gstreamer-1.0" / Path(f).name)
copy(SYSL / "gstreamer1.0/gstreamer-1.0/gst-plugin-scanner", L / "gstreamer1.0/gstreamer-1.0/gst-plugin-scanner")

# ---- shared library closure (ELF seeds + libraries the typelibs dlopen by soname)
ldcache = dict(re.findall(r"^\s*(\S+) \(libc6,x86-64.*\) => (\S+)$", sh("ldconfig", "-p"), re.M))
seeds = [p for p in APPDIR.rglob("*") if p.is_file() and (p.suffix == ".so" or ".so." in p.name or os.access(p, os.X_OK))
         and p.open("rb").read(4) == b"\x7fELF"]
for t in (L / "girepository-1.0").glob("*.typelib"):
    for so in set(re.findall(rb"lib[\w\-\.\+]+?\.so\.\d+", t.read_bytes())):
        if so.decode() in ldcache: seeds.append(Path(ldcache[so.decode()]))
seen, queue = set(), list(seeds)
while queue:
    for d in deps(queue.pop()):
        if d.name in seen or HOST.match(d.name): continue
        seen.add(d.name); copy(d, L / d.name); queue.append(d)
for p in seeds:  # dlopen'ed typelib libraries themselves
    if str(p).startswith("/") and not str(p).startswith(str(APPDIR)) and not HOST.match(p.name) and not (L / p.name).exists():
        copy(p, L / p.name)
print(f"bundled {len(list(L.glob('*.so*')))} libraries")

# ---- WebKit: helper-process path is compiled in; point it to a same-length /tmp symlink made by AppRun
for lib in L.glob("libwebkitgtk-6.0.so*"):
    data = lib.read_bytes(); n = data.count(WK_DIR.encode())
    assert n, f"{WK_DIR} not found in {lib}"
    lib.write_bytes(data.replace(WK_DIR.encode(), WK_LINK.encode())); print(f"patched {n} path(s) in {lib.name}")

# ---- OBS: data/plugin dirs are compiled in (env vars don't cover libobs' own data) -> same-length /tmp links (AppRun)
OBS_SUBST = {b"/usr/share/obs/": b"/tmp/.ttln-obs/",
             b"/usr/lib/x86_64-linux-gnu/obs-plugins": b"/tmp/.ttln-obsplug-" + b"x" * 18}
for k, v in OBS_SUBST.items(): assert len(k) == len(v), (k, v)
for f in [U / "bin/obs", *L.glob("libobs*.so*"), *(L / "obs-plugins").glob("*.so")]:
    data = f.read_bytes(); new = data
    for k, v in OBS_SUBST.items(): new = new.replace(k, v)
    if new != data: f.write_bytes(new); print(f"patched OBS paths in {f.name}")

# ---- gnutls: Ubuntu's build reads CAs from a fixed file that openSUSE/Fedora name differently -> same-length /tmp link
CA, CA_LINK = b"/etc/ssl/certs/ca-certificates.crt", b"/tmp/.ttln-ca-bundle-xxxxxxxxxxxxx"
assert len(CA) == len(CA_LINK)
for lib in L.glob("libgnutls.so*"):
    data = lib.read_bytes(); n = data.count(CA)
    if n: lib.write_bytes(data.replace(CA, CA_LINK)); print(f"patched {n} CA path(s) in {lib.name}")

# ---- AppRun, desktop entry, icon
apprun = (SRC / "packaging/AppRun").read_text().replace("@WK_LINK@", WK_LINK).replace("@CA_LINK@", CA_LINK.decode()) \
    .replace("@OBS_DATA_LINK@", "/tmp/.ttln-obs").replace("@OBS_PLUG_LINK@", OBS_SUBST[b"/usr/lib/x86_64-linux-gnu/obs-plugins"].decode())
(APPDIR / "AppRun").write_text(apprun); (APPDIR / "AppRun").chmod(0o755)
copy(SRC / "packaging/tiktok-live-native.desktop", APPDIR / "tiktok-live-native.desktop")
copy(SRC / "packaging/tiktok-live-native.svg", APPDIR / "tiktok-live-native.svg")
for p in APPDIR.rglob("*"):
    if p.is_file(): p.chmod(p.stat().st_mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
