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


def decodificar_dsr(dsr):
    """Convierte el formato comprimido de Power BI (DSR) en una lista de filas {campo: valor}.
    En cada fila, "R" marca campos que repiten el valor anterior y "\u00d8" campos vacios."""
    filas = []
    for ds in dsr.get("DS", []):
        for ph in ds.get("PH", []):
            for clave, lista in ph.items():
                if not clave.startswith("DM"):
                    continue
                campos, previa = None, {}
                for fila in lista:
                    if "S" in fila:
                        campos = [c["N"] for c in fila["S"]]
                    if not campos:
                        continue
                    rep, nul = fila.get("R", 0), fila.get("\u00d8", 0)
                    vals, actual = list(fila.get("C", [])), {}
                    for i, nom in enumerate(campos):
                        if rep >> i & 1:
                            actual[nom] = previa.get(nom)
                        elif nul >> i & 1:
                            actual[nom] = None
                        else:
                            actual[nom] = vals.pop(0) if vals else None
                    filas.append(actual)
                    previa = actual
    return filas


def extraer_curva(consultas):
    """Busca la consulta con la fecha y los dos valores (pronostico y tiempo real).
    Devuelve {"pron": [[minuto, MW], ...], "real": [[minuto, MW], ...]} o None."""
    for c in consultas:
        try:
            datos = c["results"][0]["result"]["data"]
            sel = datos["descriptor"]["Select"]
        except Exception:
            continue
        grupos = [s for s in sel if s.get("Kind") == 1]
        fecha = next((s["Value"] for s in grupos if any(k in s.get("Name", "").lower() for k in ("fecha", "hora", "time"))),
                     grupos[0]["Value"] if grupos else None)
        medidas = [s for s in sel if s.get("Kind") == 2]
        real = next((s["Value"] for s in medidas if any(k in s.get("Name", "").lower() for k in ("valordemanda", "real"))),
                    medidas[0]["Value"] if medidas else None)
        pron = next((s["Value"] for s in medidas if s["Value"] != real and "pron" in s.get("Name", "").lower()),
                    next((s["Value"] for s in medidas if s["Value"] != real), None))
        if not (fecha and real and pron):
            continue
        curva = {"pron": [], "real": []}
        for fila in decodificar_dsr(datos["dsr"]):
            t = fila.get(fecha)
            if isinstance(t, (int, float)):
                # Power BI entrega la hora local de Colombia escrita como si fuera UTC
                h = datetime.fromtimestamp(t / 1000, timezone.utc)
                minuto = round(h.hour * 60 + h.minute + h.second / 60.0, 1)
            elif isinstance(t, str) and re.search(r"\d{1,2}:\d{2}", t):
                hh, mm = re.search(r"(\d{1,2}):(\d{2})", t).groups()
                minuto = int(hh) * 60 + int(mm)
            else:
                continue
            for clave, campo in (("pron", pron), ("real", real)):
                v = fila.get(campo)
                if isinstance(v, (int, float)):
                    curva[clave].append([minuto, round(v, 1)])
        if curva["real"] or curva["pron"]:
            return curva
    return None


