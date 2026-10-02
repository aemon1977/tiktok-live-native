#!/usr/bin/env python3
"""TikTok web session reuse (Firefox cookies.sqlite) and RTMP push URL retrieval.

Never logs or stores cookies, tokens or the RTMP URL. /webcast/room/create/ prepares a new room on every call,
so get_push_url() must only run after the user confirmed a REAL live.
"""
import configparser, shutil, sqlite3, tempfile, time
from pathlib import Path
import requests
from engine import Log

# Classic, XDG (newer Firefox), Ubuntu snap, Flatpak, every place a Firefox profiles.ini may live.
FIREFOX_DIRS = [Path.home() / d for d in (".mozilla/firefox", ".config/mozilla/firefox", "snap/firefox/common/.mozilla/firefox",
                                         ".var/app/org.mozilla.firefox/.mozilla/firefox", ".var/app/org.mozilla.firefox/config/mozilla/firefox")]
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0"


def _profiles():
    for ff in FIREFOX_DIRS:
        c = configparser.RawConfigParser(); c.read(ff / "profiles.ini")
        for sec in c.sections():
            if sec.startswith("Profile") and c[sec].get("Path"):
                p = Path(c[sec]["Path"]); yield ff / p if c[sec].get("IsRelative", "1") == "1" else p


def _rows(profile):
    """TikTok cookies from a private temp copy (db + WAL, so recent logins are included). Copy is deleted on return."""
    db = profile / "cookies.sqlite"
    with tempfile.TemporaryDirectory() as td:  # 0700
        for suf in ("", "-wal"):
            if Path(str(db) + suf).exists(): shutil.copy2(str(db) + suf, Path(td) / ("cookies.sqlite" + suf))
        con = sqlite3.connect(Path(td) / "cookies.sqlite")
        try: return con.execute("select host,name,value,path,isSecure,expiry from moz_cookies where host like '%tiktok.com'").fetchall()
        finally: con.close()


def _expiry(e): return e / 1000 if e > 1e11 else e  # newer Firefox stores milliseconds


def find_session():
    """(profile, rows) for the Firefox profile with the freshest valid TikTok sessionid, or (None, [])."""
    best = (0, None, [])
    for p in _profiles():
        if not (p / "cookies.sqlite").exists(): continue
        try: rows = _rows(p)
        except Exception: continue
        exp = max((_expiry(r[5]) for r in rows if r[1] == "sessionid" and r[2]), default=0)
        if exp > max(best[0], time.time()): best = (exp, p, rows)
    return best[1], best[2]


def session_status():
    """Local check only (no network): {"ok", "profile", "expires"}."""
    p, rows = find_session()
    if not p: return {"ok": False, "profile": None, "expires": None}
    exp = max(_expiry(r[5]) for r in rows if r[1] == "sessionid")
    return {"ok": True, "profile": p.name, "expires": time.strftime("%d/%m/%Y", time.localtime(exp))}


API = "https://webcast.tiktok.com/webcast/"
WEB = {"aid": "1988", "app_name": "tiktok_web", "device_platform": "web", "channel": "web"}


def _session(log):
    p, rows = find_session()
    if not p: log("TIKTOK_AUTH=FAIL no_session"); raise RuntimeError("No hay sesión de TikTok en Firefox. Inicia sesión en tiktok.com con Firefox.")
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Referer": "https://www.tiktok.com/live", "Origin": "https://www.tiktok.com"})
    for h, n, v, pa, se, _ in rows: s.cookies.set(n, v, domain=h, path=pa or "/", secure=bool(se))
    return s


def _json(log, tag, call):
    try: r = call(); d = r.json()
    except Exception as e:
        log(f"{tag}=FAIL request_error={type(e).__name__}"); return None
    log(f"{tag}_HTTP={r.status_code} status_code={d.get('status_code')}"); return d


