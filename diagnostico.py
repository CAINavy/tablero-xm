# -*- coding: utf-8 -*-
"""Diagnostico: muestra que responde la API de XM para cada metrica del tablero."""
import json
from datetime import datetime, timedelta

import server

hoy = datetime.now(server.TZ_CO).date()
salida = []


def p(*a):
    salida.append(" ".join(str(x) for x in a))


def resumir(resp):
    items = resp.get("Items", []) or []
    p("  Items:", len(items))
    sumas = {}
    for i in items:
        fecha = i.get("Date", "")[:10]
        for clave in ("HourlyEntities", "DailyEntities"):
            for e in i.get(clave) or []:
                if "Value" in e:
                    x = server.num(e.get("Value"))
                else:
                    v = e.get("Values") or {}
                    xs = [server.num(x) for k, x in v.items() if k.lower().startswith("hour")]
                    xs = [x for x in xs if x is not None]
                    x = sum(xs) if xs else None
                if x is not None:
                    sumas[fecha] = sumas.get(fecha, 0) + x
    for fecha in sorted(sumas):
        p("   ", fecha, "total del dia:", round(sumas[fecha] / 1e6, 2), "millones")


pruebas = [
    ("daily", "DemaSIN", "Sistema", 10, 0),
    ("hourly", "DemaReal", "Sistema", 10, 0),
    ("hourly", "DemaReal", "Sistema", 30, 26),
    ("hourly", "DemaRealReg", "Sistema", 8, 4),
    ("hourly", "DemaRealNoReg", "Sistema", 8, 4),
    ("hourly", "Gene", "Sistema", 10, 0),
    ("daily", "PorcVoluUtilDiar", "Sistema", 6, 0),
]

for tipo, metrica, entidad, desde, hasta in pruebas:
    p("=" * 70)
    p(tipo, metrica, entidad, hoy - timedelta(days=desde), "->", hoy - timedelta(days=hasta))
    try:
        r = server.consultar(tipo, metrica, entidad, hoy - timedelta(days=desde), hoy - timedelta(days=hasta))
        p("  Respuesta (inicio):", json.dumps(r, ensure_ascii=False)[:300])
        resumir(r)
    except Exception as e:
        p("  ERROR:", e)

texto = "\n".join(salida)
print(texto)
with open("diagnostico.txt", "w", encoding="utf-8") as f:
    f.write(texto + "\n")
