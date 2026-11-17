# -*- coding: utf-8 -*-
"""
Tablero del Mercado Electrico Colombiano (datos de XM)
------------------------------------------------------
Un solo archivo, sin librerias externas (solo Python 3).
Pensado para un celular Android viejo con Termux.

  python server.py               -> inicia el tablero en http://localhost:8080
  python server.py --probar      -> consulta XM una vez y muestra que funciono
  python server.py --metricas X  -> busca metricas de XM que contengan X

Paneles:
  1. Precio de Bolsa Nacional   (PrecBolsNaci, horario, COP/kWh)
  2. Demanda del SIN            (DemaSIN, diario, kWh -> GWh/dia)
  3. Generacion por tecnologia  (Gene por Recurso + ListadoRecursos)
  4. Nivel de embalses          (VoluUtilDiarEner / CapaUtilDiarEner, diario)
"""

import base64
import json
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

_cache = {"precio": None, "demanda": None, "generacion": None, "embalses": None,
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
    return {"fechas": [d.strftime("%Y-%m-%d") for d, _ in serie], "valores": gwh,
            "ultimo": gwh[-1], "fecha": serie[-1][0].strftime("%d/%m/%Y"),
            "promedio": sum(gwh) / len(gwh), "minimo": min(gwh), "maximo": max(gwh),
            "metrica": metrica}


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
    dia = dias[-1][0]
    # 2) Solo para ese dia se descarga el detalle por planta
    por_recurso = horarios(consultar("hourly", M_GENERACION, "Recurso", dia, dia))
    if not por_recurso:
        raise RuntimeError("XM no devolvio generacion por planta")
    suma = dict((t, 0.0) for t in TECNOLOGIAS)
    for cod, pts in por_recurso.items():
        suma[mapa.get(cod, "Otras")] += sum(x for t, x in pts if t.date() == dia)
    total = sum(suma.values())
    if total <= 0:
        raise RuntimeError("generacion en cero")
    tecs = [{"nombre": t, "gwh": suma[t] / 1e6, "pct": 100.0 * suma[t] / total}
            for t in TECNOLOGIAS if suma[t] > 0]
    return {"fecha": dia.strftime("%d/%m/%Y"), "total_gwh": total / 1e6,
            "renovable_pct": 100.0 * sum(suma[t] for t in RENOVABLES) / total,
            "tecnologias": tecs}


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
           ("generacion", panel_generacion), ("embalses", panel_embalses)]


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
  .fila{display:flex;align-items:baseline;flex-wrap:wrap;margin-top:.5vmin}
  #valor{font-size:15vmin;font-weight:700;line-height:1;color:#f5b301;margin-right:2vmin}
  #uni{font-size:5vmin;color:#c9d1d9}
  #sub{font-size:3.4vmin;color:#8b949e;margin-top:.6vmin}
  .stats{display:flex;margin:1.4vmin 0 .6vmin}
  .st{flex:1;font-size:3.2vmin;color:#8b949e}
  .st b{display:block;font-size:4.6vmin;color:#e6edf3}
  #cont{flex:1;position:relative;min-height:0}
  canvas{position:absolute;left:0;top:0;width:100%;height:100%}
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
    <div class="fila"><span id="valor">--</span><span id="uni"></span></div>
    <div id="sub">Consultando XM...</div>
    <div class="stats" id="stats">
      <div class="st"><span id="l1"></span><b id="s1">--</b></div>
      <div class="st"><span id="l2"></span><b id="s2">--</b></div>
      <div class="st"><span id="l3"></span><b id="s3">--</b></div>
    </div>
    <div id="cont"><canvas id="g"></canvas></div>
  </div>
  <div id="barra">
    <div class="tab" data-i="0">Precio $/kWh<b id="t0">--</b></div>
    <div class="tab" data-i="1">Demanda GWh<b id="t1">--</b></div>
    <div class="tab" data-i="2">Renovable<b id="t2">--</b></div>
    <div class="tab" data-i="3">Embalses<b id="t3">--</b></div>
  </div>
</div>
<div id="aviso">Toca para pantalla completa</div>
<script>
var D = {}, pagina = 0, pausaHasta = 0, ROTAR_SEG = 12;
var PAGS = ['precio','demanda','generacion','embalses'];
var mP = location.search.match(/p=(\d)/); if(mP) pagina = parseInt(mP[1],10) % 4;
var COLORES = {'Hidráulica':'#3987e5','Térmica':'#d95926','Eólica':'#199e70','Solar':'#c98500',
               'Biomasa y cogen.':'#d55181','Otras':'#6e7681'};

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

function linea(vals, etiqueta, cadaN, color, dec){
  var L0 = lienzo(), x = L0.x, W = L0.W, H = L0.H, n = vals.length;
  if(!n || H < 30) return;
  var t = fs(0.032), L = t*3.8, R = t*0.8, T = t*0.8, B = t*2;
  var mn = Math.min.apply(null,vals), mx = Math.max.apply(null,vals);
  var pad = (mx-mn)*0.15 || Math.abs(mx)*0.05 || 1; mn = mn-pad; mx = mx+pad;
  function X(i){ return L + (W-L-R) * (n>1 ? i/(n-1) : 0.5); }
  function Y(v){ return T + (H-T-B) * (1-(v-mn)/(mx-mn)); }
  x.font = t+'px Roboto,Arial'; x.textBaseline = 'middle';
  for(var k=0;k<=3;k++){
    var v = mn + (mx-mn)*k/3, y = Y(v);
    x.strokeStyle='#21262d'; x.lineWidth=1; x.beginPath(); x.moveTo(L,y); x.lineTo(W-R,y); x.stroke();
    x.fillStyle='#8b949e'; x.textAlign='right'; x.fillText(f(v,dec), L-t*0.4, y);
  }
  x.textAlign='center'; x.textBaseline='top';
  for(var i=0;i<n;i++){
    if(i%cadaN===0 || (i===n-1 && (n-1)%cadaN >= cadaN/2)){
      x.textAlign = (i===n-1 && i>0) ? 'right' : (i===0 ? 'left' : 'center');
      x.fillText(etiqueta(i), i===n-1 && i>0 ? W : X(i), H-B+t*0.5);
    }
  }
  var g = x.createLinearGradient(0,T,0,H-B);
  g.addColorStop(0, color+'55'); g.addColorStop(1, color+'00');
  x.beginPath(); x.moveTo(X(0),H-B);
  for(i=0;i<n;i++) x.lineTo(X(i),Y(vals[i]));
  x.lineTo(X(n-1),H-B); x.closePath(); x.fillStyle=g; x.fill();
  x.beginPath(); for(i=0;i<n;i++){ if(i) x.lineTo(X(i),Y(vals[i])); else x.moveTo(X(i),Y(vals[i])); }
  x.strokeStyle=color; x.lineWidth=Math.max(2,t*0.16); x.lineJoin='round'; x.stroke();
  x.beginPath(); x.arc(X(n-1),Y(vals[n-1]),t*0.32,0,6.3); x.fillStyle=color; x.fill();
}

function barras(tecs){
  var L0 = lienzo(), x = L0.x, W = L0.W, H = L0.H, n = tecs.length;
  if(!n || H < 30) return;
  var t = fs(0.036), fila = Math.min(H/n, t*2.4), alto = Math.max(6, fila*0.5);
  x.font = t+'px Roboto,Arial'; x.textBaseline='middle';
  var Lnom = 0; for(var i=0;i<n;i++) Lnom = Math.max(Lnom, x.measureText(tecs[i].nombre).width);
  Lnom += t*0.8;
  x.font = 'bold '+t+'px Roboto,Arial'; var wPct = x.measureText('100,0 %').width + t*0.5;
  x.font = (t*0.8)+'px Roboto,Arial'; var wGwh = x.measureText('000,0 GWh').width;
  var ancho = W - Lnom - t*0.5 - wPct - wGwh;
  var max = 0; for(i=0;i<n;i++) max = Math.max(max, tecs[i].pct);
  x.font = t+'px Roboto,Arial';
  for(i=0;i<n;i++){
    var y = fila*i + fila/2, w = Math.max(3, ancho * tecs[i].pct / max), c = COLORES[tecs[i].nombre] || '#6e7681';
    x.fillStyle = '#c9d1d9'; x.textAlign='left'; x.fillText(tecs[i].nombre, 0, y);
    x.fillStyle = c; x.beginPath();
    var r = Math.min(4, alto/2, w/2), x0 = Lnom, y0 = y-alto/2;
    x.moveTo(x0,y0); x.lineTo(x0+w-r,y0); x.arcTo(x0+w,y0,x0+w,y0+r,r);
    x.lineTo(x0+w,y0+alto-r); x.arcTo(x0+w,y0+alto,x0+w-r,y0+alto,r); x.lineTo(x0,y0+alto); x.closePath(); x.fill();
    x.fillStyle = '#e6edf3'; x.font = 'bold '+t+'px Roboto,Arial';
    x.fillText(f(tecs[i].pct,1)+' %', x0+w+t*0.5, y);
    x.fillStyle = '#8b949e'; x.font = (t*0.8)+'px Roboto,Arial';
    x.fillText(f(tecs[i].gwh,1)+' GWh', x0+w+t*0.5+wPct, y);
    x.font = t+'px Roboto,Arial';
  }
}

// ---------- paginas ----------
function poner(tit, valor, uni, sub, est){
  $('tit').innerHTML = tit; $('valor').innerHTML = valor; $('uni').innerHTML = uni; $('sub').innerHTML = sub;
  $('stats').style.display = est ? 'flex' : 'none';
  if(est) for(var i=0;i<3;i++){ $('l'+(i+1)).innerHTML = est[i][0]; $('s'+(i+1)).innerHTML = est[i][1]; }
}
function hora(s){ return s.substr(11,2)+'h'; }
function dia(s){ return s.substr(8,2)+'/'+s.substr(5,2); }

function mostrar(){
  var p = PAGS[pagina], d = D[p], err = (D.errores||{})[p];
  var tabs = document.querySelectorAll('.tab');
  for(var i=0;i<tabs.length;i++) tabs[i].className = 'tab' + (i===pagina ? ' on' : '');
  $('meta').innerHTML = 'XM &middot; ' + (D.consultado || '');
  var TIT = {precio:'Precio de bolsa', demanda:'Demanda del SIN', generacion:'Generación por tecnología', embalses:'Nivel de embalses'};
  if(!d){
    poner(TIT[p], '--', '', err ? 'Sin datos de XM: '+err : 'Consultando XM...', null);
    lienzo(); return;
  }
  if(p==='precio'){
    poner(TIT[p], f(d.ultimo,1), 'COP/kWh', 'Hora '+d.ultima_hora+' &middot; últimas 24 h publicadas',
      [['Promedio',f(d.promedio,1)],['Mínimo',f(d.minimo,1)],['Máximo',f(d.maximo,1)]]);
    linea(d.valores, function(i){ return hora(d.horas[i]); }, 4, '#f5b301', 0);
  } else if(p==='demanda'){
    poner(TIT[p], f(d.ultimo,1), 'GWh/día', 'Día '+d.fecha+' &middot; últimos 30 días',
      [['Promedio',f(d.promedio,1)+' GWh'],['Mínimo',f(d.minimo,1)+' GWh'],['Máximo',f(d.maximo,1)+' GWh']]);
    linea(d.valores, function(i){ return dia(d.fechas[i]); }, 7, '#3987e5', 0);
  } else if(p==='generacion'){
    poner(TIT[p], f(d.renovable_pct,1), '% renovable', 'Día '+d.fecha+' &middot; total '+f(d.total_gwh,1)+' GWh', null);
    barras(d.tecnologias);
  } else {
    poner(TIT[p], f(d.ultimo,1), '% de volumen útil', 'Día '+d.fecha+' &middot; últimos 30 días',
      [['Hace 7 días',f(d.hace7,1)+' %'],['Hace 30 días',f(d.hace30,1)+' %'],
       ['Cambio 30 d',(d.hace30!==null ? (d.ultimo-d.hace30>=0?'+':'')+f(d.ultimo-d.hace30,1)+' pts' : '--')]]);
    linea(d.valores, function(i){ return dia(d.fechas[i]); }, 7, '#199e70', 0);
  }
}

function barraInferior(){
  $('t0').innerHTML = D.precio ? f(D.precio.ultimo,0) : '--';
  $('t1').innerHTML = D.demanda ? f(D.demanda.ultimo,0) : '--';
  $('t2').innerHTML = D.generacion ? f(D.generacion.renovable_pct,0)+' %' : '--';
  $('t3').innerHTML = D.embalses ? f(D.embalses.ultimo,0)+' %' : '--';
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

// ---------- toques: pantalla completa, siguiente pagina, ir a un panel ----------
function enPantallaCompleta(){
  return document.fullscreenElement || document.webkitFullscreenElement ||
         (window.matchMedia && window.matchMedia('(display-mode: fullscreen)').matches);
}
function actualizarAviso(){ $('aviso').style.display = enPantallaCompleta() ? 'none' : 'block'; }
document.addEventListener('click', function(ev){
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
