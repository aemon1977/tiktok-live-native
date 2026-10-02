#!/usr/bin/env python3
"""TikTok viewers+chat view, and a translucent always-on-top overlay of it.

  python3 chat_overlay.py <tiktok_user>     overlay (started/stopped by app.py during LIVE)

GNOME/Wayland gives regular apps no way to stay above other windows, so ONLY this overlay process runs through
XWayland (GDK_BACKEND=x11) and asks the window manager for _NET_WM_STATE_ABOVE + _NET_WM_WINDOW_OPACITY.
Screen capture is untouched (portal + PipeWire). The Firefox session lives in an ephemeral WebKit session.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__": os.environ["GDK_BACKEND"] = "x11"  # must precede Gtk import
import ctypes, json, warnings
import gi
warnings.filterwarnings("ignore", category=DeprecationWarning)  # GdkX11 xid/skip-taskbar: deprecated, still the only X11 route
gi.require_version("Gtk", "4.0"); gi.require_version("Gdk", "4.0"); gi.require_version("WebKit", "6.0"); gi.require_version("Soup", "3.0")
from gi.repository import Gdk, GLib, Gtk, Soup, WebKit
import tiktok

PANEL = '[data-e2e="public-screen-container"]'  # TikTok's test id for the viewers+chat column (stabler than classes)
CSS = PANEL + """{display:flex!important;position:fixed!important;inset:0!important;width:100vw!important;height:100vh!important;
max-width:none!important;margin:0!important;border-radius:0!important;z-index:2147483647!important}
video{visibility:hidden!important}"""
# Overlay look: page fully transparent, one dark veil, white shadowed text.
OVERLAY_CSS = CSS + """body *{visibility:hidden!important}""" + PANEL + "," + PANEL + """ *{visibility:visible!important}
html,body,""" + PANEL + """ *{background:transparent!important;background-color:transparent!important;border-color:rgba(255,255,255,.25)!important}
""" + PANEL + """{background:rgba(0,0,0,.40)!important;box-sizing:border-box!important;padding-bottom:22px!important}
[data-e2e="live-chat-input-container"]{flex-shrink:0!important}
[data-e2e="live-chat-input-container"] > *{background:rgba(0,0,0,.55)!important;border-radius:12px!important}
""" + PANEL + """ *{color:#fff!important;text-shadow:0 1px 2px #000,0 0 4px #000}"""
# Own stream video: muted+paused (no echo, no CPU). Until TikTok sees the stream the page has no chat: reload until it does.
JS = """(()=>{const q='%s',post=m=>{try{webkit.messageHandlers.tt.postMessage(m)}catch(e){}};
let told=false;setInterval(()=>{document.querySelectorAll('video').forEach(v=>{v.muted=true;v.pause()});
if(!told&&document.querySelector(q)){told=true;post('ready')}},1000);
setTimeout(()=>{if(!document.querySelector(q)){post('reload');location.reload()}},8000);})()""" % PANEL.replace("'", "\\'")


def chat_view(user, log, overlay=False):
    """WebView of tiktok.com/@user/live reduced to the viewers+chat column, logged in with the Firefox session."""
    ns = WebKit.NetworkSession.new_ephemeral(); cm = ns.get_cookie_manager()
    for h, n, v, pa, se, _ in tiktok.find_session()[1]:
        c = Soup.Cookie.new(n, v, h, pa or "/", -1); c.set_secure(bool(se)); cm.add_cookie(c, None, None, None)
    ucm = WebKit.UserContentManager()
    ucm.add_style_sheet(WebKit.UserStyleSheet(OVERLAY_CSS if overlay else CSS, WebKit.UserContentInjectedFrames.TOP_FRAME,
                                              WebKit.UserStyleLevel.USER, None, None))
    ucm.add_script(WebKit.UserScript(JS, WebKit.UserContentInjectedFrames.TOP_FRAME, WebKit.UserScriptInjectionTime.END, None, None))
    ucm.register_script_message_handler("tt", None)
    ucm.connect("script-message-received::tt", lambda m, v: log("CHAT=" + ("READY" if v.to_string() == "ready" else "WAITING_RELOAD")))
    view = WebKit.WebView(network_session=ns, user_content_manager=ucm, vexpand=True)
    view.get_settings().set_user_agent(tiktok.UA); view.set_is_muted(True)
    if overlay:
        clear = Gdk.RGBA(); clear.parse("rgba(0,0,0,0)"); view.set_background_color(clear)
    view.load_uri(f"https://www.tiktok.com/@{user}/live")
    return view


# ------------------------------------------------------------------ X11 window-manager hints (overlay only)
class _XClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong), ("format", ctypes.c_int), ("l", ctypes.c_long * 5),
                ("pad", ctypes.c_long * 12)]  # XEvent is 24 longs


