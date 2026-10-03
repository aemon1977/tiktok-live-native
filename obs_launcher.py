#!/usr/bin/env python3
"""Integrated OBS engine: writes an OBS profile + scene collection that mirror the app (canvas, fps, bitrate, screen or
window, camera at the same place/size and mode, mic, computer sound, TikTok server/key, MP4 recording) and starts the
OBS bundled in the AppImage (or the system one when running from source), already streaming.

OBS gets its own config dir (DATA/obs), never the user's ~/.config/obs-studio. The stream key exists on disk only while
OBS runs: scrub() blanks it afterwards.
"""
import configparser, json, os, shutil, signal, subprocess, uuid
from pathlib import Path
import engine as E

NAME = "TikTokLIVE"
XDG = E.DATA / "obs"                      # XDG_CONFIG_HOME for OBS -> XDG/obs-studio/...
CONF = XDG / "obs-studio"
FOURCC = {"YUY2": 0x56595559, "NV12": 0x3231564E, "MJPG": 0x47504A4D}


def obs_binary():
    """Bundled OBS inside the AppImage, else a system `obs`."""
    appdir = os.environ.get("TTLN_APPDIR")
    if appdir and (Path(appdir) / "usr/bin/obs").exists(): return str(Path(appdir) / "usr/bin/obs")
    return shutil.which("obs")


def v4l2_settings(cfg, cam):
    """OBS linux-v4l2 packs (a << 16) | b: resolution = width, height; framerate = the V4L2 *frame interval*
    numerator, denominator (1/30 s for 30 fps — packing 30/1 meant one frame every 30 s: black camera)."""
    import re
    # auto_reset + OBS's default 5-frame select timeout (166 ms) kept resetting slow-starting cameras (Elgato Facecam 4K)
    # before their first frame: 0 frames. Give 2 s and don't reset.
    s = {"device_id": cfg["camera_device"], "input": 0, "auto_reset": False, "timeout_frames": 60, "buffering": False}
    fr = re.search(r"framerate=(\d+)/(\d+)", cam["caps"]); fmt = re.search(r"format=(\w+)", cam["caps"])
    s["resolution"] = (cam["w"] << 16) | cam["h"]
    if fr: s["framerate"] = (int(fr[2]) << 16) | int(fr[1])  # caps fps n/d -> interval d/n
    code = "MJPG" if cam["jpeg"] else (fmt[1] if fmt else "YUY2")
    if code in FOURCC: s["pixelformat"] = FOURCC[code]
    return s


def _source(sid, name, settings):
    return {"id": sid, "versioned_id": sid, "name": name, "uuid": str(uuid.uuid4()), "settings": settings,
            "enabled": True, "muted": False, "volume": 1.0, "mixers": 255, "flags": 0}


def _item(src, item_id, box, W, H):
    """Scene item fitted (scale-inner = keep aspect, no stretch) into box (x, y, w, h)."""
    x, y, w, h = box
    return {"name": src["name"], "source_uuid": src["uuid"], "id": item_id, "visible": True, "locked": False,
            "pos": {"x": float(x), "y": float(y)}, "rot": 0.0, "scale": {"x": 1.0, "y": 1.0}, "align": 5,
            "bounds_type": 2, "bounds_align": 0, "bounds": {"x": float(w), "y": float(h)}, "crop_left": 0, "crop_top": 0,
            "crop_right": 0, "crop_bottom": 0, "scale_filter": "lanczos", "blend_type": "normal"}