def _csrf(log, s):
    """secsdk CSRF handshake the web client performs before protected POSTs (room/create is in its protected list)."""
    try:
        r = s.head(API + "room/create/", params=WEB, timeout=10,
                   headers={"x-secsdk-csrf-request": "1", "x-secsdk-csrf-version": "1.2.22"})
        parts = r.headers.get("x-ware-csrf-token", "").split(",")
        token = parts[1] if len(parts) > 1 else ""
    except Exception: token = ""
    if token: Log.secrets.add(token)
    log("CSRF=" + ("OBTAINED" if token else "NONE")); return token


def create_room(log, title=""):
    """Creates/prepares the TikTok room. Only after the user confirmed a real live.
    With a title: same request as TikTok LIVE Center's web Go LIVE (livecenter main.js, createLiveRoom): POST with every
    field in the QUERY string, empty form body, secsdk CSRF token. If that yields no RTMP URL, one plain GET
    (the flow validated on 2026-10-02). Returns {url, user, title, room_id, stream_id}."""
    s = _session(log)
    try:
        d = None
        if title:
            hdr = {"Content-Type": "application/x-www-form-urlencoded"}
            token = _csrf(log, s)
            if token: hdr["x-secsdk-csrf-token"] = token
            q = {**WEB, "title": title, "cover_uri": "", "screenshot_cover_status": "1"}
            d = _json(log, "CREATE_POST", lambda: s.post(API + "room/create/", params=q, headers=hdr, timeout=15))
            if not ((d or {}).get("data") or {}).get("stream_url", {}).get("rtmp_push_url"): d = None
        if d is None: d = _json(log, "CREATE_GET", lambda: s.get(API + "room/create/", params=WEB, timeout=15))
    finally: s.close()
    if d is None: raise RuntimeError("No se pudo contactar con TikTok")
    data = d.get("data") or {}
    url = (data.get("stream_url") or {}).get("rtmp_push_url", "")
    if url: Log.secrets.add(url)
    if d.get("status_code") != 0 or not url.startswith("rtmp"):
        msg = str(data.get("message") or data.get("prompts") or d.get("message") or "sin detalle")[:200]
        log("TIKTOK_AUTH=FAIL" if d.get("status_code") else "TIKTOK_AUTH=PASS"); log("RTMP=NOT_OBTAINED reason=" + msg)
        raise RuntimeError(f"TikTok no ha devuelto URL RTMP (status_code={d.get('status_code')}): {msg}")
    log("TIKTOK_AUTH=PASS"); log("RTMP=OBTAINED")
    room_id = data.get("id_str") or str(data.get("id") or "")
    owner = data.get("owner") or {}
    return {"url": url, "user": owner.get("display_id") or owner.get("unique_id") or "", "title": title,
            "room_id": room_id, "stream_id": data.get("stream_id_str") or str(data.get("stream_id") or "")}


def room_info(log, room_id):
    """Read-only room/info: title, user_count (TikTok's own live viewer counter), status."""
    if not room_id: return {}
    s = _session(log)
    try: d = _json(log, "ROOM_INFO", lambda: s.get(API + "room/info/", params={**WEB, "room_id": room_id}, timeout=15))
    finally: s.close()
    return (d or {}).get("data") or {}


def room_title(log, room_id):
    """The title TikTok stored. Neither create's response nor room/info right after create show it; it appears
    once the room is live, so ask a few seconds after streaming started."""
    return room_info(log, room_id).get("title")


def finish_room(log, room):
    """Tells TikTok the LIVE ended (anchor status 4 = finish), so the room closes now instead of after a timeout."""
    if not (room.get("room_id") and room.get("stream_id")): log("ROOM_FINISH=SKIPPED no_ids"); return False
    s = _session(log)
    try:
        d = _json(log, "ROOM_FINISH", lambda: s.post(API + "room/ping/anchor/", params=WEB, timeout=15,
                                                       data={"status": "4", "room_id": room["room_id"], "stream_id": room["stream_id"]}))
    finally: s.close()
    ok = bool(d) and d.get("status_code") == 0
    log("ROOM_FINISH=" + ("PASS" if ok else "FAIL"))
    return ok


if __name__ == "__main__":  # safe local check, never contacts /room/create/
    print(session_status())
