# -*- coding: utf-8 -*-
"""
Captura la grafica de "Demanda en Tiempo Real" de XM (Sinergox, Power BI)
y la guarda en docs/tr/ para mostrarla en el tablero.

  python captura.py

Si la captura falla, intenta conservar la ultima imagen publicada en la pagina,
para que el panel no quede vacio.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

URL_XM = "https://sinergox.xm.com.co/demand/real-demand/real-time-demand"
URL_PUBLICADA = os.environ.get("URL_TABLERO", "https://cainavy.github.io/tablero-xm/")
CARPETA = os.path.join("docs", "tr")
IMAGEN = os.path.join(CARPETA, "demanda.png")
INFO = os.path.join(CARPETA, "captura.json")
TZ_CO = timezone(timedelta(hours=-5))

# Oculta aviso de cookies, el asistente "BIX" y cualquier elemento flotante
CSS_LIMPIEZA = """
  [class*="cookie" i], [id*="cookie" i], [class*="bix" i], [id*="bix" i],
  [class*="chat" i], [id*="chat" i] { display: none !important; }
"""


def capturar():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            navegador = p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        except Exception:
            navegador = p.chromium.launch(args=["--no-sandbox"])
        # bypass_csp: la pagina de XM prohibe estilos insertados; esto solo
        # afecta a nuestro navegador temporal y permite ocultar avisos.
        pagina = navegador.new_page(viewport={"width": 1500, "height": 1000},
                                    device_scale_factor=1.5, locale="es-CO",
                                    timezone_id="America/Bogota", bypass_csp=True)
        # Guardar las respuestas de Power BI con los datos de la grafica
        respuestas = []
        pagina.on("response", lambda r: respuestas.append(r) if "querydata" in r.url.lower() else None)
        pagina.goto(URL_XM, wait_until="domcontentloaded", timeout=90000)

        # Rechazar cookies opcionales si aparece el aviso
        try:
            pagina.get_by_role("button", name="Rechazar").click(timeout=8000)
        except Exception:
            pass
        try:
            pagina.add_style_tag(content=CSS_LIMPIEZA)
        except Exception as e:
            print("Aviso: no se pudo ocultar avisos (%s)" % str(e)[:80])

        marco = pagina.locator('iframe[src*="powerbi"]').first
        marco.wait_for(state="attached", timeout=90000)
        marco.scroll_into_view_if_needed()

        # Esperar a que Power BI dibuje la grafica (aparecen elementos SVG)
        contenido = pagina.frame_locator('iframe[src*="powerbi"]').first
        contenido.locator("svg").first.wait_for(state="visible", timeout=120000)
        time.sleep(12)  # tiempo extra para que termine de pintar las lineas

        # Quitar elementos fijos que puedan tapar la grafica
        pagina.evaluate("""() => {
            const iframe = document.querySelector('iframe[src*="powerbi"]');
            for (const el of document.querySelectorAll('body *')) {
                const s = getComputedStyle(el);
                if ((s.position === 'fixed' || s.position === 'sticky') && !el.contains(iframe)) {
                    el.style.display = 'none';
                }
            }
        }""")
        time.sleep(1)

        # Hora del dato que muestra XM (titulo "Demanda en tiempo real ... 02:30:09")
        frame = next((f for f in pagina.frames if "powerbi" in (f.url or "")), None)
        dato = None
        recorte = None
        if frame is not None:
            try:
                texto = frame.evaluate("() => document.body.innerText || ''")
                m = re.search(r"tiempo real[^\n]*?(\d{1,2}:\d{2})(:\d{2})?", texto, re.I)
                if m:
                    dato = m.group(1).zfill(5)
            except Exception as e:
                print("Aviso: no se leyo la hora del dato (%s)" % str(e)[:80])
            # Recortar el espacio vacio: union de los visuales de Power BI
            try:
                caja = frame.evaluate("""() => {
                    const els = document.querySelectorAll('visual-container, visual-container-modern, .visualContainer, .visual-container');
                    let x0=1e9, y0=1e9, x1=-1, y1=-1;
                    for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 20 || r.height < 20) continue;
                        x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top);
                        x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
                    }
                    return x1 < 0 ? null : {x: x0, y: y0, w: x1 - x0, h: y1 - y0};
                }""")
                bi = marco.bounding_box()
                if caja and bi and caja["w"] * caja["h"] > 0.15 * bi["width"] * bi["height"]:
                    pad = 10
                    x = max(bi["x"], bi["x"] + caja["x"] - pad)
                    y = max(bi["y"], bi["y"] + caja["y"] - pad)
                    recorte = {"x": x, "y": y,
                               "width": min(bi["x"] + bi["width"], bi["x"] + caja["x"] + caja["w"] + pad) - x,
                               "height": min(bi["y"] + bi["height"], bi["y"] + caja["y"] + caja["h"] + pad) - y}
            except Exception as e:
                print("Aviso: sin recorte (%s)" % str(e)[:80])

        os.makedirs(CARPETA, exist_ok=True)
        if recorte:
            pagina.screenshot(path=IMAGEN, clip=recorte, timeout=60000)
            print("Recorte:", {k: round(v) for k, v in recorte.items()})
        else:
            marco.screenshot(path=IMAGEN, timeout=60000)
        consultas = []
        for r in respuestas:
            try:
                consultas.append(r.json())
            except Exception as e:
                consultas.append({"error": str(e)[:200]})
        with open(os.path.join(CARPETA, "consultas.json"), "w", encoding="utf-8") as f:
            json.dump(consultas, f, ensure_ascii=False)
        print("Respuestas de Power BI guardadas:", len(consultas))
        navegador.close()

    if os.path.getsize(IMAGEN) < 20000:
        raise RuntimeError("la captura salio casi vacia (%d bytes)" % os.path.getsize(IMAGEN))
    return dato


def conservar_anterior():
    """Descarga la ultima captura publicada para no dejar el panel vacio."""
    os.makedirs(CARPETA, exist_ok=True)
    for nombre, destino in (("tr/demanda.png", IMAGEN), ("tr/captura.json", INFO)):
        try:
            with urllib.request.urlopen(URL_PUBLICADA + nombre + "?n=%d" % time.time(), timeout=30) as r:
                with open(destino, "wb") as f:
                    f.write(r.read())
        except Exception as e:
            print("No se pudo recuperar", nombre, "->", e)


def main():
    ahora = datetime.now(TZ_CO)
    try:
        dato = capturar()
        with open(INFO, "w", encoding="utf-8") as f:
            json.dump({"hora": ahora.strftime("%H:%M"), "fecha": ahora.strftime("%d/%m/%Y"),
                       "iso": ahora.isoformat(timespec="seconds"), "dato": dato, "ok": True}, f)
        print("[OK] Captura guardada:", IMAGEN, os.path.getsize(IMAGEN), "bytes | dato XM:", dato)
    except Exception as e:
        print("[ERROR] Captura fallida:", e)
        conservar_anterior()
        # No se detiene el flujo: el resto del tablero se publica igual
    return 0


if __name__ == "__main__":
    sys.exit(main())
