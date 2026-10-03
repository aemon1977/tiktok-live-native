#!/usr/bin/env python3
"""Fast checks without hardware or network. Run: python3 selftest.py"""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # works with python3 -P too
import ast
from pathlib import Path
import engine as E

for f in Path(__file__).parent.glob("*.py"): ast.parse(f.read_text(), str(f))

# caps: DMA_DRM ignored, height lists expanded, 16:9 >=24fps preferred over 4:3
s = ["video/x-raw, format=(string)DMA_DRM, width=(int)1280, height=(int)720, framerate=(fraction)30/1;",
     "video/x-raw, format=(string)YUY2, width=(int)800, height=(int)600, framerate=(fraction){ 60/1, 30/1 };",
     "video/x-raw, format=(string)YUY2, width=(int)1280, height=(int){ 1024, 960, 720 }, framerate=(fraction){ 60/1, 30/1, 10/1 };",
     "video/x-raw, format=(string)YUY2, width=(int)1920, height=(int)1080, framerate=(fraction)5/1;"]
c = E.pick_camera_caps(s)
assert c["caps"] == "video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1", c
assert E.pick_camera_caps(["image/jpeg, width=(int)640, height=(int)360, framerate=(fraction)30/1;"])["jpeg"]

# geometry: PiP keeps camera aspect, stays inside the canvas, top-right by default
cfg = {**E.DEFAULTS, "screen": True, "camera": True}
for fmt, (W, H) in E.FORMATS.items():
    for cw, ch in ((1280, 720), (640, 480)):
        for px, py in ((1, 0), (0, 1), (.5, .5)):
            x, y, w, h = E.geometry({**cfg, "orientation": fmt, "pip_x": px, "pip_y": py}, cw, ch)["camera"]
            assert abs(w / h - cw / ch) < 0.02 and 0 <= x and x + w <= W and 0 <= y and y + h <= H, (fmt, x, y, w, h)
x, y, w, h = E.geometry(cfg, 1280, 720)["camera"]
assert x + w > 650 and y < 50
assert E.geometry({**cfg, "camera": False})["screen"] == (0, 0, 720, 1280)
assert E.layout({**cfg, "screen": False}) == "camera"

# PiP drag: pip_fractions() inverts geometry() (dragging the camera to where it already is changes nothing)
for fmt in E.FORMATS:
    for fx, fy in ((0, 0), (1, 1), (.3, .7)):
        c = {**cfg, "orientation": fmt, "pip_x": fx, "pip_y": fy}; x, y, w, h = E.geometry(c, 1280, 720)["camera"]
        gx, gy = E.pip_fractions(c, x, y, w, h); assert abs(gx - fx) < .01 and abs(gy - fy) < .01, (fmt, fx, fy, gx, gy)
assert E.pip_fractions(cfg, -500, 99999, 100, 56) == (0.0, 1.0)  # dragged off-canvas clamps to the edge

# desktop audio follows the sound (live 2026-10-02: game moved to headphones -> 19 min of silence)
SK = [{"index": 1, "name": "placa"}, {"index": 2, "name": "cascos"}]
assert E.pick_output("placa", SK, []) == "placa"                                            # nothing plays: default
assert E.pick_output("placa", SK, [{"sink": 2, "corked": False}]) == "cascos"                # sound moved: follow it
assert E.pick_output("placa", SK, [{"sink": 2}, {"sink": 1}]) == "placa"                     # plays on default too
assert E.pick_output("placa", SK, [{"sink": 2, "corked": True}, {"sink": 2, "mute": True}]) == "placa"  # paused/muted

# integrated OBS: camera settings use V4L2's packing (framerate = frame interval) and the scene mirrors the app
import obs_launcher as OL
v = OL.v4l2_settings({**cfg, "camera_device": "/dev/video2"}, {"caps": "video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1",
                                                            "jpeg": False, "w": 1280, "h": 720})
assert v["resolution"] == (1280 << 16) | 720 and v["framerate"] == (1 << 16) | 30 and v["pixelformat"] == 0x56595559, v
col = OL.scene_collection({**cfg, "orientation": "horizontal"}, {"caps": "image/jpeg,width=1920,height=1080,framerate=30/1",
                                                                 "jpeg": True, "w": 1920, "h": 1080})
it = {i["name"]: i for i in col["sources"][0]["settings"]["items"]}
assert (it["Cámara"]["pos"]["x"], it["Cámara"]["bounds"]["x"]) == (E.geometry({**cfg, "orientation": "horizontal"}, 1920, 1080)["camera"][0],
                                                                   E.geometry({**cfg, "orientation": "horizontal"}, 1920, 1080)["camera"][2])

# TikTok push URL: the signed query belongs to the stream name
assert E.rtmp_parts("rtmp://push.example.com/game/stream-123?expire=1&sign=ab") == ("push.example.com", 1935, "game", "stream-123?expire=1&sign=ab")

# every GStreamer element the pipelines use is bundled in the AppImage (v1.1.0 almost shipped without mp4mux/level)
import re
from gi.repository import Gst
c = {**E.DEFAULTS, "screen": True, "camera": True, "camera_device": "/dev/video2", "microphone": True, "desktop_audio": True}
cam = {"caps": "video/x-raw,width=1280,height=720", "jpeg": True, "w": 1280, "h": 720}
d = (E.describe(c, "rtmp", "test", cam, E.preview_sink(c), record="/tmp/x.mp4", desk="x.monitor") + " " +
     E.describe(c, "record", "test", cam, out="/tmp/x.mkv", desk="x.monitor"))
bundled = set(re.search(r"ELEMENTS = \((.*?)\)\.split\(\)", (Path(__file__).parent / "packaging/assemble.py").read_text(), re.S)[1]
              .replace('"', "").split())
used = {w for w in re.findall(r"(?:^|!\s*|\s)([a-z][a-z0-9]+)(?=\s|$)", d) if Gst.ElementFactory.find(w)}
assert used <= bundled, f"faltan en packaging/assemble.py ELEMENTS: {sorted(used - bundled)}"

# secrets never reach the log file
E.Log.secrets.add("stream-SECRET?sign=x")
lg = E.Log("selftest.log"); lg("url=rtmp://h/app/stream-SECRET?sign=x")
assert "SECRET" not in lg.path.read_text(); lg.path.unlink()
print("SELFTEST_OK")
