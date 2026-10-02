#!/usr/bin/env python3
"""TikTok LIVE Native — GTK4 GUI. Preview, local A/V test and real TikTok LIVE share one GStreamer engine."""
import os, sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # works with python3 -P too
import json, subprocess, threading, time
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Gdk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk
from chat_overlay import chat_view
import engine as E
from gi.repository import Gst  # initialised by engine
import tiktok

MAX_RETRIES = 5


def esc(s): return GLib.markup_escape_text(str(s))


class ChatPanel(Gtk.Box):
    """Viewers + chat of the running LIVE, docked in the app, plus an optional translucent always-on-top overlay
    (chat_overlay.py, separate XWayland process) to read it over the window being streamed."""
    def __init__(self, log, cfg):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=360, visible=False)
        self.log = log; self.cfg = cfg; self.view = None; self.user = None; self.proc = None
        head = Gtk.Box(spacing=6); self.append(head)
        t = Gtk.Label(label="Espectadores y chat", xalign=0, hexpand=True); t.add_css_class("heading"); head.append(t)
        self.b_overlay = Gtk.ToggleButton(label="Superponer", active=cfg["chat_overlay"],
                                          tooltip_text="Mostrar u ocultar el chat semitransparente siempre encima (se recuerda)")
        self.b_overlay.connect("toggled", self.on_toggle); head.append(self.b_overlay)

    def on_toggle(self, b):
        self.cfg["chat_overlay"] = b.get_active(); E.save_cfg(self.cfg)
        self.log("CHAT_OVERLAY=" + ("SHOWN" if b.get_active() else "HIDDEN")); self.sync_overlay()

    def load(self, user):
        if self.view: self.remove(self.view)
        self.user = user; self.view = chat_view(user, self.log); self.append(self.view); self.set_visible(True)
        self.stop_overlay(); self.sync_overlay()

    def sync_overlay(self):
        alive = self.proc and self.proc.poll() is None
        if self.b_overlay.get_active() and self.user and not alive:
            self.proc = subprocess.Popen([sys.executable, str(E.HERE / "chat_overlay.py"), self.user], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env={**os.environ, "GDK_BACKEND": "x11"})
            self.log("CHAT_OVERLAY=STARTED"); proc = self.proc
            def closed_by_user():  # its ✕ = "hide": reflect it on the toggle (and remember it)
                if self.proc is not proc: return False
                if proc.poll() is None: return True
                self.proc = None; self.b_overlay.set_active(False); return False
            GLib.timeout_add_seconds(1, closed_by_user)
        elif not self.b_overlay.get_active(): self.stop_overlay()

    def stop_overlay(self):
        proc, self.proc = self.proc, None  # cleared first: an app-initiated stop is not a user "hide"
        if proc and proc.poll() is None: proc.terminate(); self.log("CHAT_OVERLAY=STOPPED")


