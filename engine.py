#!/usr/bin/env python3
"""A/V engine: GNOME ScreenCast portal + V4L2 camera + Pulse mic -> compositor -> preview / MKV / FLV-RTMP.

Validated rules kept from v17.6-v20:
- The portal session and the FD from OpenPipeWireRemote stay alive for the whole capture.
- pipewiresrc negotiates its native format; conversion/rate happen downstream.
"""
import json, os, re, subprocess, time
from pathlib import Path
from urllib.parse import urlsplit
import gi
gi.require_version("Gio", "2.0"); gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst
Gst.init(None)

VERSION = "1.2.1"
HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("TTLN_DATA_DIR") or HERE)  # AppRun points this to ~/.local/share/tiktok-live-native
LOGS = DATA / "logs"; CFG = DATA / "config"
LOGS.mkdir(parents=True, exist_ok=True); CFG.mkdir(parents=True, exist_ok=True)

FORMATS = {"vertical720": (720, 1280), "vertical": (1080, 1920), "horizontal": (1920, 1080)}
FORMAT_NAMES = {"vertical720": "Vertical 720×1280", "vertical": "Vertical 1080×1920", "horizontal": "Horizontal 1920×1080"}
QUALITY = {"25": 25, "30": 30, "60": 60}  # fps
QUALITY_NAMES = {"25": "25 FPS (conexión lenta)", "30": "30 FPS (recomendado)", "60": "60 FPS (más fluido, más subida)"}
BITRATE = {(False, 25): 2500, (False, 30): 3000, (False, 60): 4500, (True, 25): 4000, (True, 30): 4500, (True, 60): 6000}
THREADS = min(8, os.cpu_count() or 2)


def video_kbps(cfg):
    """Video bitrate by canvas size (720p vs 1080p class) and fps; 1080p at 720p's bitrate looked soft."""
    W, H = FORMATS[cfg["orientation"]]
    return BITRATE[(W * H > 1280 * 720, QUALITY[cfg["quality"]])]
LAYOUT_NAMES = {"pip": "Pantalla + cámara PiP", "screen": "Pantalla completa", "camera": "Cámara completa", None: "Sin vídeo"}
DEFAULTS = {"title": "", "engine": "app", "record_live": False, "chat_overlay": True, "desktop_audio": True, "camera_mode": "", "orientation": "vertical720", "quality": "25", "screen": True, "camera": False, "camera_device": "",
            "microphone": True, "mic_device": "", "pip_size": 0.30, "pip_x": 1.0, "pip_y": 0.0}


def recordings_dir():
    """~/Vídeos/TikTok LIVE (XDG Videos dir, localized), visible to the user even when running from the AppImage."""
    videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS) or str(Path.home() / "Videos")
    d = Path(videos) / "TikTok LIVE"; d.mkdir(parents=True, exist_ok=True); return d


def install_appimage_thumbnailer():
    """File managers (GNOME Files, Nemo, Caja…) only show an AppImage's own icon with an AppImage thumbnailer, which
    distros don't ship. Install a user-level one (reads .DirIcon with unsquashfs, never runs the AppImage) once."""
    src = HERE / "appimage-icon.thumbnailer"
    dst = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "thumbnailers/appimage-icon.thumbnailer"
    if not src.exists() or (dst.exists() and dst.read_text() == src.read_text()): return False
    dst.parent.mkdir(parents=True, exist_ok=True); dst.write_text(src.read_text())
    appimage = os.environ.get("APPIMAGE")  # forget GNOME's cached "no thumbnail" for this file so it retries
    if appimage:
        import hashlib
        md5 = hashlib.md5(Path(appimage).resolve().as_uri().encode()).hexdigest()
        for p in (Path.home() / ".cache/thumbnails").glob(f"**/{md5}.png"): p.unlink(missing_ok=True)
    return True


def host_env():
    """Environment for host programs (file manager...): without the AppImage's library/plugin overrides."""
    drop = ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "GI_TYPELIB_PATH", "GIO_MODULE_DIR", "GSETTINGS_SCHEMA_DIR",
            "GDK_PIXBUF_MODULE_FILE", "GTK_EXE_PREFIX", "GTK_DATA_PREFIX", "WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "GDK_BACKEND")
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("GST_")}
    if "TTLN_HOST_XDG_DATA_DIRS" in env: env["XDG_DATA_DIRS"] = env["TTLN_HOST_XDG_DATA_DIRS"]
    return env


