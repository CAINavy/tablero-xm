# -*- coding: utf-8 -*-
"""
Tablero del Mercado Electrico Colombiano (datos de XM)
------------------------------------------------------
Un solo archivo, sin librerias externas (solo Python 3).
Pensado para un celular Android viejo con Termux.

  python server.py               -> inicia el tablero en http://localhost:8080
  python server.py --probar      -> consulta XM una vez y muestra que funciono
  python server.py --metricas X  -> busca metricas de XM que contengan X

Paneles (en pantalla son 3):
  1. Precio de Bolsa Nacional   (PrecBolsNaci, horario, COP/kWh)
  2. Demanda del SIN            (curva de hoy en tiempo real, de captura.py, + DemaSIN de 5 dias)
  3. Generacion y embalses      (Gene por Recurso + ListadoRecursos, con la curva solar del dia;
                                 VoluUtilDiarEner / CapaUtilDiarEner, diario)
"""

import base64
import html
import json
import re
import ssl
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

PUERTO = 8080
API = "https://servapibi.xm.com.co/"
REFRESCO_SEG = 60 * 60          # XM publica 1 vez al dia; consultar cada hora sobra
TZ_CO = timezone(timedelta(hours=-5))

# Metricas de XM (si alguna cambia de nombre, usa --metricas para buscarla)
M_PRECIO = "PrecBolsNaci"
M_DEMANDA_DIA = "DemaSIN"                    # demanda de energia del SIN, diaria
M_DEMANDA_HORA = "DemaReal"                  # respaldo
M_GENERACION = "Gene"
M_VOLUMEN = "VoluUtilDiarEner"
M_CAPACIDAD = "CapaUtilDiarEner"
M_PORC_EMBALSE = "PorcVoluUtilDiar"           # respaldo si falla el calculo

# Orden fijo de tecnologias (el color sigue a la tecnologia, no a su posicion)
TECNOLOGIAS = ["Hidráulica", "Térmica", "Eólica", "Solar", "Biomasa y cogen.", "Otras"]
RENOVABLES = ("Hidráulica", "Eólica", "Solar", "Biomasa y cogen.")

_cache = {"precio": None, "demanda": None, "generacion": None, "embalses": None, "irradiancia": None,
          "errores": {}, "consultado": None}
_recursos = {"mapa": None, "fecha": 0}
_lock = threading.Lock()