class Win:
    def __init__(self, app):
        self.cfg = E.load_cfg(); self.cams = E.list_cameras(); self.mics = E.list_mics()
        self.portal = None; self.portal_ready = False; self.eng = None; self.mode = "idle"
        self.live_url = None; self.room = None; self.retries = 0; self._rebuild = 0; self.cam_now = None
        self.win = Adw.ApplicationWindow(application=app, title="TikTok LIVE Native", default_width=1180, default_height=820)
        self.win.connect("close-request", self.on_close)
        self.log = E.Log("app.log", fresh=False, echo=self.append_log)
        self.log("=== TikTok LIVE Native v21 · inicio ===")
        self.build()
        self.refresh_session(); self.sync_ui()
        if self.cfg["screen"] and E.ScreenPortal.TOKEN.exists(): self.pick_screen(forget=False)  # restores last choice, no dialog
        else: self.schedule_rebuild()
        self.win.present()

    # ------------------------------------------------------------ UI
    def build(self):
        tv = Adw.ToolbarView(); tv.add_top_bar(Adw.HeaderBar()); self.win.set_content(tv)
        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, margin_top=6, margin_bottom=12, margin_start=12, margin_end=12)
        tv.set_content(root)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, width_request=430)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=left); root.append(sc)
        self.settings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        g = Adw.PreferencesGroup(title="TikTok"); left.append(g)
        self.row_session = Adw.ActionRow(title="Sesión"); g.add(self.row_session)
        b = Gtk.Button(label="Comprobar", valign=Gtk.Align.CENTER); b.connect("clicked", lambda *_: self.refresh_session())
        self.row_session.add_suffix(b)
        self.e_title = Adw.EntryRow(title="Título del LIVE", text=self.cfg["title"]); g.add(self.e_title)
        self.e_title.connect("changed", lambda e: (self.cfg.__setitem__("title", e.get_text().strip()), E.save_cfg(self.cfg)))
        sw = Adw.SwitchRow(title="Grabar el LIVE", subtitle=f"Copia exacta de lo emitido en {E.recordings_dir()}", active=self.cfg["record_live"])
        sw.connect("notify::active", lambda r, _: (self.cfg.__setitem__("record_live", r.get_active()), E.save_cfg(self.cfg))); g.add(sw)
        b = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Abrir carpeta de grabaciones", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda *_: subprocess.Popen(["xdg-open", str(E.recordings_dir())], env=E.host_env())); sw.add_suffix(b)
        g.add(Adw.ActionRow(title="LIVE", subtitle="Se confirma al pulsar Iniciar LIVE: TikTok crea la sala en ese momento, no antes."))
        left.append(self.settings)

        g = Adw.PreferencesGroup(title="Vídeo"); self.settings.append(g)
        self.c_format = self.combo("Formato", [E.FORMAT_NAMES[k] for k in E.FORMATS], list(E.FORMATS).index(self.cfg["orientation"]),
                                   lambda i: self.set("orientation", list(E.FORMATS)[i])); g.add(self.c_format)
        self.s_screen = self.switch("Pantalla / ventana", "screen"); g.add(self.s_screen)
        self.row_pick = Adw.ActionRow(title="Captura de pantalla", subtitle="No seleccionada"); g.add(self.row_pick)
        b = Gtk.Button(label="Seleccionar…", valign=Gtk.Align.CENTER); b.connect("clicked", lambda *_: self.pick_screen(forget=True))
        self.row_pick.add_suffix(b)
        self.s_camera = self.switch("Cámara", "camera"); g.add(self.s_camera)
        names = [f"{c['name']} ({c['path']})" for c in self.cams] or ["No detectada"]
        idx = next((i for i, c in enumerate(self.cams) if c["path"] == self.cfg["camera_device"]), 0)
        if self.cams and not self.cfg["camera_device"]: self.cfg["camera_device"] = self.cams[0]["path"]
        self.c_cam = self.combo("Dispositivo de cámara", names, idx, lambda i: self.cams and self.set_camera(self.cams[i]["path"]))
        g.add(self.c_cam)
        self.c_mode = Adw.ComboRow(title="Resolución de cámara"); g.add(self.c_mode)
        self.c_mode.connect("notify::selected", self.on_mode); self.fill_modes()
        if not self.cams: self.cfg["camera"] = False; self.s_camera.set_sensitive(False)
        self.row_layout = Adw.ActionRow(title="Layout"); g.add(self.row_layout)

        g = Adw.PreferencesGroup(title="Audio"); self.settings.append(g)
        self.s_mic = self.switch("Micrófono", "microphone"); g.add(self.s_mic)
        r = self.switch("Audio del equipo", "desktop_audio"); r.set_subtitle("Lo que suena en tu PC (juego, navegador…), mezclado con el micrófono")
        g.add(r)
        mnames = ["Predeterminado del sistema"] + [d for _, d in self.mics]
        midx = next((i + 1 for i, m in enumerate(self.mics) if m[0] == self.cfg["mic_device"]), 0)
        g.add(self.combo("Dispositivo de micrófono", mnames, midx, lambda i: self.set("mic_device", self.mics[i - 1][0] if i else "")))

        g = Adw.PreferencesGroup(title="Calidad"); self.settings.append(g)
        g.add(self.combo("FPS / bitrate", [E.QUALITY_NAMES[k] for k in E.QUALITY], list(E.QUALITY).index(self.cfg["quality"]),
                         lambda i: self.set("quality", list(E.QUALITY)[i])))

        # PiP stays editable during LIVE: it moves compositor pads without rebuilding the pipeline.
        self.g_pip = Adw.PreferencesGroup(title="Cámara PiP",
                                          description="Arrástrala con el ratón en la vista previa; rueda = tamaño. Se aplica en directo.")
        left.append(self.g_pip); self.pip_rows = {}; self._syncing = False; self._pip_save = 0
        corners = Gtk.Box(spacing=6, homogeneous=True, margin_bottom=6)
        for label, fx, fy in (("↖ Arriba izq.", 0, 0), ("↗ Arriba der.", 1, 0), ("↙ Abajo izq.", 0, 1), ("↘ Abajo der.", 1, 1)):
            b = Gtk.Button(label=label); b.connect("clicked", lambda _b, fx=fx, fy=fy: self.set_pip(pip_x=fx, pip_y=fy)); corners.append(b)
        self.g_pip.add(corners)
        for title, key in (("Tamaño (% del ancho)", "pip_size"), ("Posición X (0 izq. – 100 der.)", "pip_x"), ("Posición Y (0 arriba – 100 abajo)", "pip_y")):
            r = Adw.SpinRow.new_with_range(5 if key == "pip_size" else 0, 100, 1); r.set_title(title); self.pip_rows[key] = r
            r.set_value(round(self.cfg[key] * 100)); r.connect("notify::value", self.on_pip, key); self.g_pip.add(r)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True); root.append(right)
        self.chat = ChatPanel(self.log, self.cfg); root.append(self.chat)
        self.sources = Gtk.Label(xalign=0, wrap=True); right.append(self.sources)
        ov = Gtk.Overlay(vexpand=True)
        self.aspect = Gtk.AspectFrame(obey_child=False, vexpand=True, hexpand=True)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN); self.picture.add_css_class("view")
        drag = Gtk.GestureDrag(); drag.connect("drag-begin", self.drag_begin); drag.connect("drag-update", self.drag_update)
        self.picture.add_controller(drag); self._drag = None
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", lambda c, dx, dy: E.layout(self.cfg) == "pip" and
                       self.set_pip(pip_size=min(max(self.cfg["pip_size"] - dy * 0.02, 0.05), 1.0)))
        self.picture.add_controller(scroll)
        self.aspect.set_child(self.picture); ov.set_child(self.aspect)
        self.overlay = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.overlay.add_css_class("title-3"); ov.add_overlay(self.overlay); right.append(ov)

        btns = Gtk.Box(spacing=8, homogeneous=True); right.append(btns)
        self.b_test = Gtk.Button(label="PROBAR LOCALMENTE (8 s)"); self.b_test.connect("clicked", self.on_test)
        self.b_live = Gtk.Button(label="INICIAR LIVE"); self.b_live.add_css_class("suggested-action"); self.b_live.connect("clicked", self.on_live)
        self.b_stop = Gtk.Button(label="DETENER LIVE"); self.b_stop.add_css_class("destructive-action"); self.b_stop.connect("clicked", self.on_stop)
        for b in (self.b_test, self.b_live, self.b_stop): b.add_css_class("pill"); btns.append(b)
        self.status = Gtk.Label(xalign=0, wrap=True); self.status.add_css_class("heading"); right.append(self.status)
        self.logview = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, wrap_mode=Gtk.WrapMode.CHAR)
        right.append(Gtk.ScrolledWindow(child=self.logview, min_content_height=140))

    def cam_modes(self):
        cam = next((c for c in self.cams if c["path"] == self.cfg["camera_device"]), None)
        return sorted(cam["modes"], key=lambda m: (m["w"] * m["h"], m["fps"]), reverse=True) if cam else []

    def fill_modes(self):
        """Modes of the selected camera; index 0 = automatic (validated pick)."""
        modes = self.cam_modes(); self._filling = True
        cam = next((c for c in self.cams if c["path"] == self.cfg["camera_device"]), None)
        self.c_mode.set_model(Gtk.StringList.new([f"Automática ({cam['label']})" if cam else "Automática"] + [m["label"] for m in modes]))
        self.c_mode.set_selected(next((i + 1 for i, m in enumerate(modes) if m["label"] == self.cfg["camera_mode"]), 0))
        self._filling = False

    def on_mode(self, row, _):
        if getattr(self, "_filling", False): return
        i = row.get_selected(); modes = self.cam_modes()
        self.set("camera_mode", modes[i - 1]["label"] if 0 < i <= len(modes) else "")

    def set_camera(self, path):
        if path == self.cfg["camera_device"]: return
        self.cfg["camera_mode"] = ""; self.set("camera_device", path); self.fill_modes()

    def combo(self, title, items, idx, cb):
        r = Adw.ComboRow(title=title, model=Gtk.StringList.new(items)); r.set_selected(idx)
        r.connect("notify::selected", lambda r, _: cb(r.get_selected())); return r

    def switch(self, title, key):
        r = Adw.SwitchRow(title=title, active=self.cfg[key]); r.connect("notify::active", lambda r, _: self.set(key, r.get_active())); return r

    def append_log(self, s):  # may be called from the TikTok request thread
        def add():
            buf = self.logview.get_buffer(); buf.insert(buf.get_end_iter(), s + "\n")
            self.logview.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
        GLib.idle_add(add)

    def set(self, key, val):
        if self.cfg[key] == val: return
        self.cfg[key] = val; E.save_cfg(self.cfg); self.sync_ui()
        if key == "screen" and val and not self.portal_ready: self.pick_screen(forget=False)
        else: self.schedule_rebuild()

    def sync_ui(self):
        c = self.cfg; lay = E.layout(c); W, H = E.FORMATS[c["orientation"]]
        self.aspect.set_ratio(W / H)
        self.row_layout.set_subtitle(E.LAYOUT_NAMES[lay])
        self.g_pip.set_visible(lay == "pip"); self.c_cam.set_sensitive(c["camera"]); self.c_mode.set_sensitive(c["camera"]); self.row_pick.set_sensitive(c["screen"])
        on = lambda b, m="ACTIVADA", f="DESACTIVADA": f"<b>{m}</b>" if b else f"<span alpha='60%'>{f}</span>"
        self.sources.set_markup(
            f"Pantalla: {on(c['screen'])}   ·   Cámara: {on(c['camera'])}{esc(' ' + c['camera_device']) if c['camera'] else ''}"
            f"   ·   Micrófono: {on(c['microphone'], 'ACTIVADO', 'DESACTIVADO')}   ·   Audio equipo: {on(c['desktop_audio'], 'ACTIVADO', 'DESACTIVADO')}   ·   {W}×{H} @ {E.QUALITY[c['quality']]} fps · {E.video_kbps(c)} kbps")
        busy = self.mode in ("test", "connecting", "live", "stopping")
        self.settings.set_sensitive(not busy)
        self.b_test.set_sensitive(not busy); self.b_live.set_sensitive(not busy)
        self.b_stop.set_sensitive(self.mode in ("connecting", "live"))

    def set_mode(self, mode, status=None):
        self.mode = mode
        if status is not None: self.status.set_markup(status)
        self.sync_ui()

    def ask(self, heading, body, ok, cb, danger=False):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("cancel", "CANCELAR"); d.add_response("ok", ok)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if danger else Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("cancel"); d.set_close_response("cancel")
        d.connect("response", lambda d, r: r == "ok" and cb()); d.present(self.win)

    def info(self, heading, body):
        d = Adw.AlertDialog(heading=heading, body=body); d.add_response("ok", "Aceptar"); d.present(self.win)

    # ------------------------------------------------------------ session / sources
    def refresh_session(self):
        s = tiktok.session_status()
        self.log("TIKTOK_SESSION=" + ("FOUND" if s["ok"] else "NOT_FOUND"))
        self.row_session.set_subtitle(f"● Sesión iniciada (Firefox, caduca {s['expires']})" if s["ok"] else
                                      "✕ Sin sesión: inicia sesión en tiktok.com con Firefox y pulsa Comprobar")
        return s["ok"]

    def pick_screen(self, forget):
        if self.mode in ("test", "connecting", "live", "stopping"): return
        def go():
            if self.portal: self.portal.close()
            self.portal_ready = False; self.row_pick.set_subtitle("Esperando selector de GNOME…")
            self.portal = E.ScreenPortal(self.log); self.portal.start(self.portal_ok, self.portal_err, forget=forget)
        self.stop_engine(go)  # the running pipeline uses the old session

    def portal_ok(self, portal):
        self.portal_ready = True
        self.row_pick.set_subtitle(("Pantalla completa" if portal.monitor else "Ventana") + f" · PipeWire nodo {portal.node}")
        self.schedule_rebuild()

    def portal_err(self, msg):
        self.log("PORTAL=FAIL " + msg); self.portal_ready = False
        self.row_pick.set_subtitle("No seleccionada · " + msg); self.schedule_rebuild()

    def validate(self):
        c = self.cfg
        if not E.layout(c): return "Activa pantalla y/o cámara."
        if c["screen"] and not self.portal_ready: return "Pantalla activada pero sin seleccionar: pulsa «Seleccionar…»."
        err, _ = E.check_devices(c, self.cams)
        return err and err[1]

    # ------------------------------------------------------------ engine / preview
    def attach(self, eng):
        """Show the engine's appsink frames in the preview picture (latest frame wins)."""
        self._shown = eng; self.picture.set_paintable(None)
        sink = eng.pipe.get_by_name("preview")
        if not sink: return
        def on_sample(s):  # streaming thread: copy the frame, paint it on the UI thread
            smp = s.emit("pull-sample"); st = smp.get_caps().get_structure(0); buf = smp.get_buffer()
            w, h = st.get_value("width"), st.get_value("height"); data = GLib.Bytes.new(buf.extract_dup(0, buf.get_size()))
            def paint():
                if self._shown is eng: self.picture.set_paintable(Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, data, buf.get_size() // h))
            GLib.idle_add(paint); return Gst.FlowReturn.OK
        sink.connect("new-sample", on_sample)

    def stop_engine(self, then):
        eng, self.eng = self.eng, None
        if eng: eng.stop(lambda: then and then())
        elif then: then()

    def schedule_rebuild(self):
        if self._rebuild: GLib.source_remove(self._rebuild)
        self._rebuild = GLib.timeout_add(300, self.rebuild_preview)

    def rebuild_preview(self):
        self._rebuild = 0
        if self.mode not in ("idle", "preview"): return False
        self.stop_engine(self.start_preview); return False

    def start_preview(self):
        self.mode = "idle"; err = self.validate()
        if err: self.overlay.set_text(err); self.overlay.set_visible(True); self.picture.set_paintable(None); return
        _, cam = E.check_devices(self.cfg, self.cams); self.cam_now = cam
        try:
            self.eng = E.Engine(self.cfg, "preview", E.Log("preview.log"), self.portal if self.cfg["screen"] else None, cam, E.preview_sink(self.cfg),
                                on_error=self.preview_error)
        except Exception as e: return self.preview_error(str(e))
        self.attach(self.eng); self.eng.start(); self.overlay.set_visible(False); self.set_mode("preview")

    def preview_error(self, err):
        self.log("PREVIEW_ERROR=" + err); self.eng = None; self.mode = "idle"
        self.overlay.set_text("Preview detenida:\n" + err); self.overlay.set_visible(True)
        if "screen_src" in err: self.portal_ready = False; self.row_pick.set_subtitle("La captura terminó · pulsa «Seleccionar…»")

    def on_pip(self, row, _, key):
        if not self._syncing: self.set_pip(**{key: row.get_value() / 100})

    def set_pip(self, **kw):
        """Move/resize the camera PiP live (compositor pads only, no pipeline rebuild) and keep the spin rows in sync."""
        self.cfg.update(kw)
        if self.eng: self.eng.set_geometry(self.cfg, self.cam_now)
        self._syncing = True
        for k, v in kw.items(): self.pip_rows[k].set_value(round(v * 100))
        self._syncing = False
        if self._pip_save: GLib.source_remove(self._pip_save)
        self._pip_save = GLib.timeout_add(400, lambda: (setattr(self, "_pip_save", 0), E.save_cfg(self.cfg), False)[2])

    def drag_begin(self, g, x, y):
        """Grab the camera only if the press lands on it (preview fills the canvas aspect exactly)."""
        self._drag = None
        if E.layout(self.cfg) != "pip" or not self.picture.get_width(): return
        W, H = E.FORMATS[self.cfg["orientation"]]; sc = self.picture.get_width() / W
        cam = self.cam_now; bx, by, bw, bh = E.geometry(self.cfg, *(cam and (cam["w"], cam["h"]) or (16, 9)))["camera"]
        if bx <= x / sc <= bx + bw and by <= y / sc <= by + bh: self._drag = (bx, by, bw, bh, sc)

    def drag_update(self, g, dx, dy):
        if not self._drag: return
        bx, by, bw, bh, sc = self._drag
        fx, fy = E.pip_fractions(self.cfg, bx + dx / sc, by + dy / sc, bw, bh); self.set_pip(pip_x=fx, pip_y=fy)

    # ------------------------------------------------------------ local test
    def on_test(self, *_):
        err = self.validate()
        if err: return self.info("No se puede probar", err)
        self.ask("Prueba local A/V (8 s)", E.summary(self.cfg, self.cams) + "\n\nNo conecta con TikTok. Guarda logs/av-test.mkv.",
                 "PROBAR", self.run_test)

    def run_test(self):
        self.set_mode("test", "Grabando prueba local de 8 s…")
        tlog = E.Log("av-test.log", echo=self.append_log); tlog("=== TikTok LIVE Native v21 · prueba A/V local (GUI) ===")
        self.cam_now = E.check_devices(self.cfg, self.cams)[1]
        def go():
            eng = E.run_local_test(self.cfg, tlog, self.test_done, screen=self.portal if self.cfg["screen"] else None,
                                   cams=self.cams, preview=E.preview_sink(self.cfg))
            if eng: self.attach(eng)
        self.stop_engine(go)

    def test_done(self, ok, eng):
        lines = [l.split(" ", 1)[1] for l in (E.LOGS / "av-test.log").read_text().splitlines()
                 if l.split(" ", 1)[-1].split("=")[0] in ("SCREEN", "CAMERA", "MIC", "COMPOSITOR", "VIDEO_ENCODER", "AUDIO_ENCODER", "OUTPUT", "ERROR")]
        self.set_mode("idle", "✔ Prueba local correcta · logs/av-test.mkv" if ok else "✖ Prueba local fallida · logs/av-test.log")
        self.info("AV_LOCAL_TEST=" + ("PASS" if ok else "FAIL"), "\n".join(lines))
        self.schedule_rebuild()

    # ------------------------------------------------------------ LIVE
    def on_live(self, *_):
        err = self.validate()
        if err: return self.info("No se puede iniciar", err)
        if not self.refresh_session(): return self.info("Sin sesión TikTok", "Inicia sesión en tiktok.com con Firefox y vuelve a intentarlo.")
        body = E.summary(self.cfg, self.cams, "EMISIÓN REAL")
        self.ask("Vas a iniciar una emisión REAL en TikTok.", body, "INICIAR LIVE", self.go_live, danger=True)

    def go_live(self):
        self.log("LIVE_CONFIRMED_BY_USER"); self.log("\n".join(E.summary(self.cfg, self.cams, "LIVE_CONFIG").splitlines()))
        self.set_mode("connecting", "Obteniendo RTMP de TikTok…")
        def fetch():  # network off the UI thread; /room/create/ is called exactly once here
            try: room = tiktok.create_room(self.log, self.cfg["title"]); GLib.idle_add(self.start_live, room)
            except Exception as e: GLib.idle_add(self.live_failed, str(e))
        threading.Thread(target=fetch, daemon=True).start()

    def start_live(self, room):
        if self.mode != "connecting":  # user pressed stop while TikTok was creating the room: close it
            self.log("LIVE_ABORTED_BEFORE_START"); self.room = room; self.finish_room(); return
        self.room = room; self.live_url = room["url"]; self.retries = 0; self.stop_engine(self.launch_live)
        if not room["user"]: self.log("CHAT=NO_USERNAME")

    def launch_live(self):
        if self.mode not in ("connecting", "live"): return False
        err, cam = E.check_devices(self.cfg, self.cams); self.cam_now = cam
        if err: self.live_failed(err[1]); return False
        rec = None
        if self.cfg["record_live"]:  # one file per (re)connection: a cut FLV stays playable up to the cut
            rec = E.recordings_dir() / time.strftime("live-%Y%m%d-%H%M%S.flv"); self.log(f"RECORDING={rec.name}")
            self.last_rec = rec
        try:
            eng = E.Engine(self.cfg, "rtmp", self.log, self.portal if self.cfg["screen"] else None, cam, E.preview_sink(self.cfg), record=rec,
                           rtmp_url=self.live_url, on_error=self.live_error, on_eos=lambda: self.live_error("EOS inesperado"))
        except Exception as e: self.live_failed(str(e)); return False
        self.eng = eng; self.attach(eng); self.overlay.set_visible(False)
        self.log("STREAMING=CONNECTING"); eng.start(); self.set_mode("live", "Conectando con TikTok…")
        state = {"bytes": 0, "started": False}
        def watch():
            if self.eng is not eng or self.mode != "live": return False
            if eng.bytes and not state["started"]:
                state["started"] = True; self.retries = 0; self.log("STREAMING=STARTED")
                user = (self.room or {}).get("user")
                if user and not self.room.get("chat"):  # once per room (not on reconnects); TikTok needs the stream first
                    self.room["chat"] = True; r = self.room
                    current = lambda: self.room is r and self.mode == "live"  # user may stop within these seconds
                    GLib.timeout_add_seconds(5, lambda: (current() and self.chat.load(user), False)[1])
                    if r.get("title"): GLib.timeout_add_seconds(10, lambda: (current() and self.check_title(r), False)[1])
                    GLib.timeout_add_seconds(5, lambda: self.poll_viewers(r, current))
            kbps = (eng.bytes - state["bytes"]) * 8 // 1000; state["bytes"] = eng.bytes
            v = (self.room or {}).get("viewers")
            if state["started"]: self.status.set_markup(f"<span foreground='#e01b24'>●</span> EN DIRECTO · {kbps} kbps enviados"
                                                       + (f" · 👁 {v} espectadores" if v is not None else "")
                                                       + (" · ⚠ TikTok no muestra el título" if (self.room or {}).get("title_bad") else ""))
            return True
        GLib.timeout_add_seconds(1, watch)
        return False

    def live_error(self, err):
        self.eng = None
        if self.mode != "live": return
        self.log("STREAMING=ERROR")
        if self.retries < MAX_RETRIES and "screen_src" not in err and "cam_src" not in err and "mic_src" not in err:
            self.retries += 1; self.log(f"RECONNECT_ATTEMPT={self.retries}/{MAX_RETRIES}")
            self.status.set_text(f"Conexión perdida · reintento {self.retries}/{MAX_RETRIES} en 3 s…")
            GLib.timeout_add_seconds(3, self.launch_live)
        else: self.live_failed(err)

    def check_title(self, room):
        def run():
            got = tiktok.room_title(self.log, room["room_id"]); ok = got == room["title"]
            self.log("TITLE=" + ("APPLIED" if ok else "NOT_APPLIED"))
            room["title_bad"] = not ok  # shown on the live status line
        threading.Thread(target=run, daemon=True).start()

    STATS = E.CFG / "live-stats.json"  # read by the chat overlay process

    def poll_viewers(self, room, current):
        """Official viewer count (room/info user_count) every 15 s; the web chat column only counts itself for the host."""
        if not current(): self.STATS.unlink(missing_ok=True); return False
        def run():
            n = tiktok.room_info(lambda *_: None, room["room_id"]).get("user_count")  # quiet: no log line every 15 s
            if n is not None:
                room["viewers"] = n; self.STATS.write_text(json.dumps({"viewers": n}))
        threading.Thread(target=run, daemon=True).start()
        GLib.timeout_add_seconds(15, lambda: self.poll_viewers(room, current)); return False

    def finish_room(self):
        """Close the TikTok room now (off the UI thread). Without it TikTok keeps it 'EN VIVO' until a timeout."""
        room, self.room = self.room, None; self.STATS.unlink(missing_ok=True)
        if room: threading.Thread(target=tiktok.finish_room, args=(self.log, room), daemon=True).start()

    def live_failed(self, err):
        self.log("STREAMING=FAILED"); self.live_url = None; self.finish_room(); self.chat.stop_overlay()
        self.set_mode("idle", "✖ LIVE no iniciado / detenido por error"); self.info("Error en LIVE", err); self.schedule_rebuild()

    def on_stop(self, *_):
        if self.mode not in ("connecting", "live"): return
        self.set_mode("stopping", "Deteniendo LIVE…")
        def done():
            self.log("STREAMING=STOPPED"); self.live_url = None; self.finish_room(); self.chat.stop_overlay()
            rec = getattr(self, "last_rec", None); self.last_rec = None
            self.set_mode("idle", "LIVE detenido" + (f" · grabado en {esc(rec)}" if rec and rec.exists() else "")); self.schedule_rebuild()
        self.stop_engine(done)

    def on_close(self, *_):
        if self.mode == "live": self.log("STREAMING=STOPPED reason=window_closed")
        def bye():
            if self.room: tiktok.finish_room(self.log, self.room); self.room = None  # synchronous: the app is exiting
            self.chat.stop_overlay()
            if self.portal: self.portal.close()
            self.win.destroy()
        self.mode = "closing"
        if self.eng: self.stop_engine(bye); return True
        bye(); return False


app = Adw.Application(application_id="io.github.tiktok_live_native")
app.connect("activate", lambda a: Win(a))
sys.exit(app.run(None))
