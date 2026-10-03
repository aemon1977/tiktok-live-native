#!/usr/bin/env python3
"""AppImage self-check (`TikTok-LIVE-Native.AppImage --selftest`): bundled libraries, GStreamer elements, a real
H.264+AAC encode, and HTTPS through both TLS stacks (Python requests; GIO/gnutls = what WebKit uses). No TikTok calls
that change anything: only a plain GET of the public home page."""
import os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
ok = True


def check(name, fn):
    global ok
    try: detail = fn(); print(f"{name}=PASS" + (f" {detail}" if detail else ""))
    except Exception as e: ok = False; print(f"{name}=FAIL {type(e).__name__}: {e}")


import gi


def typelibs():
    for ns, v in (("Gtk", "4.0"), ("Gdk", "4.0"), ("Adw", "1"), ("WebKit", "6.0"), ("Soup", "3.0"), ("Gst", "1.0"),
                  ("GstPbutils", "1.0"), ("GdkX11", "4.0")):
        gi.require_version(ns, v); __import__("gi.repository." + ns)
    from gi.repository import Adw, Gtk, WebKit
    return f"gtk={Gtk.get_major_version()}.{Gtk.get_minor_version()} adw={Adw.get_major_version()}.{Adw.get_minor_version()} webkit={WebKit.get_major_version()}.{WebKit.get_minor_version()}"


def elements():
    from gi.repository import Gst
    Gst.init(None)
    need = ("pipewiresrc v4l2src pulsesrc videotestsrc audiotestsrc compositor videoconvertscale videorate audiomixer audioconvert "
            "audioresample x264enc avenc_aac h264parse aacparse flvmux rtmp2sink matroskamux mp4mux level filesink tee queue appsink "
            "jpegdec").split()
    missing = [e for e in need if not Gst.ElementFactory.find(e)]
    if missing: raise RuntimeError("faltan " + " ".join(missing))
    return f"{len(need)} elementos, GStreamer {Gst.version_string().split()[-1]}"


def encode():
    import tempfile, engine as E
    from gi.repository import Gst
    out = os.path.join(tempfile.mkdtemp(), "t.mkv")
    p = Gst.parse_launch(f"videotestsrc num-buffers=60 ! video/x-raw,width=1280,height=720,framerate=30/1 ! videoconvertscale n-threads=4 ! "
                         f"video/x-raw,format=I420 ! x264enc speed-preset=faster ! h264parse ! matroskamux name=m ! filesink location={out} "
                         f"audiotestsrc num-buffers=60 ! audioconvert ! avenc_aac ! aacparse ! m.")
    p.set_state(Gst.State.PLAYING)
    msg = p.get_bus().timed_pop_filtered(30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR); p.set_state(Gst.State.NULL)
    if not msg or msg.type != Gst.MessageType.EOS: raise RuntimeError(msg.parse_error()[0].message if msg else "timeout")
    codecs = sorted(s["codec_name"] for s in E.probe(out))
    if codecs != ["aac", "h264"]: raise RuntimeError(f"streams={codecs}")
    return "h264+aac"


def https_requests():
    import requests
    return f"HTTP {requests.get('https://www.tiktok.com/', timeout=15).status_code} (TLS OK)"


def https_gio():  # same TLS path (glib-networking + system trust) as the WebKit chat view
    from gi.repository import GLib, Soup
    s = Soup.Session(); m = Soup.Message.new("GET", "https://www.tiktok.com/")
    s.send_and_read(m, None)
    return f"HTTP {int(m.get_status())} (TLS OK)"


def pactl():
    import subprocess
    if os.environ.get("TTLN_NO_AUDIO"): return "SKIP (sin servidor de audio en este entorno)"
    r = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=5)
    if r.returncode: raise RuntimeError(r.stderr.strip() or "sin servidor de audio")
    return next((l.split(":", 1)[1].strip() for l in r.stdout.splitlines() if l.startswith("Server Name")), "")


def obs():  # integrated OBS binary + its libraries resolve on this distro (no display needed for --version)
    import subprocess, obs_launcher as OL
    b = OL.obs_binary()
    if not b: return "SKIP (OBS no incluido: ejecución desde código)"
    r = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20)
    if "OBS Studio" not in r.stdout: raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
    plugs = sorted(p.stem for p in (Path(b).parent.parent / "lib/x86_64-linux-gnu/obs-plugins").glob("*.so"))
    return r.stdout.strip() + f" · {len(plugs)} plugins"


for name, fn in (("TYPELIBS", typelibs), ("GST_ELEMENTS", elements), ("ENCODE", encode), ("HTTPS_PYTHON", https_requests),
                 ("HTTPS_GIO", https_gio), ("AUDIO_SERVER", pactl), ("OBS", obs)):
    check(name, fn)
print("SELFTEST=" + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