# ---------------------------------------------------------------- API de XM
def consultar(tipo, metrica, entidad="Sistema", ini=None, fin=None, filtro=None):
    cuerpo = {"MetricId": metrica, "Entity": entidad, "Filter": filtro or []}
    if ini is not None:
        cuerpo["StartDate"] = ini.strftime("%Y-%m-%d")
        cuerpo["EndDate"] = fin.strftime("%Y-%m-%d")
    req = urllib.request.Request(
        API + tipo, data=json.dumps(cuerpo).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Connection": "close"},
    )
    with urllib.request.urlopen(req, timeout=90, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8"))


def num(v):
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def minus(d):
    return {str(k).lower(): v for k, v in (d or {}).items()}


def horarios(resp):
    """{codigo: [(datetime, valor), ...]} a partir de una respuesta /hourly."""
    salida = {}
    for item in resp.get("Items", []) or []:
        fecha = datetime.strptime(item["Date"][:10], "%Y-%m-%d")
        for ent in item.get("HourlyEntities", []) or []:
            v = minus(ent.get("Values"))
            codigo = str(v.get("code") or ent.get("Id") or "Sistema")
            for h in range(1, 25):
                x = num(v.get("hour%02d" % h))
                if x is not None:
                    salida.setdefault(codigo, []).append((fecha + timedelta(hours=h - 1), x))
    for c in salida:
        salida[c].sort(key=lambda t: t[0])
    return salida


def diarios(resp):
    """[(date, valor), ...] a partir de una respuesta /daily."""
    salida = []
    for item in resp.get("Items", []) or []:
        fecha = datetime.strptime(item["Date"][:10], "%Y-%m-%d").date()
        for ent in item.get("DailyEntities", []) or []:
            if "Value" in ent:
                x = num(ent.get("Value"))
            else:
                x = num(minus(ent.get("Values")).get("value"))
            if x is not None:
                salida.append((fecha, x))
    salida.sort()
    return salida


def listas(resp):
    filas = []
    for item in resp.get("Items", []) or []:
        for ent in item.get("ListEntities", []) or []:
            filas.append(ent.get("Values") or {})
    return filas


def serie_sistema(resp):
    datos = horarios(resp)
    if not datos:
        return []
    return max(datos.values(), key=len)


def quitar_dias_incompletos(serie, umbral=0.8):
    """XM publica primero datos parciales de los ultimos dias: se descartan al final de la serie
    los dias cuyo total es menor que el 80 % de la mediana de los demas."""
    serie = list(serie)
    if len(serie) < 3:
        return serie
    valores = sorted(v for _, v in serie)
    mediana = valores[len(valores) // 2]
    while serie and serie[-1][1] < umbral * mediana:
        serie.pop()
    return serie


def totales_por_dia(puntos):
    dias = {}
    for t, x in puntos:
        dias[t.date()] = dias.get(t.date(), 0.0) + x
    return sorted(dias.items())


# ---------------------------------------------------------------- Paneles
def panel_precio(hoy):
    pts = serie_sistema(consultar("hourly", M_PRECIO, "Sistema", hoy - timedelta(days=7), hoy))
    if not pts:
        raise RuntimeError("XM no devolvio precios")
    u = pts[-24:]
    v = [p for _, p in u]
    return {"horas": [t.strftime("%Y-%m-%d %H") for t, _ in u], "valores": v,
            "ultimo": v[-1], "ultima_hora": u[-1][0].strftime("%d/%m %H:00"),
            "promedio": sum(v) / len(v), "minimo": min(v), "maximo": max(v)}


def panel_demanda(hoy):
    """Demanda de energia del SIN, diaria (GWh/dia), ultimos 30 dias completos."""
    ini = hoy - timedelta(days=31)
    serie = diarios(consultar("daily", M_DEMANDA_DIA, "Sistema", ini, hoy))
    metrica = M_DEMANDA_DIA
    if not serie:   # respaldo: suma diaria de la demanda real horaria
        serie = totales_por_dia(serie_sistema(consultar("hourly", M_DEMANDA_HORA, "Sistema", ini, hoy)))
        metrica = M_DEMANDA_HORA
    serie = quitar_dias_incompletos(serie)
    if not serie:
        raise RuntimeError("XM no devolvio demanda")
    gwh = [v / 1e6 for _, v in serie]
    datos = {"fechas": [d.strftime("%Y-%m-%d") for d, _ in serie], "valores": gwh,
             "ultimo": gwh[-1], "fecha": serie[-1][0].strftime("%d/%m/%Y"),
             "promedio": sum(gwh) / len(gwh), "minimo": min(gwh), "maximo": max(gwh),
             "metrica": metrica}
    # Curva hora a hora (MW) de los ultimos 5 dias completos: pico de la noche y valle de la madrugada
    try:
        dias5 = [d for d, _ in serie[-5:]]
        total = dict(serie)
        pts = serie_sistema(consultar("hourly", M_DEMANDA_HORA, "Sistema", dias5[0], dias5[-1]))
        por_dia = {}
        for t, x in pts:
            if t.date() in total:
                por_dia.setdefault(t.date(), []).append((t, x))
        horas = []
        for d in dias5:
            p = por_dia.get(d, [])
            # solo dias con las 24 horas y que sumen lo mismo que la demanda diaria
            if len(p) >= 24 and sum(x for _, x in p) >= 0.9 * total[d]:
                horas += [[t.strftime("%Y-%m-%d %H"), round(x / 1000.0, 1)] for t, x in p]
        if horas:
            datos["horas5"] = horas
    except Exception as e:
        print("Aviso: sin curva horaria de demanda (%s)" % e)
    return datos


def clasificar(attrs):
    a = minus(attrs)
    texto = " ".join(str(a.get(k, "")) for k in ("enersource", "type", "rectype")).upper()
    if "AGUA" in texto or "HIDR" in texto:
        return "Hidráulica"
    if "SOLAR" in texto:
        return "Solar"
    if "VIENTO" in texto or "EOLIC" in texto or "EÓLIC" in texto:
        return "Eólica"
    if any(k in texto for k in ("BIOMASA", "BAGAZO", "BIOGAS", "CASCARILLA", "COGENER", "RESIDUO")):
        return "Biomasa y cogen."
    if any(k in texto for k in ("GAS", "CARB", "COMBUST", "ACPM", "JET", "DIESEL", "FUEL", "TERM", "CRUDO", "GLP")):
        return "Térmica"
    return "Otras"


def mapa_recursos(hoy):
    if _recursos["mapa"] and time.time() - _recursos["fecha"] < 24 * 3600:
        return _recursos["mapa"]
    filas = listas(consultar("lists", "ListadoRecursos", "Sistema", hoy - timedelta(days=1), hoy))
    mapa = {}
    for f in filas:
        cod = minus(f).get("code")
        if cod:
            mapa[str(cod)] = clasificar(f)
    if not mapa:
        raise RuntimeError("XM no devolvio el listado de recursos")
    _recursos.update(mapa=mapa, fecha=time.time())
    return mapa


def panel_generacion(hoy):
    mapa = mapa_recursos(hoy)
    # 1) El total del sistema (consulta liviana) dice cual es el ultimo dia completo
    dias = quitar_dias_incompletos(totales_por_dia(
        serie_sistema(consultar("hourly", M_GENERACION, "Sistema", hoy - timedelta(days=8), hoy))))
    if not dias:
        raise RuntimeError("XM no devolvio generacion")
    # 2) Solo para ese dia se descarga el detalle por planta; si XM aun no lo
    #    publica, se prueba con los dias anteriores
    por_recurso = {}
    for dia, _ in reversed(dias[-3:]):
        por_recurso = horarios(consultar("hourly", M_GENERACION, "Recurso", dia, dia))
        if por_recurso:
            break
    if not por_recurso:
        raise RuntimeError("XM no devolvio generacion por planta")
    suma = dict((t, 0.0) for t in TECNOLOGIAS)
    solar_h = [0.0] * 24          # kWh de cada hora = MW promedio de esa hora x 1000
    for cod, pts in por_recurso.items():
        tec = mapa.get(cod, "Otras")
        suma[tec] += sum(x for t, x in pts if t.date() == dia)
        if tec == "Solar":
            for t, x in pts:
                if t.date() == dia:
                    solar_h[t.hour] += x
    total = sum(suma.values())
    if total <= 0:
        raise RuntimeError("generacion en cero")
    tecs = [{"nombre": t, "gwh": suma[t] / 1e6, "pct": 100.0 * suma[t] / total}
            for t in TECNOLOGIAS if suma[t] > 0]
    return {"fecha": dia.strftime("%d/%m/%Y"), "total_gwh": total / 1e6,
            "renovable_pct": 100.0 * sum(suma[t] for t in RENOVABLES) / total,
            "tecnologias": tecs, "solar_mw": [round(x / 1000.0, 1) for x in solar_h]}


URL_IRRADIANCIA = "https://www.tutiempo.net/radiacion-solar/yopal.html"


def leer_irradiancia(texto_html, hoy):
    """Pronostico horario de irradiancia (W/m2) de hoy en la pagina de tutiempo.net.
    Cada dia aparece como "Hoy 17 de Noviembre ... 6189 wh/m2 ... 06:00 11 w/m2 ... 17:00 31 w/m2"."""
    texto = html.unescape(re.sub(r"<[^>]+>", " ", texto_html))
    texto = re.sub(r"\s+", " ", texto)
    dias = list(re.finditer(r"(Hoy|Mañana|Lunes|Martes|Miércoles|Jueves|Viernes|Sábado|Domingo)"
                            r" (\d{1,2}) de [A-Za-zÁÉÍÓÚáéíóú]+", texto))
    for i, m in enumerate(dias):
        if int(m.group(2)) != hoy.day:
            continue
        bloque = texto[m.end(): dias[i + 1].start() if i + 1 < len(dias) else len(texto)]
        horas = [0.0] * 24
        n = 0
        for h, v in re.findall(r"(\d{1,2}):00 ?(\d+(?:[.,]\d+)?) ?w/m ?2", bloque, re.I):
            if 0 <= int(h) < 24:
                horas[int(h)] = float(v.replace(",", "."))
                n += 1
        total = re.search(r"(\d+(?:[.,]\d+)?) ?wh/m ?2", bloque, re.I)
        sol = re.search(r"(\d{1,2}:\d{2}) ?(\d{1,2}:\d{2})", bloque)
        if n >= 6:
            return {"fecha": hoy.strftime("%d/%m/%Y"), "w_m2": horas,
                    "total_wh_m2": float(total.group(1).replace(",", ".")) if total else sum(horas),
                    "salida": sol.group(1) if sol else None, "puesta": sol.group(2) if sol else None}
    return None


def panel_irradiancia(hoy):
    req = urllib.request.Request(URL_IRRADIANCIA, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36",
        "Accept-Language": "es-CO,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
        datos = leer_irradiancia(r.read().decode("utf-8", "replace"), hoy)
    if not datos:
        raise RuntimeError("tutiempo.net no trajo la irradiancia de hoy")
    return datos


def diarios_por_tramos(metrica, ini, fin, entidad="Sistema"):
    """La API de XM entrega maximo 31 dias por consulta: se pide por tramos de 30 dias."""
    salida = []
    while ini <= fin:
        tope = min(ini + timedelta(days=29), fin)
        salida += diarios(consultar("daily", metrica, entidad, ini, tope))
        ini = tope + timedelta(days=1)
    return sorted(dict(salida).items())


def panel_embalses(hoy):
    ini = hoy - timedelta(days=40)
    serie = []
    try:
        vol = dict(diarios_por_tramos(M_VOLUMEN, ini, hoy))
        cap = dict(diarios_por_tramos(M_CAPACIDAD, ini, hoy))
        serie = [(d, 100.0 * vol[d] / cap[d]) for d in sorted(vol) if cap.get(d)]
    except Exception:
        serie = []
    if not serie:   # respaldo: porcentaje directo
        serie = diarios_por_tramos(M_PORC_EMBALSE, ini, hoy)
        if serie and max(v for _, v in serie) <= 1.5:
            serie = [(d, v * 100.0) for d, v in serie]
    if not serie:
        raise RuntimeError("XM no devolvio datos de embalses")
    completa = serie
    serie = [(d, x) for d, x in serie if d > serie[-1][0] - timedelta(days=31)]   # grafica: 30 dias
    v = [x for _, x in serie]

    def hace(n):
        objetivo = completa[-1][0] - timedelta(days=n)
        prev = [x for d, x in completa if d <= objetivo]
        return prev[-1] if prev else None

    return {"fechas": [d.strftime("%Y-%m-%d") for d, _ in serie], "valores": v,
            "ultimo": v[-1], "fecha": serie[-1][0].strftime("%d/%m/%Y"),
            "hace7": hace(7), "hace30": hace(30)}


PANELES = [("precio", panel_precio), ("demanda", panel_demanda),
           ("generacion", panel_generacion), ("embalses", panel_embalses),
           ("irradiancia", panel_irradiancia)]


def actualizar(verboso=False):
    hoy = datetime.now(TZ_CO).date()
    for nombre, funcion in PANELES:
        try:
            datos = funcion(hoy)
            with _lock:
                _cache[nombre] = datos
                _cache["errores"].pop(nombre, None)
            print("[OK]    %-10s" % nombre, resumen(nombre, datos) if verboso else "")
        except Exception as e:     # un panel que falla no tumba a los demas
            with _lock:
                _cache["errores"][nombre] = str(e)
            print("[ERROR] %-10s %s" % (nombre, e))
    with _lock:
        _cache["consultado"] = datetime.now(TZ_CO).strftime("%d/%m %H:%M")


def resumen(nombre, d):
    if nombre == "precio":
        return "%.1f COP/kWh (%s)" % (d["ultimo"], d["ultima_hora"])
    if nombre == "demanda":
        return "%.1f GWh (%s) [%s]" % (d["ultimo"], d["fecha"], d["metrica"])
    if nombre == "generacion":
        return "%.1f GWh %s | " % (d["total_gwh"], d["fecha"]) + ", ".join(
            "%s %.0f%%" % (t["nombre"], t["pct"]) for t in d["tecnologias"])
    if nombre == "embalses":
        return "%.1f %% (%s)" % (d["ultimo"], d["fecha"])
    if nombre == "irradiancia":
        return "%.0f Wh/m2 en Yopal (%s), maximo %.0f W/m2" % (d["total_wh_m2"], d["fecha"], max(d["w_m2"]))
    return ""


def bucle_actualizacion():
    while True:
        actualizar()
        time.sleep(REFRESCO_SEG)


# ---------------------------------------------------------------- Pagina web
ICONO_192 = 'iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAACv0lEQVR42u3dTW7CUAyF0eSJaQaZEFbXxXV1VVfRFSDxV3j2Pd+0M3RkWyXAuu3HIj3a8BIIIAEkgASQBJAAEkACSAJIAAkgASQBpBd38hK8sJ/v32t/unydTSA9qMcK01N1HT8AGT8A0QMQPQAp8QACyPgBiB6A6AFIiQcQQMYPQPQA1FVP+/0FkACyvACiByB6Eg8ggASQ5QVQPz0h+wsgswcgAWT8AERP4gEEkNkDkAAyfgCiJ/EAAsjsAUgAtRk/afsLIMsLIHoAEkDGT+IBlA7I8gKIHoAWn4EHyPgxgegRQPQA5AACyPgBiB4BRA9ADiCAjB+A6FEmoP/WE76/3EACyPICiB6AEvU4gNxAAsjyAqiiHvurISCzByABZPwARI8DKAiQ2QOQADJ+AKLHAXRv67Yf9CyeqXUD0QOQ8VNvmQ56lAWInqlueTcQPUmAjJ/Z/o8w6FEEIHrm/DemG4ieAEDGz7RvoQx61BkQPZO/g+sGoqcvIONn/qdHBj1qCIieKg+vuYHoaQfI+Cn04OygR30A0VPuuX03ED1dABk/FT8zNOhReUD01P3IohuInuKAjJ/SH5ce9NDzTCevHdBuIOMHoMjx0+CbYgAyewASQMYPQEr8kjyA/MgGQALI8gJIid8PDBA9AHkLDCDjByAl/jQCQPQAJIBaXtAJv+sDkOUFED0A0QOQHNEuaOMHIMsLIHoAEkAyfgB6xwWd/EvyJhA9AAkg4wcgegByQdMDkACyvACiByAHED0ACSDLCyB6AKJHAPkqBYCMH4DoAYgeAUQPQC5ogIwfgOgBSPQAJIA+e0EbPwBZXgDRA5AAMn4E0A0XND0AmT0ACSDjByB6BNANFzQ9AAkgywsgegCiRwB5Dhog4wcgeuJat/3wKsgEEkACSABJAAkgASSAJIAEkAASQBJAAkjT9AfT4QYcC0aVHAAAAABJRU5ErkJggg=='
ICONO_512 = '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'

MANIFEST = json.dumps({
    "name": "Tablero Energía XM", "short_name": "Energía XM",
    "start_url": "/", "scope": "/", "display": "fullscreen", "orientation": "landscape",
    "background_color": "#0d1117", "theme_color": "#0d1117",
    "icons": [{"src": "/icono-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icono-512.png", "sizes": "512x512", "type": "image/png"}],
})
SERVICE_WORKER = "self.addEventListener('fetch', function (e) {});"

PAGINA = r"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0d1117">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icono-192.png">
<title>Energía XM</title>
<style>
  html,body{margin:0;height:100%;background:#0d1117;color:#e6edf3;
    font-family:Roboto,Arial,sans-serif;overflow:hidden;-webkit-user-select:none;user-select:none}
  #app{display:flex;flex-direction:column;height:100%;box-sizing:border-box}
  #main{flex:1;display:flex;flex-direction:column;padding:2.5vmin 3vmin 1vmin;min-height:0}
  .top{display:flex;justify-content:space-between;align-items:baseline}
  #tit{font-size:4vmin;color:#c9d1d9;letter-spacing:.06em;text-transform:uppercase;font-weight:700}
  #meta{font-size:2.8vmin;color:#6e7681}
  #extra{margin-left:auto;text-align:right;color:#f0642a;display:none}
  #extra b{font-size:7vmin;font-weight:700;line-height:1}
  #extra i{font-style:normal;font-size:3.6vmin;margin-left:1vmin}
  #extra small{display:block;font-size:2.8vmin;color:#c98a6e;margin-top:.4vmin}
  .fila{display:flex;align-items:baseline;flex-wrap:wrap;margin-top:.5vmin}
  #valor{font-size:15vmin;font-weight:700;line-height:1;color:#f5b301;margin-right:2vmin}
  #uni{font-size:5vmin;color:#c9d1d9}
  #sub{font-size:3.4vmin;color:#8b949e;margin-top:.6vmin}
  .stats{display:flex;margin:1.4vmin 0 .6vmin}
  .st{flex:1;font-size:3.2vmin;color:#8b949e}
  .st b{display:block;font-size:4.6vmin;color:#e6edf3}
  #cont{flex:1;position:relative;min-height:0}
  canvas{position:absolute;left:0;top:0;width:100%;height:100%}
  #img{position:absolute;left:0;top:0;width:100%;height:100%;object-fit:contain;display:none;
    filter:invert(.92) hue-rotate(180deg) saturate(1.3);border-radius:1vmin}
  #nota{position:absolute;left:0;right:0;top:40%;text-align:center;color:#8b949e;font-size:3.4vmin;display:none}
  #barra{display:flex;border-top:1px solid #21262d}
  .tab{flex:1;padding:1.2vmin 1vmin 1.4vmin;text-align:center;border-top:.7vmin solid transparent;
    color:#6e7681;font-size:2.8vmin}
  .tab b{display:block;font-size:4.2vmin;color:#8b949e;white-space:nowrap}
  .tab.on{border-top-color:#f5b301;background:#161b22;color:#c9d1d9}
  .tab.on b{color:#e6edf3}
  #aviso{position:fixed;right:2vmin;top:2vmin;font-size:2.8vmin;color:#0d1117;background:#f5b301;
    padding:.6vmin 1.4vmin;border-radius:1vmin}
</style></head>
<body><div id="app">
  <div id="main">
    <div class="top"><span id="tit">Cargando</span><span id="meta"></span></div>
    <div class="fila" id="fila"><span id="valor">--</span><span id="uni"></span><span id="extra"></span></div>
    <div id="sub">Consultando XM...</div>
    <div class="stats" id="stats">
      <div class="st"><span id="l1"></span><b id="s1">--</b></div>
      <div class="st"><span id="l2"></span><b id="s2">--</b></div>
      <div class="st"><span id="l3"></span><b id="s3">--</b></div>
    </div>
    <div id="cont"><canvas id="g"></canvas><img id="img" alt="Grafica de demanda en tiempo real de XM"><div id="nota"></div></div>
  </div>
  <div id="barra">
    <div class="tab" data-i="0">Precio $/kWh<b id="t0">--</b></div>
    <div class="tab" data-i="1">Demanda MW<b id="t1">--</b></div>
    <div class="tab" data-i="2">Solar &middot; Embalses<b id="t2">--</b></div>
  </div>
</div>
<div id="aviso">Toca para pantalla completa</div>
<script>
var D = {}, pagina = 0, pausaHasta = 0, ROTAR_SEG = 12;
var PAGS = ['precio','demanda','generacion'];
var mP = location.search.match(/p=(\d)/); if(mP) pagina = parseInt(mP[1],10) % PAGS.length;
var COLORES = {'Hidráulica':'#3987e5','Térmica':'#d95926','Eólica':'#199e70','Solar':'#f5b301',
               'Biomasa y cogen.':'#d55181','Otras':'#6e7681'};
var C_REAL = '#f5b301', C_PRON = '#3987e5', C_EMB = '#199e70';

function $(id){ return document.getElementById(id); }
function f(n, dec){
  if(n === null || n === undefined || isNaN(n)) return '--';
  try{ return n.toLocaleString('es-CO',{minimumFractionDigits:dec,maximumFractionDigits:dec}); }
  catch(e){ return n.toFixed(dec); }
}
function fs(k){ return Math.max(10, Math.round(Math.min(window.innerWidth, window.innerHeight)*k)); }

// ---------- graficas ----------
function lienzo(){
  var c = $('g'), box = $('cont'), r = window.devicePixelRatio || 1;
  var W = box.clientWidth, H = box.clientHeight;
  c.width = W*r; c.height = H*r;
  var x = c.getContext('2d'); x.setTransform(r,0,0,r,0,0); x.clearRect(0,0,W,H);
  return {x:x, W:W, H:H};
}
function rect(x, x0, y0, w, h, r){
  r = Math.max(0, Math.min(r, w/2, h/2));
  x.beginPath(); x.moveTo(x0, y0+h); x.lineTo(x0, y0+r); x.arcTo(x0, y0, x0+r, y0, r);
  x.lineTo(x0+w-r, y0); x.arcTo(x0+w, y0, x0+w, y0+r, r); x.lineTo(x0+w, y0+h); x.closePath();
}
// Escala con valores "redondos" para el eje Y
function escala(mn, mx, n){
  var paso = (mx-mn)/n || 1, p10 = Math.pow(10, Math.floor(Math.log(paso)/Math.LN10)), m = paso/p10;
  paso = (m<=1?1:m<=2?2:m<=2.5?2.5:m<=5?5:10)*p10;
  return {mn: Math.floor(mn/paso)*paso, mx: Math.ceil(mx/paso)*paso, paso: paso};
}

// Linea con area dentro de un recuadro (x0,y0,W,H) del lienzo
function lineaEn(x, x0, y0, W, H, vals, etiqueta, cadaN, color, dec, t){
  var n = vals.length; if(!n || H < 30) return;
  var L = t*3.8, R = t*0.8, T = t*0.8, B = t*2;
  var mn = Math.min.apply(null,vals), mx = Math.max.apply(null,vals);
  var pad = (mx-mn)*0.15 || Math.abs(mx)*0.05 || 1; mn = mn-pad; mx = mx+pad;
  function X(i){ return x0 + L + (W-L-R) * (n>1 ? i/(n-1) : 0.5); }
  function Y(v){ return y0 + T + (H-T-B) * (1-(v-mn)/(mx-mn)); }
  x.font = t+'px Roboto,Arial'; x.textBaseline = 'middle';
  for(var k=0;k<=3;k++){
    var v = mn + (mx-mn)*k/3, y = Y(v);
    x.strokeStyle='#21262d'; x.lineWidth=1; x.beginPath(); x.moveTo(x0+L,y); x.lineTo(x0+W-R,y); x.stroke();
    x.fillStyle='#8b949e'; x.textAlign='right'; x.fillText(f(v,dec), x0+L-t*0.4, y);
  }
  x.textBaseline='top';
  for(var i=0;i<n;i++){
    if(i%cadaN===0 || (i===n-1 && (n-1)%cadaN >= cadaN/2)){
      x.textAlign = (i===n-1 && i>0) ? 'right' : (i===0 ? 'left' : 'center');
      x.fillText(etiqueta(i), i===n-1 && i>0 ? x0+W : X(i), y0+H-B+t*0.5);
    }
  }
  var g = x.createLinearGradient(0,y0+T,0,y0+H-B);
  g.addColorStop(0, color+'55'); g.addColorStop(1, color+'00');
  x.beginPath(); x.moveTo(X(0),y0+H-B);
  for(i=0;i<n;i++) x.lineTo(X(i),Y(vals[i]));
  x.lineTo(X(n-1),y0+H-B); x.closePath(); x.fillStyle=g; x.fill();
  x.beginPath(); for(i=0;i<n;i++){ if(i) x.lineTo(X(i),Y(vals[i])); else x.moveTo(X(i),Y(vals[i])); }
  x.strokeStyle=color; x.lineWidth=Math.max(2,t*0.16); x.lineJoin='round'; x.stroke();
  x.beginPath(); x.arc(X(n-1),Y(vals[n-1]),t*0.32,0,6.3); x.fillStyle=color; x.fill();
}
function linea(vals, etiqueta, cadaN, color, dec){
  var L = lienzo(); lineaEn(L.x, 0, 0, L.W, L.H, vals, etiqueta, cadaN, color, dec, fs(0.032));
}

// Precio (arriba) e irradiancia de Yopal (abajo): mismas horas, misma altura, mismo eje X
var C_IRR = '#f0642a';
function precioEIrradiancia(d, ir){
  var L = lienzo(), x = L.x, W = L.W, H = L.H, t = fs(0.03);
  if(!ir){ lineaEn(x, 0, 0, W, H, d.valores, function(i){ return hora(d.horas[i]); }, 4, '#f5b301', 0, t); return; }
  // Precio (eje izquierdo, amarillo) e irradiancia (eje derecho, naranja rojizo) sobrepuestos, por hora del dia
  var horas = d.horas.map(function(h){ return parseInt(h.substr(11,2),10); });
  var pv = d.valores, iv = horas.map(function(h){ return ir.w_m2[h] || 0; }), n = pv.length;
  var pmn = Math.min.apply(null,pv), pmx = Math.max.apply(null,pv), pad = (pmx-pmn)*0.15 || pmx*0.05 || 1;
  pmn -= pad; pmx += pad;
  var imx = escala(0, Math.max.apply(null, iv)*1.05 || 1, 3).mx;
  x.font = t+'px Roboto,Arial';
  var Lm = x.measureText(f(pmx,0)).width + t*0.8, R = x.measureText(f(imx,0)).width + t*0.8, T = t*2.2, B = t*2;
  function X(i){ return Lm + (W-Lm-R) * (n>1 ? i/(n-1) : 0.5); }
  function YP(v){ return T + (H-T-B) * (1-(v-pmn)/(pmx-pmn)); }
  function YI(v){ return T + (H-T-B) * (1 - v/imx); }
  // leyenda
  x.textBaseline='middle'; x.textAlign='left';
  x.strokeStyle='#f5b301'; x.lineWidth=Math.max(3,t*0.2); x.beginPath(); x.moveTo(Lm, t*0.7); x.lineTo(Lm+t*1.1, t*0.7); x.stroke();
  x.fillStyle='#f5b301'; x.fillText('Precio COP/kWh', Lm+t*1.4, t*0.7);
  var lx = Lm + t*1.4 + x.measureText('Precio COP/kWh').width + t*1.4;
  x.fillStyle=C_IRR; x.fillRect(lx, t*0.4, t*0.9, t*0.6);
  x.fillText('Irradiancia Yopal, pronóstico hoy (W/m²)', lx+t*1.2, t*0.7);
  // rejilla con 4 lineas compartidas: precio a la izquierda, irradiancia a la derecha
  for(var k=0;k<=3;k++){
    var y = T + (H-T-B)*(1-k/3);
    x.strokeStyle='#21262d'; x.lineWidth=1; x.beginPath(); x.moveTo(Lm,y); x.lineTo(W-R,y); x.stroke();
    x.textAlign='right'; x.fillStyle='#d9a400'; x.fillText(f(pmn+(pmx-pmn)*k/3,0), Lm-t*0.4, y);
    x.textAlign='left'; x.fillStyle=C_IRR; x.fillText(f(imx*k/3,0), W-R+t*0.4, y);
  }
  x.textBaseline='top'; x.fillStyle='#8b949e';
  for(var i=0;i<n;i++){
    if(i%4===0 || (i===n-1 && (n-1)%4 >= 2)){
      x.textAlign = i===n-1 ? 'right' : (i===0 ? 'left' : 'center');
      x.fillText(hora(d.horas[i]), X(i), H-B+t*0.5);
    }
  }
  // irradiancia: area + linea
  var g = x.createLinearGradient(0, T, 0, H-B);
  g.addColorStop(0, C_IRR+'55'); g.addColorStop(1, C_IRR+'00');
  x.beginPath(); x.moveTo(X(0), YI(0));
  for(i=0;i<n;i++) x.lineTo(X(i), YI(iv[i]));
  x.lineTo(X(n-1), YI(0)); x.closePath(); x.fillStyle=g; x.fill();
  x.beginPath(); for(i=0;i<n;i++){ if(i) x.lineTo(X(i),YI(iv[i])); else x.moveTo(X(i),YI(iv[i])); }
  x.strokeStyle=C_IRR; x.lineWidth=Math.max(2,t*0.14); x.lineJoin='round'; x.stroke();
  // precio encima, con borde oscuro para que se distinga al cruzarse
  x.beginPath(); for(i=0;i<n;i++){ if(i) x.lineTo(X(i),YP(pv[i])); else x.moveTo(X(i),YP(pv[i])); }
  x.lineJoin='round'; x.strokeStyle='#0d1117'; x.lineWidth=Math.max(5,t*0.36); x.stroke();
  x.strokeStyle='#f5b301'; x.lineWidth=Math.max(2.5,t*0.18); x.stroke();
  x.beginPath(); x.arc(X(n-1),YP(pv[n-1]),t*0.32,0,6.3); x.fillStyle='#f5b301'; x.fill();
  // pico de irradiancia
  var mx = Math.max.apply(null, iv), ip = iv.indexOf(mx);
  if(mx > 0){
    x.beginPath(); x.arc(X(ip), YI(mx), t*0.28, 0, 6.3); x.fillStyle=C_IRR; x.fill();
    x.font='bold '+(t*0.9)+'px Roboto,Arial'; x.textBaseline='bottom';
    x.textAlign = X(ip) > W*0.6 ? 'right' : 'left';
    x.fillText(f(mx,0)+' W/m²', X(ip) + (x.textAlign==='left' ? t*0.5 : -t*0.5), YI(mx)-t*0.2);
  }
}

// Curva del dia: pronostico (area azul escalonada) y tiempo real (linea naranja), en MW
function curvaDia(x, x0, y0, W, H, cv, t){
  var pr = cv.pron || [], re = cv.real || [], todos = pr.concat(re);
  if(!todos.length || H < 40) return;
  var mn = 1e12, mx = -1e12;
  for(var i=0;i<todos.length;i++){ mn = Math.min(mn, todos[i][1]); mx = Math.max(mx, todos[i][1]); }
  var e = escala(mn - (mx-mn)*0.05, mx + (mx-mn)*0.05, 4);
  x.font = t+'px Roboto,Arial';
  var L = x.measureText(f(e.mx,0)).width + t*0.8, R = t*0.6, T = t*2.2, B = t*1.9;
  function X(m){ return x0 + L + (W-L-R) * m/1440; }
  function Y(v){ return y0 + T + (H-T-B) * (1-(v-e.mn)/(e.mx-e.mn)); }
  // leyenda
  x.textBaseline='middle'; x.textAlign='left';
  var ly = y0 + t*0.7, lx = x0 + L;
  x.fillStyle=C_PRON; x.fillRect(lx, ly-t*0.3, t*0.9, t*0.6);
  x.fillStyle='#c9d1d9'; x.fillText('Pronóstico', lx+t*1.2, ly);
  lx += t*1.2 + x.measureText('Pronóstico').width + t*1.2;
  x.strokeStyle=C_REAL; x.lineWidth=Math.max(3,t*0.22); x.beginPath(); x.moveTo(lx, ly); x.lineTo(lx+t*1.1, ly); x.stroke();
  x.fillStyle='#c9d1d9'; x.fillText('Tiempo real', lx+t*1.4, ly);
  x.textAlign='right'; x.fillStyle='#8b949e'; x.fillText('MW', x0+W-R, ly);
  // rejilla y ejes
  for(var v=e.mn; v<=e.mx+e.paso/2; v+=e.paso){
    var y = Y(v);
    x.strokeStyle='#21262d'; x.lineWidth=1; x.beginPath(); x.moveTo(x0+L,y); x.lineTo(x0+W-R,y); x.stroke();
    x.fillStyle='#8b949e'; x.textAlign='right'; x.fillText(f(v,0), x0+L-t*0.4, y);
  }
  x.textBaseline='top';
  var cada = (W-L-R) < t*22 ? 6 : 3;
  for(var h=0; h<=24; h+=cada){
    x.textAlign = h===0 ? 'left' : (h===24 ? 'right' : 'center');
    x.fillStyle='#8b949e'; x.fillText(h+'h', X(h*60), y0+H-B+t*0.5);
  }
  // pronostico: area + linea
  if(pr.length){
    x.beginPath(); x.moveTo(X(pr[0][0]), Y(e.mn));
    for(i=0;i<pr.length;i++) x.lineTo(X(pr[i][0]), Y(pr[i][1]));
    x.lineTo(X(pr[pr.length-1][0]), Y(e.mn)); x.closePath();
    x.fillStyle = C_PRON+'40'; x.fill();
    x.beginPath(); for(i=0;i<pr.length;i++){ if(i) x.lineTo(X(pr[i][0]),Y(pr[i][1])); else x.moveTo(X(pr[i][0]),Y(pr[i][1])); }
    x.strokeStyle=C_PRON; x.lineWidth=Math.max(1.5,t*0.12); x.lineJoin='round'; x.stroke();
  }
  // tiempo real: linea gruesa con borde oscuro para que resalte sobre el area
  if(re.length){
    x.beginPath(); for(i=0;i<re.length;i++){ if(i) x.lineTo(X(re[i][0]),Y(re[i][1])); else x.moveTo(X(re[i][0]),Y(re[i][1])); }
    x.lineJoin='round'; x.lineCap='round';
    x.strokeStyle='#0d1117'; x.lineWidth=Math.max(5,t*0.42); x.stroke();
    x.strokeStyle=C_REAL; x.lineWidth=Math.max(3,t*0.24); x.stroke();
    var u = re[re.length-1], ux = X(u[0]), uy = Y(u[1]);
    x.beginPath(); x.arc(ux, uy, t*0.38, 0, 6.3); x.fillStyle=C_REAL; x.fill();
    x.lineWidth=2; x.strokeStyle='#0d1117'; x.stroke();
    // valor actual junto al punto
    var txt = f(u[1],0)+' MW'; x.font='bold '+t+'px Roboto,Arial';
    var tw = x.measureText(txt).width, tx = ux + t*0.7, ty = uy - t*1.5;
    if(tx + tw > x0+W-R) tx = ux - t*0.7 - tw;
    if(ty < y0+T) ty = uy + t*0.6;
    x.fillStyle='rgba(13,17,23,.85)'; rect(x, tx-t*0.3, ty-t*0.15, tw+t*0.6, t*1.3, t*0.3); x.fill();
    x.fillStyle='#e6edf3'; x.textAlign='left'; x.textBaseline='top'; x.fillText(txt, tx, ty);
  }
  return {L: L, R: R};
}

// Trazo suave entre puntos (curva monotona: no inventa picos entre un dia y otro)
function curvaSuave(x, px, py){
  var n = px.length, m = [], d = [];
  for(var i=0;i<n-1;i++) d.push((py[i+1]-py[i])/(px[i+1]-px[i]));
  for(i=0;i<n;i++){
    if(i===0) m.push(d[0]||0); else if(i===n-1) m.push(d[n-2]||0);
    else m.push(d[i-1]*d[i] <= 0 ? 0 : 2/(1/d[i-1]+1/d[i]));
  }
  for(i=0;i<n-1;i++){
    var h = (px[i+1]-px[i])/3;
    x.bezierCurveTo(px[i]+h, py[i]+m[i]*h, px[i+1]-h, py[i+1]-m[i+1]*h, px[i+1], py[i+1]);
  }
}

// Linea pequena: energia de los ultimos dias (un punto por dia, con su valor)
// Curva hora a hora de los ultimos dias (MW), eje desde cero, con separacion por dia
function curvaDias(x, x0, y0, W, H, horas, fechas, gwh, color, t, marco){
  var n = horas.length; if(n < 24 || H < t*4) return;
  var vals = horas.map(function(h){ return h[1]; });
  var e = escala(0, Math.max.apply(null, vals)*1.12, 3), mx = e.mx;
  x.font = t+'px Roboto,Arial';
  var L = marco ? marco.L : x.measureText(f(mx,0)).width + t*0.8, R = marco ? marco.R : t*0.6, T = t*2.4, B = t*1.9;
  function X(i){ return x0 + L + (W-L-R) * (i+0.5)/n; }
  function Y(v){ return y0 + T + (H-T-B) * (1 - v/mx); }
  x.textBaseline='middle'; x.textAlign='left'; x.fillStyle='#c9d1d9';
  x.fillText('Demanda hora a hora (MW) · últimos días publicados', x0+L, y0+t*0.7);
  for(var v=0; v<=mx+e.paso/2; v+=e.paso){
    var y = Y(v);
    x.strokeStyle='#21262d'; x.lineWidth=1; x.beginPath(); x.moveTo(x0+L,y); x.lineTo(x0+W-R,y); x.stroke();
    x.fillStyle='#8b949e'; x.textAlign='right'; x.fillText(f(v,0), x0+L-t*0.4, y);
  }
  // dias: separadores a medianoche, fecha y energia del dia debajo, pico marcado
  var ini = 0;
  for(var i=1;i<=n;i++){
    if(i===n || horas[i][0].substr(0,10) !== horas[ini][0].substr(0,10)){
      var fe = horas[ini][0].substr(0,10), xa = x0 + L + (W-L-R)*ini/n, xb = x0 + L + (W-L-R)*i/n;
      if(ini>0){ x.strokeStyle='#30363d'; x.lineWidth=1; x.setLineDash([t*0.3,t*0.3]);
        x.beginPath(); x.moveTo(xa, y0+T); x.lineTo(xa, y0+H-B); x.stroke(); x.setLineDash([]); }
      var k = fechas.indexOf(fe), txt = dia(fe) + (k>=0 ? ' · '+f(gwh[k],0)+' GWh' : '');
      x.fillStyle='#8b949e'; x.textAlign='center'; x.textBaseline='top'; x.font=t+'px Roboto,Arial';
      x.fillText(txt, (xa+xb)/2, y0+H-B+t*0.5);
      var ip = ini; for(var j=ini;j<i;j++) if(vals[j] > vals[ip]) ip = j;
      x.beginPath(); x.arc(X(ip), Y(vals[ip]), t*0.25, 0, 6.3); x.fillStyle=color; x.fill();
      x.fillStyle='#e6edf3'; x.textBaseline='bottom'; x.font=(t*0.9)+'px Roboto,Arial';
      x.fillText(f(vals[ip],0), X(ip), Y(vals[ip])-t*0.25);
      ini = i;
    }
  }
  var g = x.createLinearGradient(0, y0+T, 0, y0+H-B);
  g.addColorStop(0, color+'44'); g.addColorStop(1, color+'00');
  x.beginPath(); x.moveTo(X(0), Y(0));
  for(i=0;i<n;i++) x.lineTo(X(i), Y(vals[i]));
  x.lineTo(X(n-1), Y(0)); x.closePath(); x.fillStyle=g; x.fill();
  x.beginPath(); for(i=0;i<n;i++){ if(i) x.lineTo(X(i),Y(vals[i])); else x.moveTo(X(i),Y(vals[i])); }
  x.strokeStyle=color; x.lineWidth=Math.max(2,t*0.16); x.lineJoin='round'; x.stroke();
}

function lineaDias(x, x0, y0, W, H, fechas, vals, color, t, marco){
  var n = vals.length; if(!n || H < t*4) return;
  x.font = t+'px Roboto,Arial';
  // eje Y desde cero, con marcas redondas
  var e = escala(0, Math.max.apply(null, vals)*1.12 || 1, 3), mn = 0, mx = e.mx;
  // mismos margenes que la curva de hoy, para que las dos graficas queden alineadas
  var L = marco ? marco.L : x.measureText(f(mx,0)).width + t*0.8, R = marco ? marco.R : t*0.6, T = t*2.6, B = t*1.9;
  var ip = t*1.6;   // margen interno para que las etiquetas de los extremos no choquen con el eje
  function X(i){ return x0 + L + ip + (W-L-R-2*ip) * (n>1 ? i/(n-1) : 0.5); }
  function Y(v){ return y0 + T + (H-T-B) * (1-(v-mn)/(mx-mn)); }
  x.textBaseline='middle'; x.textAlign='left'; x.fillStyle='#c9d1d9';
  x.fillText('Demanda por día (GWh) · últimos '+n+' días publicados', x0+L, y0+t*0.7);
  for(var v=0; v<=mx+e.paso/2; v+=e.paso){
    var y = Y(v);
    x.strokeStyle='#21262d'; x.lineWidth=1; x.beginPath(); x.moveTo(x0+L,y); x.lineTo(x0+W-R,y); x.stroke();
    x.fillStyle='#8b949e'; x.textAlign='right'; x.fillText(f(v,0), x0+L-t*0.4, y);
  }
  var g = x.createLinearGradient(0, y0+T, 0, y0+H-B);
  g.addColorStop(0, color+'44'); g.addColorStop(1, color+'00');
  var px = [], py = [];
  for(var i=0;i<n;i++){ px.push(X(i)); py.push(Y(vals[i])); }
  x.beginPath(); x.moveTo(px[0], y0+H-B); x.lineTo(px[0], py[0]); curvaSuave(x, px, py);
  x.lineTo(px[n-1], y0+H-B); x.closePath(); x.fillStyle=g; x.fill();
  x.beginPath(); x.moveTo(px[0], py[0]); curvaSuave(x, px, py);
  x.strokeStyle=color; x.lineWidth=Math.max(2,t*0.16); x.lineJoin='round'; x.stroke();
  for(i=0;i<n;i++){
    var px = X(i), py = Y(vals[i]), ult = (i===n-1);
    x.beginPath(); x.arc(px, py, t*0.28, 0, 6.3); x.fillStyle=color; x.fill();
    x.font=(ult?'bold ':'')+t+'px Roboto,Arial'; x.fillStyle='#e6edf3'; x.textAlign='center'; x.textBaseline='bottom';
    x.fillText(f(vals[i],0), px, py-t*0.35);
    x.font=t+'px Roboto,Arial'; x.fillStyle='#8b949e'; x.textBaseline='top';
    x.fillText(dia(fechas[i]), px, y0+H-B+t*0.5);
  }
}

// Barras verticales desde cero: energia de los ultimos dias
function barrasDias(x, x0, y0, W, H, fechas, vals, color, t){
  var n = vals.length; if(!n || H < 40) return;
  var T = t*2.2, B = t*1.9, max = Math.max.apply(null, vals);
  x.font = t+'px Roboto,Arial'; x.textBaseline='middle'; x.textAlign='left';
  x.fillStyle='#c9d1d9'; x.fillText('Demanda por día', x0, y0+t*0.7);
  x.textAlign='right'; x.fillStyle='#8b949e'; x.fillText('GWh', x0+W, y0+t*0.7);
  var paso = W/n, ancho = Math.min(paso*0.62, t*5), base = y0+H-B, alto = H-T-B-t*1.3;
  x.strokeStyle='#30363d'; x.lineWidth=1; x.beginPath(); x.moveTo(x0, base+0.5); x.lineTo(x0+W, base+0.5); x.stroke();
  for(var i=0;i<n;i++){
    var cx = x0 + paso*(i+0.5), h = Math.max(2, alto*vals[i]/max), ult = (i===n-1);
    x.fillStyle = ult ? color : color+'99';
    rect(x, cx-ancho/2, base-h, ancho, h, Math.min(4, ancho/4)); x.fill();
    x.textAlign='center'; x.textBaseline='bottom';
    x.font=(ult?'bold ':'')+t+'px Roboto,Arial'; x.fillStyle='#e6edf3';
    x.fillText(f(vals[i],0), cx, base-h-t*0.25);
    x.textBaseline='top'; x.font=t+'px Roboto,Arial'; x.fillStyle='#8b949e';
    x.fillText(dia(fechas[i]), cx, base+t*0.5);
  }
}

// Barras horizontales por tecnologia; la solar va primero y resaltada
function barrasTec(x, x0, y0, W, H, tecs, t){
  var n = tecs.length; if(!n || H < 30) return;
  var fila = Math.min(H/n, t*2.5), alto = Math.max(6, fila*0.52);
  x.textBaseline='middle';
  x.font='bold '+t+'px Roboto,Arial';
  var Lnom = 0; for(var i=0;i<n;i++) Lnom = Math.max(Lnom, x.measureText(tecs[i].nombre).width);
  Lnom += t*0.8;
  var wPct = x.measureText('100,0 %').width + t*0.5;
  x.font = (t*0.8)+'px Roboto,Arial'; var wGwh = x.measureText('000,0 GWh').width;
  var ancho = Math.max(10, W - Lnom - t*0.5 - wPct - wGwh);
  var max = 0; for(i=0;i<n;i++) max = Math.max(max, tecs[i].pct);
  for(i=0;i<n;i++){
    var s = tecs[i].nombre === 'Solar', y = y0 + fila*i + fila/2;
    var w = Math.max(3, ancho * tecs[i].pct / max), c = COLORES[tecs[i].nombre] || '#6e7681';
    if(s){ x.fillStyle='#f5b30118'; rect(x, x0-t*0.3, y-fila/2+1, W+t*0.3, fila-2, t*0.3); x.fill(); }
    x.font = (s?'bold ':'')+t+'px Roboto,Arial'; x.textAlign='left';
    x.fillStyle = s ? '#f5b301' : '#c9d1d9'; x.fillText(tecs[i].nombre, x0, y);
    x.fillStyle = c; x.beginPath();
    var r = Math.min(4, alto/2, w/2), bx = x0+Lnom, by = y-alto/2;
    x.moveTo(bx,by); x.lineTo(bx+w-r,by); x.arcTo(bx+w,by,bx+w,by+r,r);
    x.lineTo(bx+w,by+alto-r); x.arcTo(bx+w,by+alto,bx+w-r,by+alto,r); x.lineTo(bx,by+alto); x.closePath(); x.fill();
    x.fillStyle = '#e6edf3'; x.font = 'bold '+t+'px Roboto,Arial';
    x.fillText(f(tecs[i].pct,1)+' %', bx+w+t*0.5, y);
    x.fillStyle = '#8b949e'; x.font = (t*0.8)+'px Roboto,Arial';
    x.fillText(f(tecs[i].gwh,1)+' GWh', bx+w+t*0.5+wPct, y);
  }
}

// Curva solar del dia (MW hora a hora): la "campana" de la fotovoltaica
function curvaSolar(x, x0, y0, W, H, mw, fecha, t){
  if(!mw || !mw.length || H < t*5) return;
  var mx = Math.max.apply(null, mw), ip = mw.indexOf(mx);
  var e = escala(0, mx*1.08 || 1, 3);
  x.font = t+'px Roboto,Arial';
  var L = x.measureText(f(e.mx,0)).width + t*0.8, R = t*0.6, T = t*2.2, B = t*1.9;
  function X(h){ return x0 + L + (W-L-R) * (h+0.5)/24; }
  function Y(v){ return y0 + T + (H-T-B) * (1 - v/e.mx); }
  x.textBaseline='middle'; x.textAlign='left'; x.fillStyle='#f5b301';
  x.font='bold '+t+'px Roboto,Arial'; x.fillText('Solar hora a hora', x0+L, y0+t*0.7);
  x.textAlign='right'; x.fillStyle='#8b949e'; x.font=t+'px Roboto,Arial';
  x.fillText('MW · '+fecha.substr(0,5), x0+W-R, y0+t*0.7);
  for(var v=0; v<=e.mx+e.paso/2; v+=e.paso){
    var y = Y(v);
    x.strokeStyle='#21262d'; x.lineWidth=1; x.beginPath(); x.moveTo(x0+L,y); x.lineTo(x0+W-R,y); x.stroke();
    x.fillStyle='#8b949e'; x.textAlign='right'; x.fillText(f(v,0), x0+L-t*0.4, y);
  }
  x.textBaseline='top';
  for(var h=0; h<24; h+=6){ x.textAlign='center'; x.fillText(h+'h', X(h), y0+H-B+t*0.5); }
  x.textAlign='right'; x.fillText('23h', X(23), y0+H-B+t*0.5);
  var g = x.createLinearGradient(0, y0+T, 0, y0+H-B);
  g.addColorStop(0, '#f5b30199'); g.addColorStop(1, '#f5b30110');
  x.beginPath(); x.moveTo(X(0), Y(0));
  for(var i=0;i<mw.length;i++) x.lineTo(X(i), Y(mw[i]));
  x.lineTo(X(mw.length-1), Y(0)); x.closePath(); x.fillStyle=g; x.fill();
  x.beginPath(); for(i=0;i<mw.length;i++){ if(i) x.lineTo(X(i),Y(mw[i])); else x.moveTo(X(i),Y(mw[i])); }
  x.strokeStyle='#f5b301'; x.lineWidth=Math.max(2,t*0.18); x.lineJoin='round'; x.stroke();
  // pico del dia
  var px = X(ip), py = Y(mx);
  x.beginPath(); x.arc(px, py, t*0.32, 0, 6.3); x.fillStyle='#f5b301'; x.fill();
  var txt = 'pico '+f(mx,0)+' MW a las '+ip+'h'; x.font='bold '+(t*0.9)+'px Roboto,Arial';
  x.textAlign = px > x0+W*0.6 ? 'right' : 'left'; x.textBaseline='bottom'; x.fillStyle='#e6edf3';
  x.fillText(txt, px + (x.textAlign==='left' ? t*0.6 : -t*0.6), py - t*0.2);
}

// Recuadro de embalses: nivel actual y su tendencia de 30 dias
function embalsesEn(x, x0, y0, W, H, d, t){
  x.textBaseline='top'; x.textAlign='left';
  x.font=t+'px Roboto,Arial'; x.fillStyle='#c9d1d9'; x.fillText('Embalses (volumen útil)', x0, y0);
  x.font='bold '+(t*2.2)+'px Roboto,Arial'; x.fillStyle=C_EMB;
  var txt = f(d.ultimo,1)+' %'; x.fillText(txt, x0, y0+t*1.4);
  var w = x.measureText(txt).width;
  if(d.hace30 !== null && d.hace30 !== undefined){
    var dif = d.ultimo - d.hace30;
    x.font=t+'px Roboto,Arial'; x.fillStyle='#8b949e';
    x.fillText((dif>=0?'+':'')+f(dif,1)+' pts en 30 días', x0+w+t*0.8, y0+t*2.2);
  }
  lineaEn(x, x0, y0+t*4.2, W, H-t*4.2, d.valores, function(i){ return dia(d.fechas[i]); }, 7, C_EMB, 1, t*0.85);
}

// ---------- paginas ----------
function poner(tit, valor, uni, sub, est){
  $('extra').style.display = 'none';
  $('tit').innerHTML = tit; $('valor').innerHTML = valor; $('uni').innerHTML = uni; $('sub').innerHTML = sub;
  $('fila').style.display = valor === null ? 'none' : 'flex';
  $('sub').style.display = sub === null ? 'none' : 'block';
  $('stats').style.display = est ? 'flex' : 'none';
  if(est) for(var i=0;i<3;i++){ $('l'+(i+1)).innerHTML = est[i][0]; $('s'+(i+1)).innerHTML = est[i][1]; }
}
function hora(s){ return s.substr(11,2)+'h'; }
function dia(s){ return s.substr(8,2)+'/'+s.substr(5,2); }
function solar(g){
  for(var i=0;g && i<g.tecnologias.length;i++) if(g.tecnologias[i].nombre==='Solar') return g.tecnologias[i];
  return {nombre:'Solar', pct:0, gwh:0};
}

function mostrar(){
  var p = PAGS[pagina];
  var tabs = document.querySelectorAll('.tab');
  for(var i=0;i<tabs.length;i++) tabs[i].className = 'tab' + (i===pagina ? ' on' : '');
  $('meta').innerHTML = 'XM &middot; ' + (D.consultado || '');
  $('img').style.display = 'none'; $('nota').style.display = 'none'; $('g').style.display = 'block';
  var err = D.errores || {};
  if(p==='precio'){
    var d = D.precio;
    if(!d){ poner('Precio de bolsa', '--', '', err.precio ? 'Sin datos de XM: '+err.precio : 'Consultando XM...', null); lienzo(); return; }
    poner('Precio de bolsa', f(d.ultimo,1), 'COP/kWh', 'Hora '+d.ultima_hora+' &middot; últimas 24 h publicadas',
      [['Promedio',f(d.promedio,1)],['Mínimo',f(d.minimo,1)],['Máximo',f(d.maximo,1)]]);
    var ir = D.irradiancia;
    if(ir){
      $('extra').innerHTML = '<b>'+f(ir.total_wh_m2,0)+'</b><i>Wh/m²</i><small>irradiación de hoy en Yopal</small>';
      $('extra').style.display = 'block';
    }
    precioEIrradiancia(d, ir);
  } else if(p==='demanda'){
    mostrarDemanda();
  } else {
    mostrarGeneracion();
  }
}

// Demanda: curva de hoy (grande) + energia de los ultimos 5 dias (pequena)
function mostrarDemanda(){
  poner('Demanda del SIN', null, '', null, null);
  var L = lienzo(), x = L.x, W = L.W, H = L.H, t = fs(0.03), d = D.demanda;
  // curva de hoy grande arriba; la de los ultimos dias, baja, debajo
  var gap = t*0.8, cw = W, ch = H*0.68;
  var bx = 0, by = ch+gap, bw = W, bh = H-ch-gap;
  var marco = null;
  if(AHORA && AHORA.curva && (AHORA.curva.real.length || AHORA.curva.pron.length)){
    marco = curvaDia(x, 0, 0, cw, ch, AHORA.curva, t);
  } else if(AHORA){
    var img = $('img'), src = 'tr/demanda.png?v=' + encodeURIComponent(AHORA.iso);
    if(img.getAttribute('src') !== src) img.setAttribute('src', src);
    img.style.left='0'; img.style.top='0'; img.style.width=cw+'px'; img.style.height=ch+'px';
    img.style.display = 'block';
  } else {
    x.font=t+'px Roboto,Arial'; x.fillStyle='#8b949e'; x.textAlign='center'; x.textBaseline='middle';
    x.fillText('Curva de tiempo real: solo en la versión en la nube', cw/2, ch/2);
  }
  if(d){
    var k = Math.max(0, d.valores.length-5);
    if(d.horas5 && d.horas5.length >= 24) curvaDias(x, bx, by, bw, bh, d.horas5, d.fechas, d.valores, C_PRON, t*0.85, marco);
    else lineaDias(x, bx, by, bw, bh, d.fechas.slice(k), d.valores.slice(k), C_PRON, t*0.85, marco);
  } else {
    x.font=t+'px Roboto,Arial'; x.fillStyle='#8b949e'; x.textAlign='left'; x.textBaseline='top';
    x.fillText((D.errores||{}).demanda ? 'Demanda diaria: sin datos de XM' : 'Consultando XM...', bx, by+t);
  }
}

// Generacion por tecnologia (solar resaltada) + embalses
function mostrarGeneracion(){
  var g = D.generacion, e = D.embalses, s = solar(g);
  if(g){
    poner('Generación y embalses', f(s.pct,1), '% solar',
          'Día '+g.fecha+' &middot; solar '+f(s.gwh,1)+' GWh de '+f(g.total_gwh,1)+' GWh &middot; renovable '+f(g.renovable_pct,0)+' %', null);
  } else {
    poner('Generación y embalses', '--', '% solar', (D.errores||{}).generacion ? 'Sin datos de generación' : 'Consultando XM...', null);
  }
  var L = lienzo(), x = L.x, W = L.W, H = L.H, t = fs(0.03);
  var lado = W > H*1.3, gap = t*2;
  var hayCurva = g && g.solar_mw && g.solar_mw.length;
  var gw = lado ? W*0.58 : W, colH = lado ? H : H*0.68;
  var sh = hayCurva ? colH*0.5 : 0, by = hayCurva ? sh + t*0.8 : 0, bh = colH - by;
  var ex = lado ? gw+gap : 0, ey = lado ? 0 : colH+gap, ew = lado ? W-gw-gap : W, eh = lado ? H : H-colH-gap;
  if(g){
    if(hayCurva) curvaSolar(x, 0, 0, gw, sh, g.solar_mw, g.fecha, t*0.9);
    var tecs = g.tecnologias.slice().sort(function(a,b){
      return (b.nombre==='Solar') - (a.nombre==='Solar') || b.pct - a.pct; });
    barrasTec(x, 0, by, gw, bh, tecs, hayCurva ? t*0.85 : t);
  }
  if(e) embalsesEn(x, ex, ey, ew, eh, e, t);
}

function barraInferior(){
  var cv = AHORA && AHORA.curva, re = cv && cv.real && cv.real.length ? cv.real[cv.real.length-1][1] : null;
  $('t0').innerHTML = D.precio ? f(D.precio.ultimo,0) : '--';
  $('t1').innerHTML = re !== null ? f(re,0) : '--';
  $('t2').innerHTML = (D.generacion ? f(solar(D.generacion).pct,0)+' %' : '--') + ' &middot; ' +
                      (D.embalses ? f(D.embalses.ultimo,0)+' %' : '--');
}

// ---------- datos de tiempo real (los guarda captura.py cada 5 a 20 min) ----------
var AHORA = null;
function cargarAhora(){
  var q = new XMLHttpRequest();
  q.open('GET','tr/captura.json?t='+Date.now(),true);
  q.onreadystatechange = function(){
    if(q.readyState!==4) return;
    if(q.status === 200){ try{ AHORA = JSON.parse(q.responseText); }catch(e){} }
    barraInferior(); if(PAGS[pagina]==='demanda') mostrar();
  };
  q.send();
}

function ir(i, manual){
  pagina = (i + PAGS.length) % PAGS.length;
  if(manual) pausaHasta = Date.now() + 60000;
  mostrar();
}
setInterval(function(){ if(Date.now() > pausaHasta) ir(pagina+1, false); }, ROTAR_SEG*1000);

function cargar(){
  var q = new XMLHttpRequest();
  q.open('GET','/api/datos?t='+Date.now(),true);
  q.onreadystatechange = function(){
    if(q.readyState!==4) return;
    try{ D = JSON.parse(q.responseText); barraInferior(); mostrar(); }catch(e){}
  };
  q.send();
}
window.onresize = mostrar;
cargar(); setInterval(cargar, 5*60*1000);
cargarAhora(); setInterval(cargarAhora, 60*1000);

// ---------- toques: pantalla completa, siguiente pagina, ir a un panel ----------
function enPantallaCompleta(){
  return document.fullscreenElement || document.webkitFullscreenElement ||
         (window.matchMedia && window.matchMedia('(display-mode: fullscreen)').matches);
}
function actualizarAviso(){ $('aviso').style.display = enPantallaCompleta() ? 'none' : 'block'; }
document.addEventListener('click', function(ev){
  for(var a = ev.target; a && a !== document.body; a = a.parentNode){
    if(a.tagName === 'A'){ pausaHasta = Date.now() + 60000; return; }   // enlaces: dejar que abran
  }
  pedirWakeLock();
  if(!enPantallaCompleta()){
    var el = document.documentElement, fn = el.requestFullscreen || el.webkitRequestFullscreen;
    if(fn){
      var pr = fn.call(el);
      if(pr && pr.then) pr.then(function(){
        try{ screen.orientation.lock('landscape').catch(function(){}); }catch(e){}
      }).catch(function(){});
      return;
    }
  }
  var n = ev.target;
  while(n && n !== document.body){
    if(n.getAttribute && n.getAttribute('data-i') !== null){ ir(parseInt(n.getAttribute('data-i'),10), true); return; }
    n = n.parentNode;
  }
  ir(ev.clientX < window.innerWidth/3 ? pagina-1 : pagina+1, true);
});
document.addEventListener('fullscreenchange', function(){ actualizarAviso(); setTimeout(mostrar,300); });
document.addEventListener('webkitfullscreenchange', function(){ actualizarAviso(); setTimeout(mostrar,300); });
actualizarAviso();

// ---------- mantener la pantalla encendida ----------
var wl = null;
function pedirWakeLock(){
  if(!('wakeLock' in navigator) || wl) return;
  navigator.wakeLock.request('screen').then(function(l){
    wl = l; l.addEventListener('release', function(){ wl = null; });
  }).catch(function(){});
}
document.addEventListener('visibilitychange', function(){ if(document.visibilityState==='visible') pedirWakeLock(); });
pedirWakeLock();
if('serviceWorker' in navigator){ navigator.serviceWorker.register('/sw.js').catch(function(){}); }
</script></body></html>"""


class Manejador(BaseHTTPRequestHandler):
    def _enviar(self, codigo, tipo, cuerpo):
        b = cuerpo if isinstance(cuerpo, bytes) else cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        ruta = self.path.split("?")[0]
        if ruta == "/api/datos":
            with _lock:
                cuerpo = json.dumps(_cache)
            self._enviar(200, "application/json; charset=utf-8", cuerpo)
        elif ruta in ("/", "/index.html"):
            self._enviar(200, "text/html; charset=utf-8", PAGINA)
        elif ruta == "/manifest.json":
            self._enviar(200, "application/manifest+json; charset=utf-8", MANIFEST)
        elif ruta == "/sw.js":
            self._enviar(200, "application/javascript; charset=utf-8", SERVICE_WORKER)
        elif ruta in ("/icono-192.png", "/icono-512.png"):
            self._enviar(200, "image/png", base64.b64decode(ICONO_512 if "512" in ruta else ICONO_192))
        else:
            self._enviar(404, "text/plain; charset=utf-8", "No encontrado")

    def log_message(self, *args):
        pass


def buscar_metricas(palabra):
    filas = listas(consultar("lists", "ListadoMetricas"))
    palabra = palabra.lower()
    for f in filas:
        texto = json.dumps(f, ensure_ascii=False)
        if palabra in texto.lower():
            a = minus(f)
            print("%-22s %-10s %-8s %s" % (a.get("metricid"), a.get("entity"), a.get("type"),
                                           a.get("metricname")))


if __name__ == "__main__":
    if "--probar" in sys.argv:
        actualizar(verboso=True)
        if _recursos["mapa"]:
            cuenta = {}
            for t in _recursos["mapa"].values():
                cuenta[t] = cuenta.get(t, 0) + 1
            print("Recursos por tecnologia:", cuenta)
    elif "--metricas" in sys.argv:
        i = sys.argv.index("--metricas")
        buscar_metricas(sys.argv[i + 1] if len(sys.argv) > i + 1 else "")
    else:
        threading.Thread(target=bucle_actualizacion, daemon=True).start()
        print("Tablero listo. Abre en el navegador:  http://localhost:%d" % PUERTO)
        HTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()