def scene_collection(cfg, cam=None, screen_monitor=True, restore_token="", desktop_dev=None):
    W, H = E.FORMATS[cfg["orientation"]]
    g = E.geometry(cfg, *(cam and (cam["w"], cam["h"]) or (16, 9)))
    sources, items = [], []
    if "screen" in g:
        sid = "pipewire-screen-capture-source" if screen_monitor else "pipewire-window-capture-source"
        src = _source(sid, "Pantalla" if screen_monitor else "Ventana", {"RestoreToken": restore_token, "ShowCursor": True})
        sources.append(src); items.append(_item(src, len(items) + 1, g["screen"], W, H))
    if "camera" in g and cam:
        src = _source("v4l2_input", "Cámara", v4l2_settings(cfg, cam))
        sources.append(src); items.append(_item(src, len(items) + 1, g["camera"], W, H))
    scene = _source("scene", "LIVE", {"items": items, "id_counter": len(items), "custom_size": False})
    col = {"name": NAME, "current_scene": "LIVE", "current_program_scene": "LIVE", "scene_order": [{"name": "LIVE"}],
           "sources": [scene, *sources], "groups": [], "transitions": [], "current_transition": "Fade", "transition_duration": 300,
           "quick_transitions": [], "saved_projectors": [], "modules": {}}
    if cfg["desktop_audio"]:
        col["DesktopAudioDevice1"] = _source("pulse_output_capture", "Audio del equipo", {"device_id": desktop_dev or "default"})
    if cfg["microphone"]:
        col["AuxAudioDevice1"] = _source("pulse_input_capture", "Micrófono", {"device_id": cfg["mic_device"] or "default"})
    return col


def _ini(path, sections):
    c = configparser.RawConfigParser(); c.optionxform = str  # OBS keys are case-sensitive
    if path.exists(): c.read(path, encoding="utf-8")
    for sec, kv in sections.items():
        if not c.has_section(sec): c.add_section(sec)
        for k, v in kv.items(): c.set(sec, k, str(v))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f: c.write(f, space_around_delimiters=False)


def write_config(cfg, room_url, cam=None, screen_monitor=True, restore_token="", desktop_dev=None, record_dir=None):
    """Profile, scene collection and service for this LIVE. Returns the OBS command line."""
    W, H = E.FORMATS[cfg["orientation"]]; fps = E.QUALITY[cfg["quality"]]
    host, port, app, key = E.rtmp_parts(room_url)
    prof = CONF / "basic/profiles" / NAME
    _ini(prof / "basic.ini", {
        "General": {"Name": NAME},
        "Video": {"BaseCX": W, "BaseCY": H, "OutputCX": W, "OutputCY": H, "FPSType": 1, "FPSInt": fps, "ScaleType": "lanczos"},
        "Audio": {"SampleRate": 48000, "ChannelSetup": "Stereo"},
        "Output": {"Mode": "Simple"},
        "SimpleOutput": {"VBitrate": E.video_kbps(cfg), "ABitrate": 160, "StreamEncoder": "x264", "Preset": "veryfast",
                         "RecQuality": "Stream", "RecFormat2": "mp4", "FilePath": str(record_dir or E.recordings_dir())}})
    (prof / "service.json").write_text(json.dumps({"type": "rtmp_custom", "settings": {
        "server": f"rtmp://{host}{'' if port == 1935 else f':{port}'}/{app}/", "key": key, "use_auth": False, "bwtest": False}}))
    os.chmod(prof / "service.json", 0o600)
    scenes = CONF / "basic/scenes"; scenes.mkdir(parents=True, exist_ok=True)
    (scenes / f"{NAME}.json").write_text(json.dumps(scene_collection(cfg, cam, screen_monitor, restore_token, desktop_dev), indent=1))
    app_ini = {"General": {"FirstRun": "true", "LastVersion": 99 << 24, "EnableAutoUpdates": "false", "ConfirmOnExit": "false"},
               "BasicWindow": {"RecordWhenStreaming": str(bool(cfg["record_live"])).lower(), "WarnBeforeStartingStream": "false",
                               "WarnBeforeStoppingStream": "false", "WarnBeforeStoppingRecord": "false"},
               "Basic": {"Profile": NAME, "ProfileDir": NAME, "SceneCollection": NAME, "SceneCollectionFile": NAME}}
    for f in ("global.ini", "user.ini"): _ini(CONF / f, app_ini)  # OBS 31+ split settings into user.ini
    return [obs_binary(), "--multi", "--profile", NAME, "--collection", NAME, "--startstreaming"]


def launch(cmd, log):
    env = {**os.environ, "XDG_CONFIG_HOME": str(XDG)}
    env.pop("GDK_BACKEND", None)
    log("OBS_LAUNCH=" + " ".join(c for c in cmd[1:]))
    return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=open(E.LOGS / "obs-stderr.log", "w"),
                            start_new_session=True)