JS_SVG = r"""() => {
  // Lee la grafica dibujada (SVG): marcas de los ejes y trazos de las series, en pixeles de pantalla
  const ticksY = [], ticksX = [], muestras = [];
  // Marcas de los ejes: textos cortos ("13 mil", "3:00"), en SVG o en HTML
  for (const t of document.querySelectorAll('text, tspan, title, span, div')) {
    if (t.childElementCount > 0 && t.tagName.toLowerCase() !== 'text') continue;
    const s = (t.textContent || '').replace(/[   ]/g, ' ').trim();
    if (!s || s.length > 12) continue;
    let r = t.getBoundingClientRect();
    if (t.tagName.toLowerCase() === 'title' && t.parentElement) r = t.parentElement.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    if (muestras.length < 40) muestras.push(t.tagName + ':' + s + '@' + Math.round(r.left) + ',' + Math.round(r.top));
    let m = s.match(/^([0-9]+(?:[.,][0-9]+)?) ?mil$/i);
    if (m) { ticksY.push([parseFloat(m[1].replace(',', '.')) * 1000, r.top + r.height / 2, r.right]); continue; }
    m = s.match(/^([0-9]+)\.([0-9]{3})$/);
    if (m) { ticksY.push([parseFloat(m[1] + m[2]), r.top + r.height / 2, r.right]); continue; }
    m = s.match(/^([0-9]{1,2}):([0-9]{2})$/);
    if (m) ticksX.push([parseInt(m[1]) * 60 + parseInt(m[2]), r.left + r.width / 2, r.top]);
  }
  const trazos = [];
  for (const p of document.querySelectorAll('svg path')) {
    let largo = 0;
    try { largo = p.getTotalLength(); } catch (e) { continue; }
    const r = p.getBoundingClientRect();
    if (largo < 150 || r.width < 60) continue;
    const ctm = p.getScreenCTM(); if (!ctm) continue;
    const cs = getComputedStyle(p), n = Math.min(15000, Math.ceil(largo / 0.3)), pts = [];
    for (let i = 0; i <= n; i++) {
      const q = p.getPointAtLength(largo * i / n);
      pts.push([q.x * ctm.a + q.y * ctm.c + ctm.e, q.x * ctm.b + q.y * ctm.d + ctm.f]);
    }
    trazos.push({clase: String(p.getAttribute('class') || ''), stroke: cs.stroke, fill: cs.fill,
                 opac: cs.fillOpacity, largo: largo, caja: [r.left, r.top, r.width, r.height], pts: pts});
  }
  return {ticksY, ticksX, trazos, muestras};
}"""


def ajuste(pares):
    """Recta por minimos cuadrados: pixel -> valor."""
    n = float(len(pares))
    sx = sum(p for p, _ in pares); sy = sum(v for _, v in pares)
    sxx = sum(p * p for p, _ in pares); sxy = sum(p * v for p, v in pares)
    a = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    return a, (sy - a * sx) / n


