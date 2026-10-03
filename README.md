<img src="packaging/icons/hicolor/128x128/apps/tiktok-live-native.png" alt="" width="96" align="right">

# TikTok LIVE Native

Aplicación nativa de Linux para emitir directamente a **TikTok LIVE**: pantalla o ventana, cámara, micrófono y audio
del equipo, con vista previa, chat superpuesto y grabación. Emite con su propio motor o con el **OBS integrado** que
lleva dentro, ya configurado. Sin Wine, sin máquinas virtuales y sin TikTok LIVE Studio.

> Proyecto independiente, **no oficial**, sin relación con TikTok ni ByteDance. Usa la sesión web de tu propia
> cuenta; tu cuenta debe tener acceso a LIVE.

## Descarga

Descarga `TikTok-LIVE-Native-x86_64.AppImage` desde [Releases](../../releases) y:

```bash
chmod +x TikTok-LIVE-Native-x86_64.AppImage
./TikTok-LIVE-Native-x86_64.AppImage
```

Funciona en distribuciones con glibc ≥ 2.39: **Ubuntu 24.04+, Linux Mint 22+, Debian 13, Fedora 40+, Arch,
openSUSE Tumbleweed**. No funciona en Ubuntu 22.04 ni Debian 12 (no tienen WebKitGTK 6).

Requisitos del sistema:
- Sesión gráfica (Wayland o X11) con **PipeWire** y **xdg-desktop-portal** (lo normal en GNOME y KDE actuales).
- **Firefox** con la sesión de TikTok iniciada (también Firefox Snap o Flatpak).

Diagnóstico rápido en cualquier PC: `./TikTok-LIVE-Native-x86_64.AppImage --selftest` (y `--obs-test` para el OBS integrado)

## Qué hace

- **Fuentes:** pantalla completa o ventana (portal de escritorio + PipeWire), cámara V4L2 con resolución elegible,
  micrófono y sonido del equipo mezclados.
- **Formatos:** vertical 720×1280, vertical 1080×1920, horizontal 1920×1080, a 25/30/60 fps. Sin deformar la imagen.
- **Cámara en recuadro:** se arrastra con el ratón en la vista previa, rueda para el tamaño, botones de esquina.
  Se puede mover en pleno directo.
- **TikTok:** título del LIVE, confirmación antes de emitir, la sala se crea solo al pulsar *Iniciar LIVE*,
  reconexión automática y cierre de la sala al detener.
- **Chat y espectadores:** panel en la app y **chat superpuesto semitransparente** siempre encima, que se puede mover,
  redimensionar y ocultar. Muestra el contador oficial de espectadores.
- **Grabación** opcional en **MP4**: copia exacta de lo emitido en `~/Vídeos/TikTok LIVE/` (reproducible aunque se corte).
- **Audio vigilado:** el sonido del equipo sigue a la salida donde realmente suena (p. ej. si cambias a auriculares) y,
  si el audio se pierde durante el directo, la app avisa y rehace la cadena sola.
- **Prueba local** de 8 s sin conectar con TikTok.
- **OBS integrado (opcional):** en «Emitir con» eliges la app u OBS. Con OBS, la app genera la misma escena
  (pantalla/ventana, cámara en su sitio, micrófono, audio del equipo, formato, FPS, bitrate), pone la clave de TikTok y
  abre OBS emitiendo. Usa su propia configuración (no toca un OBS que tengas instalado) y borra la clave al cerrarse.
- **Datos para OBS:** si prefieres emitir con OBS, la app te da el servidor y la clave de TikTok para copiar y pegar
  (y un botón para finalizar la sala al acabar).

## Cómo funciona

```
pantalla/ventana (portal → PipeWire) ─┐
cámara (V4L2) ────────────────────────┤ compositor ─┬─ vista previa
micrófono + audio del equipo ── mezclador          ├─ H.264 (x264) ─┐
                                                    └────────────────┴─ FLV → RTMP de TikTok
                                                                       └─ MP4 (grabación)
```

Todo el vídeo y audio pasa por GStreamer. La URL RTMP se obtiene con la sesión web de TikTok de tu Firefox,
en el momento de iniciar el LIVE.

## Privacidad y seguridad

- Las cookies se leen de una copia temporal del perfil de Firefox y no se guardan nunca.
- La URL y la clave RTMP **no** se escriben en ningún log (se enmascaran como `***`).
- Datos locales: logs y configuración en `~/.local/share/tiktok-live-native/` (AppImage) o `./logs`, `./config`
  (código fuente).
- Antes de cada emisión real se pide confirmación explícita.

## Ejecutar desde el código (Debian 13 / Ubuntu 24.04)

```bash
sudo apt install python3-gi python3-requests gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0 gir1.2-soup-3.0 \
  gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-{base,good,bad,ugly} gstreamer1.0-libav \
  gstreamer1.0-pipewire gstreamer1.0-pulseaudio pulseaudio-utils
./run.sh          # la app
./test.sh         # pruebas locales (no contacta con TikTok)
```

| Archivo | Contenido |
|---|---|
| `app.py` | Interfaz GTK4/libadwaita |
| `engine.py` | Motor GStreamer: captura, composición, codificación, RTMP, pruebas |
| `tiktok.py` | Sesión de Firefox, creación/cierre de sala, información de la sala |
| `chat_overlay.py` | Vista de chat y chat superpuesto |
| `av_test.py` | Prueba A/V por línea de comandos |
| `packaging/` | Construcción y pruebas del AppImage |

## Construir el AppImage

Necesita Docker:

```bash
packaging/build-appimage.sh    # → dist/TikTok-LIVE-Native-x86_64.AppImage (base Ubuntu 24.04)
packaging/test-distros.sh      # --selftest en contenedores Ubuntu, Debian, Fedora, Arch y openSUSE
```

GitHub Actions hace lo mismo en cada *push*, y al crear una etiqueta `v*` publica el AppImage como Release.

## Limitaciones conocidas

- El chat superpuesto usa XWayland: GNOME en Wayland no permite «siempre encima» a aplicaciones normales.
  En juegos a pantalla completa exclusiva puede quedar oculto; en ventana sin bordes se ve.
- En el AppImage el visor web del chat se ejecuta sin el aislamiento interno de WebKit (no es compatible con
  AppImage); solo carga tiktok.com.
- Si emites la pantalla completa mientras ves tu propio LIVE en el navegador, aparece un efecto túnel (es lo que
  se está capturando). Emite otra ventana o cierra esa pestaña.
- Dos usuarios distintos del mismo PC no pueden ejecutar el AppImage a la vez.

## Licencia

[MIT](LICENSE). Proyecto no oficial; TikTok es una marca de ByteDance Ltd.