def stop(proc, timeout=15):
    """Ask OBS to quit (it stops the stream and finalizes the recording), kill it if it hangs."""
    if proc.poll() is not None: return
    proc.send_signal(signal.SIGINT)
    try: proc.wait(timeout)
    except subprocess.TimeoutExpired: proc.kill()


def streaming_started(since):
    """True once OBS's current log says the stream output started (log files are named by start time)."""
    logs = [l for l in (CONF / "logs").glob("*.txt") if l.stat().st_mtime >= since - 2]
    return any("==== Streaming Start" in l.read_text(errors="replace") for l in logs)


def scrub():
    """After OBS exits: blank the stream key; return OBS's new portal restore token (single-use tokens rotate)."""
    svc = CONF / "basic/profiles" / NAME / "service.json"
    if svc.exists():
        d = json.loads(svc.read_text()); d.get("settings", {})["key"] = ""; svc.write_text(json.dumps(d))
    try:
        col = json.loads((CONF / "basic/scenes" / f"{NAME}.json").read_text())
        return next((s["settings"].get("RestoreToken") for s in col["sources"] if s["id"].startswith("pipewire-")), None)
    except Exception: return None


def selftest(seconds=12):
    """`AppImage --obs-test`: bundled OBS loads a generated profile/collection and streams to a local RTMP server
    (ffmpeg), with a TikTok-style signed key. No screen/camera needed (colour source + 440 Hz desktop-free test)."""
    import time
    log = E.Log("obs-test.log", echo=print); log(f"=== OBS integrado · prueba local ({obs_binary()}) ===")
    if not obs_binary(): log("OBS=FAIL no encontrado"); return False
    cfg = {**E.DEFAULTS, "screen": False, "camera": False, "microphone": False, "desktop_audio": False, "record_live": False}
    out = E.LOGS / "obs-test.flv"; out.unlink(missing_ok=True)
    cmd = write_config(cfg, "rtmp://127.0.0.1:19350/game/stream-obstest?expire=1&sign=abc")
    col = CONF / "basic/scenes" / f"{NAME}.json"; c = json.loads(col.read_text())
    color = _source("color_source_v3", "Color", {"color": 0xFF2C55FE, "width": 720, "height": 1280})
    tone = _source("ffmpeg_source", "Tono", {"is_local_file": False, "input": "anullsrc", "input_format": "lavfi"})
    c["sources"][0]["settings"]["items"] = [_item(color, 1, (0, 0, 720, 1280), 720, 1280)]
    c["sources"] += [color, tone]; col.write_text(json.dumps(c))
    srv = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-listen", "1", "-timeout", "30", "-i",
                            "rtmp://127.0.0.1:19350/game/stream-obstest", "-c", "copy", str(out)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=E.host_env())
    time.sleep(1); proc = launch(cmd, log); time.sleep(seconds); stop(proc)
    try: srv.wait(10)
    except subprocess.TimeoutExpired: srv.kill()
    streams = E.probe(out) if out.exists() else []
    ok = any(s["codec_name"] == "h264" for s in streams)
    log("OBS_STREAM=" + ("PASS" if ok else "FAIL") + f" streams={[s['codec_name'] for s in streams]} bytes={out.stat().st_size if out.exists() else 0}")
    logs = sorted((CONF / "logs").glob("*.txt"))
    if logs:
        t = logs[-1].read_text(errors="replace")
        for mod in ("linux-pipewire", "linux-v4l2", "linux-pulseaudio", "obs-x264", "obs-outputs"):
            log(f"OBS_MODULE[{mod}]=" + ("LOADED" if f"{mod}.so" in t or f"[{mod}]" in t or mod in t else "MISSING"))
        log("OBS_SCENE=" + ("LOADED" if NAME in t else "?"))
    scrub(); log("OBS_KEY_SCRUBBED=" + ("PASS" if json.loads((CONF / f'basic/profiles/{NAME}/service.json').read_text())["settings"]["key"] == "" else "FAIL"))
    return ok


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--selftest"]: sys.exit(0 if selftest() else 1)
