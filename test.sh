#!/usr/bin/env bash
# Full local validation, never contacts TikTok. Summary: logs/test-summary.log
# Step 3 opens GNOME's screen picker (only the first time; later runs reuse the choice).
cd "$(dirname "$0")"; mkdir -p logs
S=logs/test-summary.log; : > "$S"
KEYS='(Traceback|Error:|^(SCREEN|CAMERA|MIC|DESKTOP_AUDIO|COMPOSITOR|VIDEO_ENCODER|AUDIO_ENCODER|OUTPUT|FLV|RTMP_LOCAL|LIVE_RECORDING|CAMERA_CAPS|ERROR|PORTAL|AV_LOCAL_TEST|RTMP_LOCAL_TEST)=)'
CAM=${1:-$(python3 -c 'import engine as E; c=E.load_cfg(); cs=E.list_cameras(); print(c["camera_device"] if any(x["path"]==c["camera_device"] for x in cs) else (cs[0]["path"] if cs else ""))')}
CAMARG=(); [ -n "$CAM" ] && CAMARG=(--camera "$CAM")
echo "== 1/4 selftest" | tee -a "$S"; python3 selftest.py 2>&1 | tail -3 | tee -a "$S"
echo "== 2/4 A/V con pantalla simulada · cámara ${CAM:-ninguna}" | tee -a "$S"
python3 av_test.py --fake-screen "${CAMARG[@]}" | grep -E "$KEYS" | tee -a "$S"
echo "== 3/4 FLV -> RTMP local (ffmpeg como servidor)" | tee -a "$S"
python3 av_test.py --fake-screen --rtmp-local --seconds 6 "${CAMARG[@]}" | grep -E "$KEYS" | tee -a "$S"
echo "== 4/4 REAL: pantalla (portal GNOME) + cámara + micrófono -> logs/av-test.mkv" | tee -a "$S"
python3 av_test.py "${CAMARG[@]}" | grep -E "$KEYS" | tee -a "$S"
