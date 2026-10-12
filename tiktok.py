#!/usr/bin/env python3
"""TikTok web session reuse (cookies of any installed Firefox- or Chromium-based browser) and RTMP push URL retrieval.

Never logs or stores cookies, tokens or the RTMP URL. /webcast/room/create/ prepares a new room on every call,
so get_push_url() must only run after the user confirmed a REAL live.
"""
import configparser, hashlib, shutil, sqlite3, subprocess, tempfile, time
from pathlib import Path
import requests
from engine import Log

H = Path.home()
# Firefox family: (name, dirs holding profiles.ini) — classic, XDG, snap, Flatpak.
FIREFOX = [("Firefox", [".mozilla/firefox", ".config/mozilla/firefox", "snap/firefox/common/.mozilla/firefox",
                        ".var/app/org.mozilla.firefox/.mozilla/firefox", ".var/app/org.mozilla.firefox/config/mozilla/firefox"]),
           ("LibreWolf", [".librewolf", ".var/app/io.gitlab.librewolf-community/.librewolf"]),
           ("Waterfox", [".waterfox"]), ("Floorp", [".floorp", ".var/app/one.ablaze.floorp/.floorp"]),
           ("Zen", [".zen", ".var/app/app.zen_browser.zen/.zen"])]
# Chromium family: (name, keyring name, user-data dirs) — native, snap, Flatpak.
CHROMIUM = [("Chrome", "Chrome", [".config/google-chrome", ".var/app/com.google.Chrome/config/google-chrome"]),
            ("Chromium", "Chromium", [".config/chromium", "snap/chromium/common/chromium", ".var/app/org.chromium.Chromium/config/chromium"]),
            ("Brave", "Brave", [".config/BraveSoftware/Brave-Browser", ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"]),
            ("Edge", "Microsoft Edge", [".config/microsoft-edge", ".var/app/com.microsoft.Edge/config/microsoft-edge"]),
            ("Vivaldi", "Chrome", [".config/vivaldi", ".var/app/com.vivaldi.Vivaldi/config/vivaldi"]),
            ("Opera", "Chromium", [".config/opera", ".var/app/com.opera.Opera/config/opera"])]
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0"


def _copy_query(db, sql):
    """Query a private temp copy (db + WAL, so recent logins are included). Copy is deleted on return."""
    with tempfile.TemporaryDirectory() as td:  # 0700
        for suf in ("", "-wal"):
            if Path(str(db) + suf).exists(): shutil.copy2(str(db) + suf, Path(td) / ("c.db" + suf))
        con = sqlite3.connect(Path(td) / "c.db")
        try: return [con.execute(q).fetchall() for q in sql]
        finally: con.close()


def _firefox_rows(db):
    rows = _copy_query(db, ["select host,name,value,path,isSecure,expiry from moz_cookies where host like '%tiktok.com'"])[0]
    return [(h, n, v, pa, se, e / 1000 if e > 1e11 else e) for h, n, v, pa, se, e in rows]  # newer Firefox: milliseconds


_keys = {}


def _keyring_password(name):
    """'<name> Safe Storage' from the Secret Service (GNOME Keyring, KeePassXC, KWallet 6) or KWallet (kwallet-query)."""
    if name in _keys: return _keys[name]
    pw = None
    try:
        import gi; gi.require_version("Secret", "1")
        from gi.repository import Secret
        svc = Secret.Service.get_sync(Secret.ServiceFlags.LOAD_COLLECTIONS, None)
        for col in svc.get_collections() or []:
            if col.get_locked(): svc.unlock_sync([col], None)
            col.load_items_sync(None)
            for it in col.get_items() or []:
                if it.get_label() == f"{name} Safe Storage":
                    it.load_secret_sync(None); pw = it.get_secret().get_text(); break
            if pw: break
    except Exception: pass
    if not pw and shutil.which("kwallet-query"):
        try:
            r = subprocess.run(["kwallet-query", "-r", f"{name} Safe Storage", "-f", f"{name} Keys", "kdewallet"],
                               capture_output=True, text=True, timeout=10)
            pw = r.stdout.strip() if r.returncode == 0 and "Failed" not in r.stdout else None
        except Exception: pass
    _keys[name] = pw
    return pw


def _chromium_decrypt(enc, name, strip_hash):
    """v10 = fixed 'peanuts' key (no keyring), v11 = key from the keyring. AES-128-CBC, IV of spaces."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    pw = b"peanuts" if enc[:3] == b"v10" else (_keyring_password(name) or "").encode()
    if not pw: return None
    key = hashlib.pbkdf2_hmac("sha1", pw, b"saltysalt", 1, 16)
    d = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    out = d.update(enc[3:]) + d.finalize()
    if not 1 <= out[-1] <= 16: return None
    out = out[:-out[-1]]
    return (out[32:] if strip_hash else out).decode()  # cookie DB version >= 24 prefixes SHA256(host)


def _chromium_rows(db, name):
    rows, meta = _copy_query(db, ["select host_key,name,value,encrypted_value,path,is_secure,expires_utc from cookies "
                                  "where host_key like '%tiktok.com'", "select value from meta where key='version'"])
    strip = int(meta[0][0]) >= 24 if meta else False
    out = []
    for h, n, v, enc, pa, se, e in rows:
        if not v and enc:
            try: v = _chromium_decrypt(enc, name, strip) if enc[:3] in (b"v10", b"v11") else None
            except Exception: v = None
        if v: out.append((h, n, v, pa, se, e / 1e6 - 11644473600 if e else 0))  # microseconds since 1601
    return out


def _profiles():
    """(label, loader) for every browser profile that has a cookie database."""
    for name, dirs in FIREFOX:
        for d in dirs:
            ff = H / d; c = configparser.RawConfigParser(); c.read(ff / "profiles.ini")
            for sec in c.sections():
                if sec.startswith("Profile") and c[sec].get("Path"):
                    p = Path(c[sec]["Path"]); p = ff / p if c[sec].get("IsRelative", "1") == "1" else p
                    if (p / "cookies.sqlite").exists(): yield f"{name} ({p.name})", lambda db=p / "cookies.sqlite": _firefox_rows(db)
    for name, kr, dirs in CHROMIUM:
        for d in dirs:
            for p in sorted((H / d).glob("*")):
                for db in (p / "Network/Cookies", p / "Cookies"):
                    if db.is_file(): yield f"{name} ({p.name})", lambda db=db, kr=kr: _chromium_rows(db, kr); break


def find_session():
    """(browser label, rows) for the profile with the freshest valid TikTok sessionid, or (None, [])."""
    best = (0, None, [])
    for label, load in _profiles():
        try: rows = load()
        except Exception: continue
        exp = max((r[5] for r in rows if r[1] == "sessionid" and r[2]), default=0)
        if exp > max(best[0], time.time()): best = (exp, label, rows)
    return best[1], best[2]


def session_status():
    """Local check only (no network): {"ok", "browser", "expires"}."""
    label, rows = find_session()
    if not label: return {"ok": False, "browser": None, "expires": None}
    exp = max(r[5] for r in rows if r[1] == "sessionid")
    return {"ok": True, "browser": label, "expires": time.strftime("%d/%m/%Y", time.localtime(exp))}


API = "https://webcast.tiktok.com/webcast/"
WEB = {"aid": "1988", "app_name": "tiktok_web", "device_platform": "web", "channel": "web"}


def _session(log):
    p, rows = find_session()
    if not p: log("TIKTOK_AUTH=FAIL no_session"); raise RuntimeError("No hay sesión de TikTok en ningún navegador. Inicia sesión en tiktok.com con tu navegador.")
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