class WM:
    def __init__(self):
        self.x = ctypes.CDLL("libX11.so.6")
        self.x.XOpenDisplay.restype = ctypes.c_void_p; self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x.XInternAtom.restype = ctypes.c_ulong; self.x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.x.XDefaultRootWindow.restype = ctypes.c_ulong; self.x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p]
        self.x.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_int]
        self.x.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
        self.x.XFlush.argtypes = [ctypes.c_void_p]
        self.x.XTranslateCoordinates.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.d = self.x.XOpenDisplay(None)
        if not self.d: raise RuntimeError("XWayland no disponible")
        self.atom = lambda n: self.x.XInternAtom(self.d, n.encode(), 0)

    def above(self, xid):
        ev = _XClientMessage(type=33, send_event=1, window=xid, message_type=self.atom("_NET_WM_STATE"), format=32)  # ClientMessage
        ev.l[0], ev.l[1], ev.l[3] = 1, self.atom("_NET_WM_STATE_ABOVE"), 1  # _NET_WM_STATE_ADD, source=application
        self.x.XSendEvent(self.d, self.x.XDefaultRootWindow(self.d), 0, (1 << 19) | (1 << 20), ctypes.byref(ev)); self.x.XFlush(self.d)

    def opacity(self, xid, value):
        v = ctypes.c_ulong(int(max(0.1, min(value, 1.0)) * 0xFFFFFFFF))
        self.x.XChangeProperty(self.d, xid, self.atom("_NET_WM_WINDOW_OPACITY"), 6, 32, 0, ctypes.byref(v), 1)  # XA_CARDINAL, replace
        self.x.XFlush(self.d)

    def move(self, xid, x, y): self.x.XMoveWindow(self.d, xid, x, y); self.x.XFlush(self.d)

    def position(self, xid):
        """Window's top-left in root coordinates (GTK4 has no window-position API)."""
        x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        self.x.XTranslateCoordinates(self.d, xid, self.x.XDefaultRootWindow(self.d), 0, 0, ctypes.byref(x), ctypes.byref(y), ctypes.byref(child))
        return x.value, y.value


