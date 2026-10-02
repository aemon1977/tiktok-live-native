#!/usr/bin/env python3
"""Local A/V test without TikTok. Uses config/broadcast.json unless overridden.

  ./av_test.py                         selected sources, GNOME screen picker if screen is on
  ./av_test.py --camera /dev/video2    force camera on with that device
  ./av_test.py --fake-screen           videotestsrc instead of the portal (automatic, no dialog)
  ./av_test.py --rtmp-local            push FLV to a local ffmpeg RTMP server instead of MKV
"""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # works with python3 -P too
import argparse, sys
import engine as E
from gi.repository import GLib

a = argparse.ArgumentParser()
a.add_argument("--seconds", type=int, default=8)
a.add_argument("--format", choices=list(E.FORMATS))
a.add_argument("--quality", choices=list(E.QUALITY))
a.add_argument("--camera"); a.add_argument("--no-camera", action="store_true")
a.add_argument("--camera-mode", help='p.ej. "1920×1080 · 30 fps" (ver list_cameras)')
a.add_argument("--mic"); a.add_argument("--no-mic", action="store_true")
a.add_argument("--no-screen", action="store_true"); a.add_argument("--fake-screen", action="store_true")
a.add_argument("--rtmp-local", action="store_true")
a.add_argument("--no-desktop-audio", action="store_true")
args = a.parse_args()

cfg = E.load_cfg()
if args.format: cfg["orientation"] = args.format
if args.quality: cfg["quality"] = args.quality
if args.camera: cfg["camera"], cfg["camera_device"] = True, args.camera
if args.camera_mode is not None: cfg["camera_mode"] = args.camera_mode
if args.no_camera: cfg["camera"] = False
if args.mic: cfg["microphone"], cfg["mic_device"] = True, args.mic
if args.no_mic: cfg["microphone"] = False
if args.no_screen: cfg["screen"] = False
if args.no_desktop_audio: cfg["desktop_audio"] = False

log = E.Log("rtmp-test.log" if args.rtmp_local else "av-test.log", echo=print)
log("=== TikTok LIVE Native v21 · prueba " + ("FLV/RTMP local" if args.rtmp_local else "A/V local") + " ===")
loop = GLib.MainLoop(); result = {"ok": False}
portal = E.ScreenPortal(log)

def done(ok, eng):
    result["ok"] = ok; portal.close(); loop.quit()

def go(screen):
    E.run_local_test(cfg, log, done, screen=screen, seconds=args.seconds, rtmp_local=args.rtmp_local)

if cfg["screen"] and not args.fake_screen:
    portal.start(go, lambda e: (log("PORTAL=FAIL " + e), log("AV_LOCAL_TEST=FAIL"), loop.quit()))
else:
    GLib.idle_add(lambda: go("test" if cfg["screen"] else None) and False)
GLib.timeout_add_seconds(args.seconds + 90, lambda: (log("GLOBAL_TIMEOUT"), loop.quit()) and False)
loop.run()
sys.exit(0 if result["ok"] else 2)