def load_cfg():
    try: d = json.loads((CFG / "broadcast.json").read_text())
    except Exception: d = {}
    cfg = {**DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS}}
    cfg["camera_device"] = cfg["camera_device"].split(" — ")[0]  # v20 stored "/dev/videoN — name"
    if cfg["orientation"] not in FORMATS: cfg["orientation"] = "vertical720"
    if str(cfg["quality"]) not in QUALITY: cfg["quality"] = "25"
    cfg["quality"] = str(cfg["quality"])
    return cfg


def save_cfg(cfg): (CFG / "broadcast.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def layout(cfg):
    return "pip" if cfg["screen"] and cfg["camera"] else "screen" if cfg["screen"] else "camera" if cfg["camera"] else None


class Log:
    """Diagnostic log under ./logs. Registered secrets are masked on every write."""
    secrets = set()  # shared by all logs: an RTMP URL registered once is masked everywhere

    def __init__(self, name, fresh=True, echo=None):
        self.path = LOGS / name; self.echo = echo
        if fresh: self.path.write_text("")

    def __call__(self, s):
        s = mask(str(s))
        with self.path.open("a", encoding="utf8") as f: f.write(time.strftime("%H:%M:%S ") + s + "\n")
        if self.echo: self.echo(s)


def mask(s):
    for x in sorted(Log.secrets, key=len, reverse=True):
        if x: s = s.replace(x, "***")
    return s


# ---------------------------------------------------------------- devices

def camera_modes(structs, target=1280 * 720):
    """System-memory modes for v4l2src, one per resolution, best first: >=24 fps, then 16:9, then closest to target
    size, raw before MJPEG. Only discrete sizes/lists are handled (what UVC devices report); ranges are skipped."""
    best = {}
    ints = lambda k, s: [int(x) for x in re.findall(r"\d+", (re.search(k + r"=\(int\)(\{[^}]*\}|\d+)", s) or ["", ""])[1])]
    for s in structs:
        m = re.match(r"(video/x-raw|image/jpeg)\b", s)
        if not m or "DMA_DRM" in s: continue
        fr = re.search(r"framerate=\(fraction\)(\{[^}]*\}|[\d/]+)", s)
        rates = [(int(a), int(b)) for a, b in re.findall(r"(\d+)/(\d+)", fr[1]) if int(a) and int(b)] if fr else []
        if not rates: continue
        ok = [r for r in rates if 24 <= r[0] / r[1] <= 30]
        rate = max(ok, key=lambda r: r[0] / r[1]) if ok else min(rates, key=lambda r: abs(r[0] / r[1] - 30))
        jpeg = m[1] == "image/jpeg"; fps = rate[0] / rate[1]
        fmt = re.search(r"format=\(string\)(\w+)", s)  # single format only; lists are left to v4l2 negotiation
        for w in ints("width", s):
            for h in ints("height", s):
                caps = (f"image/jpeg,width={w},height={h},framerate={rate[0]}/{rate[1]}" if jpeg else
                        f"video/x-raw,{'format=' + fmt[1] + ',' if fmt else ''}width={w},height={h},framerate={rate[0]}/{rate[1]}")
                key = (fps >= 24, abs(w * 9 - h * 16) < w // 50, -abs(w * h - target), not jpeg)
                mode = {"caps": caps, "jpeg": jpeg, "w": w, "h": h, "fps": fps,
                        "label": f"{w}×{h} · {fps:g} fps" + (" · MJPEG" if jpeg else "")}
                if (w, h) not in best or key > best[(w, h)][0]: best[(w, h)] = (key, mode)
    return [m for _, m in sorted(best.values(), key=lambda km: km[0], reverse=True)]


def pick_camera_caps(structs, target=1280 * 720):
    modes = camera_modes(structs, target)
    return modes[0] if modes else None


def list_cameras():
    """[{path, name, modes, + best mode's caps/jpeg/w/h/fps/label}] for V4L2 capture devices (metadata nodes are not listed)."""
    prov = Gst.DeviceProviderFactory.get_by_name("v4l2deviceprovider")
    if not prov: return []
    prov.start()
    try: devs = prov.get_devices()
    finally: prov.stop()
    out = []
    for d in devs:
        caps = d.get_caps()
        structs = [caps.get_structure(i).to_string() for i in range(caps.get_size())
                   if not caps.get_features(i).contains("memory:DMABuf")]
        modes = camera_modes(structs)
        if modes: out.append({"path": d.get_properties().get_value("device.path"), "name": d.get_display_name(), "modes": modes, **modes[0]})
    return sorted(out, key=lambda c: c["path"])


def list_mics():
    """[(pulse source name, description)] excluding monitors."""
    try: srcs = json.loads(subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, timeout=5).stdout)
    except Exception: return []
    return [(s["name"], s["description"] if s.get("description") not in (None, "(null)") else s["name"]) for s in srcs
            if not s["name"].endswith(".monitor") and not s.get("monitor_source")]


def pactl_json(*args):
    try: return json.loads(subprocess.run(["pactl", "-f", "json", *args], capture_output=True, text=True, timeout=5,
                                          env={**os.environ, "LC_ALL": "C"}).stdout or "[]")
    except Exception: return []


def desktop_monitor():
    """Monitor of the output where sound is actually playing: the default output if something plays there, otherwise
    the output an active stream uses (e.g. the game moved to wireless headphones), otherwise the default output.
    A fixed default-at-start monitor went silent for 19 min when the game's sound moved to another output (2026-10-02)."""
    try: default = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception: default = ""
    name = pick_output(default, pactl_json("list", "sinks"), pactl_json("list", "sink-inputs"))
    return name + ".monitor" if name else None


def pick_output(default, sinks, inputs):
    """Output to capture: default if something plays there (or nothing plays anywhere), else the output of the first
    playing stream. Corked (paused) and muted streams don't count."""
    names = {x["index"]: x["name"] for x in sinks}
    playing = [i["sink"] for i in inputs if not i.get("corked") and not i.get("mute")]
    if not playing or any(names.get(k) == default for k in playing): return default
    return names.get(playing[0], default)


def summary(cfg, cams=None, title="FUENTES DE PRUEBA"):
    W, H = FORMATS[cfg["orientation"]]; F, KB = QUALITY[cfg["quality"]], video_kbps(cfg)
    cam = next((c for c in cams or [] if c["path"] == cfg["camera_device"]), None)
    if cam: cam = {**cam, **next((m for m in cam["modes"] if m["label"] == cfg["camera_mode"]), {})}
    real = title == "EMISIÓN REAL"
    return "\n".join([title, "", *([f"Título: {cfg['title'] or '(sin título)'}"] if real else []),
        *([f"Grabación local: " + ("Sí → recordings/" if cfg["record_live"] else "No")] if real else []),
        "Pantalla: " + ("Sí (portal GNOME)" if cfg["screen"] else "No"),
        "Cámara: " + ((cfg["camera_device"] or "NO SELECCIONADA") + (f" ({cam['name']}, {cam['label']})" if cam else "")
                      if cfg["camera"] else "No"),
        "Micrófono: " + ((cfg["mic_device"] or "predeterminado") if cfg["microphone"] else "No"),
        "Audio del equipo: " + (f"Sí ({desktop_monitor() or 'NO DISPONIBLE'})" if cfg["desktop_audio"] else "No"),
        f"Resolución: {W}x{H}", f"FPS: {F} · vídeo {KB} kbps · audio AAC 128 kbps 48 kHz",
        f"Layout: {LAYOUT_NAMES[layout(cfg)]}"])


# ---------------------------------------------------------------- portal

class ScreenPortal:
    """GNOME ScreenCast session. Keep the object alive while capturing: the PipeWire FD belongs to the session."""
    D = "org.freedesktop.portal.Desktop"; O = "/org/freedesktop/portal/desktop"; I = "org.freedesktop.portal.ScreenCast"
    TOKEN = CFG / "portal.json"  # restore_token: lets GNOME reuse the last chosen screen without a dialog

    def __init__(self, log):
        self.log = log; self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name().replace(".", "_").lstrip(":")
        self.session = None; self.fd = None; self.node = None; self.monitor = True

    def _request(self, method, sig, args, opts, cb):
        tok = f"tt_{method.lower()}_{time.time_ns()}"
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{tok}"
        sid = []
        def resp(c, s, p, i, sg, par, u):
            self.bus.signal_unsubscribe(sid[0]); code, data = par.unpack(); self.log(f"{method}={code}")
            if code: return self.on_error(f"{method} cancelado/fallido (código {code})")
            try: cb(data)
            except Exception as e: self.on_error(f"{method}: {e!r}")
        # Subscribe before calling: avoids missing a fast Response.
        sid.append(self.bus.signal_subscribe(self.D, "org.freedesktop.portal.Request", "Response", path, None,
                                             Gio.DBusSignalFlags.NONE, resp, None))
        self.bus.call_sync(self.D, self.O, self.I, method, GLib.Variant(sig, args + ({**opts, "handle_token": GLib.Variant("s", tok)},)),
                           GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 5000, None)

    def start(self, on_ready, on_error, forget=False, types=3):
        """types: 1 monitor, 2 window, 3 both (portal SourceType bitmask)."""
        self.on_ready, self.on_error = on_ready, on_error
        try:
            token = "" if forget else json.loads(self.TOKEN.read_text()).get("restore_token", "")
        except Exception: token = ""
        def created(data):
            self.session = data["session_handle"]; self.log("PORTAL_SESSION=OK")
            opts = {"types": GLib.Variant("u", types), "multiple": GLib.Variant("b", False),
                    "cursor_mode": GLib.Variant("u", 2), "persist_mode": GLib.Variant("u", 2)}
            if token: opts["restore_token"] = GLib.Variant("s", token)
            self._request("SelectSources", "(oa{sv})", (self.session,), opts, selected)
        def selected(data):
            self.log("GNOME_SELECTOR=" + ("RESTORE" if token else "OPEN"))
            self._request("Start", "(osa{sv})", (self.session, ""), {}, started)
        def started(data):
            streams = data.get("streams", [])
            if not streams: return on_error("El portal no devolvió ningún stream")
            if data.get("restore_token"): self.TOKEN.write_text(json.dumps({"restore_token": data["restore_token"]}))
            self.node = int(streams[0][0]); self.log(f"NODE={self.node}")
            self.monitor = streams[0][1].get("source_type", 1) == 1  # 1 monitor, 2 window, 4 virtual
            self.log("SOURCE=" + ("MONITOR" if self.monitor else "WINDOW"))
            proxy = Gio.DBusProxy.new_sync(self.bus, Gio.DBusProxyFlags.NONE, None, self.D, self.O, self.I, None)
            ret, fdl = proxy.call_with_unix_fd_list_sync("OpenPipeWireRemote", GLib.Variant("(oa{sv})", (self.session, {})),
                                                         Gio.DBusCallFlags.NONE, 5000, None, None)
            self.fd = fdl.get(ret.unpack()[0]); self.log("PIPEWIRE_REMOTE_FD=OK"); self.log("PORTAL=PASS")
            on_ready(self)
        try:
            self._request("CreateSession", "(a{sv})", (), {"session_handle_token": GLib.Variant("s", f"tt_session_{time.time_ns()}")}, created)
        except Exception as e:
            on_error(f"Portal no disponible: {e!r}")

    def close(self):
        if self.session:
            try: self.bus.call_sync(self.D, self.session, "org.freedesktop.portal.Session", "Close", None, None,
                                    Gio.DBusCallFlags.NONE, 2000, None)
            except Exception: pass
        if self.fd is not None:
            try: os.close(self.fd)
            except OSError: pass
        self.session = self.fd = self.node = None


# ---------------------------------------------------------------- pipeline

def even(x): return max(2, int(round(x)) // 2 * 2)


def geometry(cfg, cam_w=16, cam_h=9):
    """Compositor boxes {"screen"|"camera": (x, y, w, h)}. Boxes keep the source aspect (no stretch); full-canvas
    boxes rely on compositor sizing-policy=keep-aspect-ratio to letterbox (FIT)."""
    W, H = FORMATS[cfg["orientation"]]; lay = layout(cfg); g = {}
    if cfg["screen"]: g["screen"] = (0, 0, W, H)
    if lay == "camera": g["camera"] = (0, 0, W, H)
    elif lay == "pip":
        cw = even(W * min(max(cfg["pip_size"], 0.05), 1.0)); ch = even(cw * cam_h / cam_w)
        if ch > H: ch = even(H); cw = even(ch * cam_w / cam_h)
        m = even(min(W, H) * 0.03); fx = min(max(cfg["pip_x"], 0), 1); fy = min(max(cfg["pip_y"], 0), 1)
        g["camera"] = (m + round(max(W - cw - 2 * m, 0) * fx), m + round(max(H - ch - 2 * m, 0) * fy), cw, ch)
    return g


def pip_fractions(cfg, x, y, w, h):
    """Inverse of geometry() for the PiP box: top-left (x, y) of a w×h camera -> (pip_x, pip_y) in 0..1."""
    W, H = FORMATS[cfg["orientation"]]; m = even(min(W, H) * 0.03)
    clamp = lambda v: min(max(v, 0.0), 1.0)
    return clamp((x - m) / max(W - w - 2 * m, 1)), clamp((y - m) / max(H - h - 2 * m, 1))


def preview_sink(cfg, long_side=960):
    """App-drawn preview: small RGB frames to an appsink. gtk4paintablesink isn't packaged on Ubuntu 24.04 (the
    AppImage base), so the app paints frames itself; ~960 px / 30 fps is plenty for a preview and cheap."""
    W, H = FORMATS[cfg["orientation"]]; k = long_side / max(W, H)
    return (f"videorate drop-only=true max-rate=30 ! videoconvertscale ! video/x-raw,format=RGB,width={even(W * k)},height={even(H * k)} ! "
            "appsink name=preview emit-signals=true max-buffers=1 drop=true sync=false")


def rtmp_parts(url):
    """rtmp://host[:port]/app/stream?query -> (host, port, app, stream?query). TikTok's query (expire/sign) is part of the stream."""
    u = urlsplit(url)
    if u.scheme != "rtmp" or not u.hostname: raise ValueError("URL RTMP no válida")
    app, _, stream = u.path.lstrip("/").partition("/")
    if not app or not stream: raise ValueError("URL RTMP sin aplicación/stream")
    return u.hostname, u.port or 1935, app, stream + ("?" + u.query if u.query else "")


def describe(cfg, mode, screen=None, cam=None, preview="fakesink sync=false", out=None, record=None, desk=None):
    """gst-launch description. mode: preview | record | rtmp. screen: ScreenPortal or "test". Secrets never go in here."""
    W, H = FORMATS[cfg["orientation"]]; F, KB = QUALITY[cfg["quality"]], video_kbps(cfg); g = geometry(cfg, *(cam and (cam["w"], cam["h"]) or (16, 9)))
    pads = ""
    for name, pad, z in (("screen", "sink_0", 0), ("camera", "sink_1", 1)):
        if name in g:
            x, y, w, h = g[name]
            pads += f" {pad}::xpos={x} {pad}::ypos={y} {pad}::width={w} {pad}::height={h} {pad}::zorder={z} {pad}::sizing-policy=keep-aspect-ratio"
    p = [f"compositor name=comp background=black{pads} ! video/x-raw,format=I420,width={W},height={H},framerate={F}/1,pixel-aspect-ratio=1/1 ! tee name=vt",
         f"vt. ! queue leaky=downstream max-size-buffers=2 ! videoconvert ! {preview}"]
    if "screen" in g:
        src = ("videotestsrc name=screen_src is-live=true pattern=smpte ! video/x-raw,width=1920,height=1080" if screen == "test" else
               f"pipewiresrc name=screen_src fd={os.dup(screen.fd)} path={screen.node} do-timestamp=true")
        # FIT done here, multithreaded (add-borders keeps aspect): compositor's own scaler is single-threaded and capped
        # 3440x1440 -> 1080p60 at ~70 fps with x264; this path measured ~286 fps. Source still negotiates its native format.
        p.append(f"{src} ! queue ! videoconvertscale n-threads={THREADS} add-borders=true ! "
                 f"video/x-raw,format=I420,width={W},height={H},pixel-aspect-ratio=1/1 ! videorate ! video/x-raw,framerate={F}/1 ! "
                 f"queue name=screen_q ! comp.sink_0")
    if "camera" in g:
        dec = " ! jpegdec" if cam["jpeg"] else ""
        k = min(1.0, W / cam["w"], H / cam["h"])  # e.g. a 4K mode is reduced (multithreaded) before the compositor
        size = f",width={even(cam['w'] * k)},height={even(cam['h'] * k)},pixel-aspect-ratio=1/1" if k < 1 else ""
        p.append(f"v4l2src name=cam_src device={cfg['camera_device']} do-timestamp=true ! {cam['caps']}{dec} ! queue ! "
                 f"videoconvertscale n-threads={THREADS} ! video/x-raw,format=I420{size} ! videorate ! video/x-raw,framerate={F}/1 ! "
                 f"queue name=cam_q ! comp.sink_1")
    if mode == "preview": return " ".join(p)
    # Audio: mic + computer sound (monitor of the default output) mixed; silence keeps a valid track if both are off.
    A = "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2"
    # level: 1 reading/s of what actually leaves the mixer (the app warns/recovers on digital silence).
    p.append(f"audiomixer name=amix ! audioconvert ! level name=alevel interval=1000000000 ! audioconvert ! "
             f"avenc_aac name=aenc bitrate=160000 ! aacparse ! tee name=atee ! queue ! mux.")
    # No do-timestamp and no device clock: with arrival-time stamps two capture devices (USB mic + sound card)
    # drifted apart until the mixer dropped everything as late -> 19 min of digital silence (live 2026-10-02 16:59).
    # Audio base sources slave their own timestamps to the shared system clock instead.
    PS = "pulsesrc provide-clock=false buffer-time=200000 latency-time=10000"
    if cfg["microphone"]:
        dev = f' device="{cfg["mic_device"]}"' if cfg["mic_device"] else ""
        p.append(f"{PS} name=mic_src{dev} ! queue ! audioconvert ! audioresample ! {A} ! queue name=mic_q ! amix.")
    if cfg["desktop_audio"]:
        p.append(f'{PS} name=desk_src device="{desk or desktop_monitor()}" ! queue ! audioconvert ! audioresample ! {A} ! '
                 f"queue name=desk_q ! amix.")
    if not (cfg["microphone"] or cfg["desktop_audio"]):
        p.append(f"audiotestsrc name=mic_src is-live=true wave=silence ! {A} ! amix.")
    # No tune=zerolatency: it disables lookahead/mb-tree (visible quality loss) to save ~1 s of latency nobody needs here.
    p.append(f"vt. ! queue ! x264enc name=venc bitrate={KB} speed-preset=faster bframes=0 key-int-max={2 * F} ! "
             f"h264parse ! tee name=vtee ! queue ! mux.")
    p.append(f'matroskamux name=mux ! filesink name=out location="{out}"' if mode == "record" else "flvmux name=mux streamable=true ! rtmp2sink name=out")
    if record:  # same encoded streams, own MP4 muxer; fragmented so a cut recording stays playable up to the cut
        p.append(f'vtee. ! queue ! rmux. atee. ! queue ! rmux. mp4mux name=rmux fragment-duration=1000 ! filesink name=rec location="{record}"')
    return " ".join(p)


class Engine:
    """One running pipeline. Counts real buffers per branch so PASS/FAIL is based on data, not on intent."""
    PROBES = {"SCREEN": ("screen_q", "src"), "CAMERA": ("cam_q", "src"), "MIC": ("mic_q", "src"), "DESKTOP_AUDIO": ("desk_q", "src"), "COMPOSITOR": ("comp", "src"),
              "VIDEO_ENCODER": ("venc", "src"), "AUDIO_ENCODER": ("aenc", "src"), "OUTPUT": ("out", "sink")}

    def __init__(self, cfg, mode, log, screen=None, cam=None, preview="fakesink sync=false", out=None, rtmp_url=None,
                 on_error=None, on_eos=None, record=None):
        self.cfg, self.mode, self.log, self.on_error, self.on_eos = cfg, mode, log, on_error, on_eos
        self.counts = {}; self.bytes = 0; self.error = None; self.eos = False; self._stop_cb = None
        self.desktop_dev = desktop_monitor() if cfg["desktop_audio"] and mode != "preview" else None
        self.pipe = Gst.parse_launch(describe(cfg, mode, screen, cam, preview, out, record, self.desktop_dev))
        if self.desktop_dev: log(f"DESKTOP_AUDIO_DEVICE={self.desktop_dev}")
        self.pipe.use_clock(Gst.SystemClock.obtain())  # one clock for screen, camera and both audio devices
        self.audio_peak = None  # dB of the last level reading (-inf = digital silence)
        if rtmp_url:
            host, port, app, stream = rtmp_parts(rtmp_url)
            Log.secrets.update({rtmp_url, stream, stream.split("?")[0]})
            sink = self.pipe.get_by_name("out")
            for k, v in (("host", host), ("port", port), ("application", app), ("stream", stream)): sink.set_property(k, v)
        for key, (el, pad) in self.PROBES.items():
            e = self.pipe.get_by_name(el)
            if e: e.get_static_pad(pad).add_probe(Gst.PadProbeType.BUFFER, self._probe, key)
        bus = self.pipe.get_bus(); bus.add_signal_watch(); bus.connect("message", self._msg)

    def _probe(self, pad, info, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        if key == "OUTPUT": self.bytes += info.get_buffer().get_size()
        return Gst.PadProbeReturn.OK

    def _msg(self, bus, m):
        if m.type == Gst.MessageType.ERROR:
            e, d = m.parse_error(); src = m.src.get_name() if m.src else "?"
            self.error = f"{src}: {e.message}"
            self.log(f"GST_ERROR[{src}]={e.message}"); self.log(f"GST_DEBUG={d}")
            self.pipe.set_state(Gst.State.NULL)
            if self._stop_cb: self._finish_stop()
            elif self.on_error: self.on_error(self.error)
        elif m.type == Gst.MessageType.EOS:
            self.eos = True; self.log("EOS")
            if self._stop_cb: self._finish_stop()
            elif self.on_eos: self.on_eos()
        elif m.type == Gst.MessageType.ELEMENT and m.get_structure() and m.get_structure().get_name() == "level":
            peaks = m.get_structure().get_value("peak")
            self.audio_peak = max(peaks) if peaks else None
        elif m.type == Gst.MessageType.STATE_CHANGED and m.src == self.pipe:
            self.log("PIPELINE_STATE=" + m.parse_state_changed()[1].value_nick)

    def start(self):
        self.log(f"PIPELINE_BUILD=OK mode={self.mode}")
        if self.pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.error = self.error or "No se pudo arrancar el pipeline"; self.log("PIPELINE_START=FAIL")
            if self.on_error: GLib.idle_add(lambda: self.on_error(self.error) and False)

    def set_geometry(self, cfg, cam=None):
        """Live PiP move/resize without rebuilding."""
        comp = self.pipe.get_by_name("comp")
        for name, pad in (("screen", "sink_0"), ("camera", "sink_1")):
            box = geometry(cfg, *(cam and (cam["w"], cam["h"]) or (16, 9))).get(name); p = comp.get_static_pad(pad)
            if box and p:
                for k, v in zip(("xpos", "ypos", "width", "height"), box): p.set_property(k, v)

    def stop(self, cb, timeout=6):
        """EOS so the muxer finalizes (MKV index / RTMP unpublish), then NULL; cb() runs once."""
        self._stop_cb = cb
        if self.error or self.pipe.get_state(0)[1] != Gst.State.PLAYING: return self._finish_stop()
        self.pipe.send_event(Gst.Event.new_eos())
        GLib.timeout_add_seconds(timeout, lambda: (self._stop_cb and (self.log("EOS_TIMEOUT"), self._finish_stop()), False)[1])

    def _finish_stop(self):
        cb, self._stop_cb = self._stop_cb, None
        self.pipe.set_state(Gst.State.NULL); self.pipe.get_bus().remove_signal_watch()
        if cb: cb()
        return False


# ---------------------------------------------------------------- local A/V test (GUI + CLI)

def check_devices(cfg, cams=None):
    """((log_line, message) | None, camera). pipewire-pulse silently falls back to the default mic for unknown
    names, so the selected mic is checked here instead of trusting pulsesrc."""
    cam = None
    if layout(cfg) in ("pip", "camera"):
        cam = next((c for c in cams or list_cameras() if c["path"] == cfg["camera_device"]), None)
        if not cam: return ("CAMERA=FAIL", f"Cámara no disponible: {cfg['camera_device'] or 'ninguna seleccionada'}"), None
        cam = {**cam, **next((m for m in cam["modes"] if m["label"] == cfg["camera_mode"]), {})}  # "" = automatic
    if cfg["microphone"] and cfg["mic_device"] and cfg["mic_device"] not in [m[0] for m in list_mics()]:
        return ("MIC=FAIL", f"Micrófono no disponible: {cfg['mic_device']}"), cam
    if cfg["desktop_audio"] and not desktop_monitor():
        return ("DESKTOP_AUDIO=FAIL", "No encuentro la salida de audio predeterminada para capturar el sonido del equipo"), cam
    return None, cam


def probe(path):
    """Streams of a recorded file via GStreamer's discoverer (bundled), so tests don't need ffprobe on the host."""
    gi.require_version("GstPbutils", "1.0"); from gi.repository import GstPbutils
    try: info = GstPbutils.Discoverer.new(10 * Gst.SECOND).discover_uri(Path(path).resolve().as_uri())
    except Exception: return []
    out = []
    for st in info.get_stream_list():
        caps = st.get_caps(); name = caps.get_structure(0).get_name() if caps else ""
        codec = {"video/x-h264": "h264", "audio/mpeg": "aac"}.get(name, name)
        if isinstance(st, GstPbutils.DiscovererVideoInfo):
            out.append({"codec_type": "video", "codec_name": codec, "width": st.get_width(), "height": st.get_height(),
                        "avg_frame_rate": f"{st.get_framerate_num()}/{st.get_framerate_denom()}"})
        elif isinstance(st, GstPbutils.DiscovererAudioInfo):
            out.append({"codec_type": "audio", "codec_name": codec, "sample_rate": st.get_sample_rate(), "channels": st.get_channels()})
    return out


def run_local_test(cfg, log, on_done, screen=None, cams=None, seconds=8, preview="fakesink sync=false", rtmp_local=False):
    """Records `seconds` of the selected mix to logs/av-test.mkv (or pushes FLV to a local RTMP listener) and
    reports PASS/FAIL per stage. Requested-but-silent sources fail the test. on_done(ok, engine)."""
    lay = layout(cfg); cam = None
    text = summary(cfg, cams)
    if screen == "test": text = text.replace("Sí (portal GNOME)", "Sí (SIMULADA: videotestsrc, sin portal)")
    for line in text.splitlines(): log(line)
    log(f"CONFIG={'x'.join(map(str, FORMATS[cfg['orientation']]))} screen={cfg['screen']} camera={cfg['camera']} "
        f"mic={cfg['microphone']} layout={lay} screen_source={'videotestsrc' if screen == 'test' else 'portal'}")
    def fail(msg):
        log("ERROR=" + msg); log("AV_LOCAL_TEST=FAIL"); GLib.idle_add(lambda: on_done(False, None) and False)
    if not lay: return fail("Selecciona pantalla y/o cámara")
    if cfg["screen"] and not screen: return fail("Pantalla activada pero no hay sesión del portal")
    err, cam = check_devices(cfg, cams)
    if err: log(err[0]); return fail(err[1])
    if cam: log(f"CAMERA_CAPS={cam['caps']}")
    mkv = LOGS / "av-test.mkv"; flv = LOGS / "rtmp-test.flv"; listener = None
    for f in (mkv, flv, LOGS / "rtmp-record-test.mp4"): f.unlink(missing_ok=True)
    url = None
    if rtmp_local:  # ffmpeg acts as RTMP server; the query mimics TikTok's signed stream name
        url = "rtmp://127.0.0.1:19350/game/stream-localtest?expire=1&sign=abc"
        listener = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-listen", "1", "-timeout", "20",
                                     "-i", "rtmp://127.0.0.1:19350/game/stream-localtest", "-c", "copy", str(flv)],
                                    stdout=subprocess.DEVNULL, stderr=open(LOGS / "rtmp-listener.log", "w"), env=host_env())
        time.sleep(1)
    try:
        eng = Engine(cfg, "rtmp" if rtmp_local else "record", log, screen, cam, preview, mkv, rtmp_url=url,
                     record=LOGS / "rtmp-record-test.mp4" if rtmp_local else None)  # same tee as a recorded LIVE
    except Exception as e:
        if listener: listener.kill()
        return fail(f"PIPELINE_BUILD: {e}")
    state = {"done": False}

    def finish():
        if state["done"]: return
        state["done"] = True
        if listener:
            try: listener.wait(timeout=10)
            except subprocess.TimeoutExpired: listener.kill()
        c = eng.counts; ok = not eng.error
        def st(key, wanted=True):
            if not wanted: return "NOT_REQUESTED"
            return "PASS" if c.get(key, 0) > 0 else "FAIL"
        res = {"SCREEN": st("SCREEN", cfg["screen"]), "CAMERA": st("CAMERA", lay in ("pip", "camera")),
               "MIC": st("MIC", cfg["microphone"]), "DESKTOP_AUDIO": st("DESKTOP_AUDIO", cfg["desktop_audio"]),
               "COMPOSITOR": st("COMPOSITOR"),
               "VIDEO_ENCODER": st("VIDEO_ENCODER"), "AUDIO_ENCODER": st("AUDIO_ENCODER")}
        target = flv if rtmp_local else mkv
        streams = probe(target) if target.exists() else []
        has = {s.get("codec_name") for s in streams}
        res["OUTPUT"] = "PASS" if eng.eos and target.exists() and target.stat().st_size > 10000 and {"h264", "aac"} <= has else "FAIL"
        if rtmp_local:
            res["FLV"] = res["RTMP_LOCAL"] = res["OUTPUT"]
            rec = LOGS / "rtmp-record-test.mp4"; rs = {x.get("codec_name") for x in probe(rec)} if rec.exists() else set()
            res["LIVE_RECORDING"] = "PASS" if {"h264", "aac"} <= rs and rec.stat().st_size > 10000 else "FAIL"
        for k, v in res.items(): log(f"{k}={v}" + (f" buffers={c.get(k, 0)}" if v != "NOT_REQUESTED" and k in c else ""))
        for s in streams:
            log("STREAM=" + " ".join(f"{k}={v}" for k, v in s.items()))
        if target.exists(): log(f"BYTES={target.stat().st_size} FILE=logs/{target.name}")
        ok = ok and all(v in ("PASS", "NOT_REQUESTED") for v in res.values())
        name = "RTMP_LOCAL_TEST" if rtmp_local else "AV_LOCAL_TEST"
        log(f"{name}=" + ("PASS" if ok else "FAIL") + (" (SCREEN SIMULADA con videotestsrc)" if screen == "test" and cfg["screen"] else ""))
        on_done(ok, eng)

    def stop():
        log(f"STOPPING_AFTER_{seconds}_SECONDS"); eng.stop(finish); return False
    eng.on_error = lambda e: finish()
    eng.on_eos = finish
    eng.start()
    GLib.timeout_add_seconds(seconds, stop)
    return eng
