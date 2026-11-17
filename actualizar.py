# -*- coding: utf-8 -*-
"""
Version "nube" del tablero XM.

GitHub Actions ejecuta este archivo cada 5 minutos (junto con captura.py):
  1. Si los datos tienen mas de 55 minutos (o si FORZAR=1), consulta XM usando
     las mismas funciones de server.py. XM publica una vez al dia, asi que
     consultar cada hora es suficiente.
  2. Guarda los datos en docs/datos.json (si un panel falla, conserva el dato anterior).
  3. Genera la pagina estatica en docs/ (index.html, manifest, iconos).
Luego GitHub Pages publica la carpeta docs/ (incluida la captura de docs/tr/).
"""

import base64
import json
import os
import sys
from datetime import datetime, timedelta

import server

SALIDA = "docs"
MINUTOS_ENTRE_CONSULTAS = 55


def escribir(nombre, contenido, binario=False):
    ruta = os.path.join(SALIDA, nombre)
    if binario:
        with open(ruta, "wb") as f:
            f.write(contenido)
    else:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(contenido)


def main():
    os.makedirs(SALIDA, exist_ok=True)
    ruta_datos = os.path.join(SALIDA, "datos.json")

    previo = {}
    if os.path.exists(ruta_datos):
        try:
            with open(ruta_datos, encoding="utf-8") as f:
                previo = json.load(f)
        except ValueError:
            previo = {}

    ahora = datetime.now(server.TZ_CO)
    try:
        ultima = datetime.fromisoformat(previo["consultado_iso"])
    except (KeyError, ValueError, TypeError):
        ultima = None
    toca = (os.environ.get("FORZAR") == "1" or ultima is None
            or ahora - ultima > timedelta(minutes=MINUTOS_ENTRE_CONSULTAS))

    correctos = len(server.PANELES)
    if toca:
        server.actualizar(verboso=True)
        datos = dict(server._cache)
        datos["errores"] = dict(server._cache["errores"])
        datos["consultado_iso"] = ahora.isoformat(timespec="seconds")
        correctos = 0
        for nombre, _ in server.PANELES:
            if nombre in datos["errores"]:
                if previo.get(nombre) is not None:
                    datos[nombre] = previo[nombre]       # se conserva el ultimo dato bueno
            else:
                correctos += 1
        escribir("datos.json", json.dumps(datos, ensure_ascii=False))
    else:
        print("Datos de XM consultados a las %s: no hace falta consultar de nuevo." % previo.get("consultado"))

    # La misma pagina del celular, pero leyendo datos.json con rutas relativas
    pagina = (server.PAGINA
              .replace("'/api/datos?t='", "'datos.json?t='")
              .replace('href="/manifest.json"', 'href="manifest.json"')
              .replace('href="/icono-192.png"', 'href="icono-192.png"')
              .replace("register('/sw.js')", "register('sw.js')"))
    escribir("index.html", pagina)

    manifest = json.loads(server.MANIFEST)
    manifest["start_url"] = "./"
    manifest["scope"] = "./"
    for icono in manifest["icons"]:
        icono["src"] = icono["src"].lstrip("/")
    escribir("manifest.json", json.dumps(manifest, ensure_ascii=False))
    escribir("sw.js", server.SERVICE_WORKER)
    escribir("icono-192.png", base64.b64decode(server.ICONO_192), binario=True)
    escribir("icono-512.png", base64.b64decode(server.ICONO_512), binario=True)
    escribir(".nojekyll", "")

    print("Paneles actualizados: %d de %d" % (correctos, len(server.PANELES)))
    if correctos == 0:
        print("XM no respondio a ninguna consulta.")
        sys.exit(1)


if __name__ == "__main__":
    main()
