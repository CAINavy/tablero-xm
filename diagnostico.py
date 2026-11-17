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
    p("  Items:", len(items), "| fechas:", [i.get("Date", "")[:10] for i in items])
    for i in items[-2:]:
        for clave in ("HourlyEntities", "DailyEntities", "ListEntities"):
            ents = i.get(clave)
            if ents is not None:
                p("  ", i.get("Date", "")[:10], clave, "entidades:", len(ents))
                for e in ents[:3]:
                    v = e.get("Values") or {}
                    nums = [server.num(x) for k, x in v.items() if k.lower().startswith("hour")]
                    nums = [x for x in nums if x is not None]
                    p("     Id:", e.get("Id"), "| claves:", list(v.keys())[:6],
                      "| horas con dato:", len(nums), "| suma:", round(sum(nums), 1) if nums else None,
                      "| muestra:", json.dumps(v, ensure_ascii=False)[:160])


pruebas = [
    ("hourly", "PrecBolsNaci", "Sistema", 10, 0),
    ("hourly", "DemaReal", "Sistema", 10, 0),
    ("hourly", "DemaCome", "Sistema", 10, 0),
    ("hourly", "Gene", "Sistema", 10, 0),
    ("hourly", "Gene", "Recurso", 8, 6),
    ("daily", "VoluUtilDiarEner", "Sistema", 15, 0),
    ("daily", "CapaUtilDiarEner", "Sistema", 15, 0),
    ("daily", "PorcVoluUtilDiar", "Sistema", 15, 0),
    ("lists", "ListadoRecursos", "Sistema", 1, 0),
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

p("=" * 70)
p("Metricas relacionadas en ListadoMetricas:")
try:
    filas = server.listas(server.consultar("lists", "ListadoMetricas"))
    p("  total metricas:", len(filas), "| campos:", list(filas[0].keys()) if filas else None)
    claves = ("dema", "gene", "volu", "capa", "embal", "porc", "precbols")
    for f in filas:
        a = server.minus(f)
        if any(k in str(a.get("metricid", "")).lower() for k in claves):
            p("  ", a.get("metricid"), "|", a.get("entity"), "|", a.get("type"), "|",
              a.get("metricunits"), "|", a.get("maxdays"), "|", a.get("metricname"))
except Exception as e:
    p("  ERROR:", e)

texto = "\n".join(salida)
print(texto)
with open("diagnostico.txt", "w", encoding="utf-8") as f:
    f.write(texto + "\n")