def curva_desde_svg(lect):
    """Convierte los trazos del SVG en (minuto del dia, MW). Cada serie se toma por su borde superior."""
    ty = {round(y, 1): v for v, y, _ in lect["ticksY"]}
    tx = {round(x, 1): v for v, x, _ in lect["ticksX"]}
    if len(ty) < 2 or len(tx) < 2:
        return None
    ay, by = ajuste([(y, v) for y, v in ty.items()])
    ax, bx = ajuste([(x, v) for x, v in tx.items()])
    xmin = min(tx) - 5
    ybase = max(ty)  # el eje Y mas bajo marcado
    series = []
    lineas = [t for t in lect["trazos"] if t["clase"].split()[:1] == ["line"]]
    for t in (lineas or lect["trazos"]):
        if "interactivity" in t["clase"]:
            continue
        top = {}
        for px, py in t["pts"]:
            if px < xmin:
                continue
            m = int(round(ax * px + bx))
            if 0 <= m <= 1440:
                v = ay * py + by
                if m not in top or v > top[m]:
                    top[m] = v
        if len(top) < 20:
            continue
        mins = sorted(top)
        # descartar rejillas y lineas planas
        vals = [top[m] for m in mins]
        if max(vals) - min(vals) < 50:
            continue
        series.append({"fin": mins[-1], "ini": mins[0], "puntos": [[m, round(top[m], 1)] for m in mins],
                       "color": t["stroke"] if t["stroke"] not in ("none", "") else t["fill"], "clase": t["clase"]})
    if not series:
        return None
    # agrupar por color (orden del documento: Power BI dibuja primero el pronostico)
    grupos = []
    for x in series:
        g = next((g for g in grupos if g["color"] == x["color"]), None)
        if g is None:
            g = {"color": x["color"], "top": {}}
            grupos.append(g)
        for m, v in x["puntos"]:
            if m not in g["top"] or v > g["top"][m]:
                g["top"][m] = v
    todos = [v for g in grupos for v in g["top"].values()]
    # base del area = fondo de la grafica; se quitan en los extremos los puntos pegados a ella
    base = min(todos) + 0.005 * (max(todos) - min(todos))
    for g in grupos:
        pts = [[m, g["top"][m]] for m in sorted(g["top"])]
        while len(pts) > 2 and pts[-1][1] <= base:
            pts.pop()
        while len(pts) > 2 and pts[0][1] <= base:
            pts.pop(0)
        g["puntos"] = pts
        g["fin"] = pts[-1][0]
    pron = grupos[0]["puntos"]
    real = grupos[1]["puntos"] if len(grupos) > 1 else []
    if len(grupos) > 1 and grupos[0]["fin"] + 15 < grupos[1]["fin"]:
        pron, real = real, pron
    def cada5(p):
        out, ult = [], -10
        for m, v in p:
            if m - ult >= 5 or m == p[-1][0]:
                out.append([m, v]); ult = m
        return out
    if not real and not pron:
        return None
    return {"pron": cada5(pron), "real": cada5(real), "origen": "svg"}


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
        red = []
        pagina.context.on("response", lambda r: red.append((r.request.method, r.status, r.url[:140]))
                          if ("analysis.windows.net" in r.url or "powerbi" in r.url) else None)
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
            except Exception:
                pass
        curva = None
        try:
            curva = extraer_curva(consultas)
        except Exception as e:
            print("Aviso: no se pudieron leer los datos de la grafica (%s)" % str(e)[:80])
        if not curva and frame is not None:   # segundo intento: leer la grafica dibujada
            try:
                lect = frame.evaluate(JS_SVG)
                print("SVG: %d marcas Y, %d marcas X, %d trazos" % (len(lect["ticksY"]), len(lect["ticksX"]), len(lect["trazos"])))
                if len(lect["ticksY"]) < 2 or len(lect["ticksX"]) < 2:
                    print("  textos:", lect.get("muestras"))
                for t in lect["trazos"][:12]:
                    print("  trazo clase=%s stroke=%s fill=%s largo=%d caja=%s" % (
                        t["clase"][:30], t["stroke"], t["fill"], t["largo"], [round(v) for v in t["caja"]]))
                curva = curva_desde_svg(lect)
                if curva:
                    print("Curva desde SVG: %d puntos de pronostico, %d de tiempo real" % (len(curva["pron"]), len(curva["real"])))
            except Exception as e:
                print("Aviso: no se pudo leer el SVG (%s)" % str(e)[:120])
        if not curva:   # diagnostico para ajustar la lectura si Power BI cambia
            print("Red Power BI:")
            for m, st, u in red[:40]:
                print("  ", m, st, u)
            print("Diagnostico Power BI: %d respuestas querydata, %d legibles" % (len(respuestas), len(consultas)))
            for c in consultas[:8]:
                try:
                    datos = c["results"][0]["result"]["data"]
                    print("  Select:", [(s.get("Kind"), s.get("Name"), s.get("Value")) for s in datos["descriptor"]["Select"]])
                    print("  DSR:", json.dumps(datos["dsr"], ensure_ascii=False)[:300])
                except Exception as e:
                    print("  sin formato esperado:", str(e)[:60], json.dumps(c, ensure_ascii=False)[:200])
        navegador.close()

    if os.path.getsize(IMAGEN) < 20000:
        raise RuntimeError("la captura salio casi vacia (%d bytes)" % os.path.getsize(IMAGEN))
    return dato, curva


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
        dato, curva = capturar()
        with open(INFO, "w", encoding="utf-8") as f:
            json.dump({"hora": ahora.strftime("%H:%M"), "fecha": ahora.strftime("%d/%m/%Y"),
                       "iso": ahora.isoformat(timespec="seconds"), "dato": dato, "ok": True,
                       "curva": curva}, f, separators=(",", ":"))
        print("[OK] Captura guardada:", IMAGEN, os.path.getsize(IMAGEN), "bytes | dato XM:", dato)
        if curva:
            print("     Curva: %d puntos de pronostico, %d de tiempo real (ultimo %s MW)"
                  % (len(curva["pron"]), len(curva["real"]), curva["real"][-1][1] if curva["real"] else "--"))
        else:
            print("     Sin datos de la curva: el tablero mostrara la imagen")
    except Exception as e:
        print("[ERROR] Captura fallida:", e)
        conservar_anterior()
        # No se detiene el flujo: el resto del tablero se publica igual
    return 0


if __name__ == "__main__":
    sys.exit(main())