def overlay(user):
    import engine as E
    log = E.Log("chat-overlay.log", echo=print); log(f"OVERLAY=START backend=x11")
    gi.require_version("GdkX11", "4.0"); from gi.repository import GdkX11
    wm = WM(); app = Gtk.Application(application_id="io.github.tiktok_live_native.chat")
    prefs_file = E.CFG / "overlay.json"  # size, opacity, position
    try: prefs = {"w": 360, "h": 620, "opacity": 0.85, **json.loads(prefs_file.read_text())}
    except Exception: prefs = {"w": 360, "h": 620, "opacity": 0.85}
    pending = []
    def save(**kw):
        prefs.update(kw)
        if not pending: pending.append(GLib.timeout_add(500, lambda: (pending.clear(), prefs_file.write_text(json.dumps(prefs)), False)[2]))

    def activate(a):
        css = Gtk.CssProvider(); css.load_from_string(
            "window.ttoverlay{background:transparent;} .ttbar{background:rgba(0,0,0,.55);color:#fff;padding:2px 8px;border-radius:8px 8px 0 0;}"
            " .ttgrip{background:rgba(0,0,0,.55);color:#fff;padding:0 4px;border-radius:6px 0 0 0;font-size:16px;}")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = Gtk.ApplicationWindow(application=a, title="TikTok chat", decorated=False, default_width=prefs["w"], default_height=prefs["h"])
        win.connect("notify::default-width", lambda w, _: save(w=w.get_default_size()[0]))
        win.connect("notify::default-height", lambda w, _: save(h=w.get_default_size()[1]))
        win.add_css_class("ttoverlay")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL); win.set_child(box)
        bar = Gtk.Box(spacing=6); bar.add_css_class("ttbar")
        title = Gtk.Label(label="✥ Chat TikTok", hexpand=True, xalign=0); bar.append(title)
        op = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.3, 1.0, 0.05); op.set_value(prefs["opacity"]); op.set_size_request(110, -1)
        op.set_tooltip_text("Opacidad"); bar.append(op)
        corners = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        menu = Gtk.MenuButton(icon_name="view-grid-symbolic", tooltip_text="Llevar a una esquina", popover=Gtk.Popover(child=corners))
        menu.add_css_class("flat"); bar.append(menu)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Ocultar (vuelve con «Superponer» en la app)"); close.add_css_class("flat"); close.connect("clicked", lambda *_: a.quit())
        bar.append(close); box.append(Gtk.WindowHandle(child=bar))  # drag the bar to move the overlay
        # Undecorated windows have no resize border: a corner grip asks the WM for an interactive resize.
        ov = Gtk.Overlay(vexpand=True); ov.set_child(chat_view(user, log, overlay=True)); box.append(ov)
        grip = Gtk.Label(label="◢", halign=Gtk.Align.END, valign=Gtk.Align.END, tooltip_text="Arrastra para cambiar el tamaño")
        grip.add_css_class("ttgrip"); grip.set_cursor_from_name("se-resize"); ov.add_overlay(grip)
        click = Gtk.GestureClick()
        def resize(g, n, x, y):
            wx, wy = grip.translate_coordinates(win, x, y)
            win.get_surface().begin_resize(Gdk.SurfaceEdge.SOUTH_EAST, g.get_device(), g.get_current_button(), wx, wy, g.get_current_event_time())
        click.connect("pressed", resize); grip.add_controller(click)

        def mapped(*_):
            xid = GdkX11.X11Surface.get_xid(win.get_surface())
            win.get_surface().set_skip_taskbar_hint(True)
            mon = Gdk.Display.get_default().get_monitor_at_surface(win.get_surface()).get_geometry()
            def corner(fx, fy):
                w, h = win.get_width(), win.get_height(); m = 20
                wm.move(xid, mon.x + m + round((mon.width - w - 2 * m) * fx), mon.y + m + round((mon.height - h - 2 * m) * fy))
            for label, fx, fy in (("↖ Arriba izquierda", 0, 0), ("↗ Arriba derecha", 1, 0), ("↙ Abajo izquierda", 0, 1), ("↘ Abajo derecha", 1, 1)):
                b = Gtk.Button(label=label); b.add_css_class("flat")
                b.connect("clicked", lambda _b, fx=fx, fy=fy: (corner(fx, fy), menu.popdown())); corners.append(b)
            if "x" in prefs: wm.move(xid, prefs["x"], prefs["y"])  # where the user left it last time
            else: corner(1, 0.08)
            wm.above(xid); wm.opacity(xid, op.get_value())
            op.connect("value-changed", lambda s: (wm.opacity(xid, s.get_value()), save(opacity=round(s.get_value(), 2))))
            def track():  # remember position after mouse drags or corner presets; show the app's official viewer count
                try: title.set_label(f"✥ Chat TikTok · 👁 {json.loads((E.CFG / 'live-stats.json').read_text())['viewers']}")
                except Exception: pass
                x, y = wm.position(xid)
                if (x, y) != (prefs.get("x"), prefs.get("y")): save(x=x, y=y)
                return True
            GLib.timeout_add_seconds(1, track)
            log("OVERLAY=ABOVE")
        win.connect("map", lambda *_: GLib.idle_add(lambda: (mapped(), False)[1]))
        parent = os.getppid()  # app.py died without stopping us: don't stay on screen orphaned
        GLib.timeout_add_seconds(2, lambda: (os.getppid() == parent) or (log("OVERLAY=PARENT_GONE"), a.quit()))
        win.present()
    app.connect("activate", activate)
    app.run(None)
    log("OVERLAY=CLOSED")


if __name__ == "__main__":
    if len(sys.argv) != 2: sys.exit("uso: chat_overlay.py <usuario_tiktok>")
    overlay(sys.argv[1])
