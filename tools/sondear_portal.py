"""Sondea un portal de compras desde la maquina donde se ejecuta y cuenta que devuelve.

PARA QUE SIRVE

  Los portales de cotizaciones de los GORE (Madre de Dios, Cusco, Moquegua)
  solo responden a conexiones peruanas. Desde el VPS no se puede ver que
  sirven, y el scraper, cuando no encuentra la tabla que espera, devuelve
  cero filas. Para escribir o ajustar un parser hace falta mirar el HTML
  real, y la unica maquina que lo ve es la PC puente.

  Este script se corre ALLI y escribe en pantalla lo justo para decidir sin
  mandar el HTML entero: codigo, URL final (las redirecciones a un login se
  ven aqui), tamano, titulo, cuantas tablas hay y como se llaman sus columnas.
  Tambien guarda el HTML en data/ por si hay que leerlo entero.

USO (desde la raiz del proyecto, en la PC puente)

    .venv-tarea\\Scripts\\python.exe tools\\sondear_portal.py https://cotizaciones.regioncusco.gob.pe/

  Sin argumentos sondea todos los portales de GORE_COTIZACIONES_PORTALS.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import httpx

from radar_bot.scrapers.orchestrator import (
    GORE_COTIZACIONES_APAGADOS,
    GORE_COTIZACIONES_PORTALS,
    HEADERS,
    _sopa,
)


def _texto(celda) -> str:
    return re.sub(r"\s+", " ", celda.get_text(" ", strip=True))[:40]


def sondear(url: str) -> None:
    print(f"\n=== {url}")
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=30,
                         follow_redirects=True, verify=False)
    except Exception as e:  # noqa: BLE001
        print(f"  FALLO DE RED: {type(e).__name__}: {e}")
        return

    print(f"  HTTP {resp.status_code}  ->  {resp.url}")
    print(f"  {len(resp.content)} bytes, content-type {resp.headers.get('content-type', '?')}")
    if resp.history:
        print("  redirecciones: " + " -> ".join(str(r.url) for r in resp.history))

    soup = _sopa(resp.text)
    titulo = soup.title.get_text(strip=True) if soup.title else ""
    print(f"  titulo: {titulo[:100]!r}")

    tablas = soup.find_all("table")
    formularios = soup.find_all("form")
    marcos = [m.get("src", "") for m in soup.find_all("iframe")]
    scripts = [s.get("src", "") for s in soup.find_all("script") if s.get("src")]
    print(f"  tablas: {len(tablas)}  formularios: {len(formularios)}  "
          f"iframes: {len(marcos)}  scripts externos: {len(scripts)}")
    for m in marcos[:3]:
        print(f"    iframe -> {m}")
    # Una aplicacion que pinta la tabla con JavaScript trae pocas etiquetas y
    # un bundle grande: si no hay tabla y si hay un app.js/main.js, el dato
    # esta en una API, no en el HTML.
    for s in scripts[:6]:
        print(f"    script -> {s}")
    for f in formularios[:3]:
        print(f"    form {f.get('method', 'get').upper()} {f.get('action', '')!r} "
              f"campos={[i.get('name') for i in f.find_all('input')][:6]}")

    for i, t in enumerate(tablas[:4], 1):
        filas = t.find_all("tr")
        print(f"  tabla {i}: {len(filas)} filas")
        if filas:
            cab = filas[0].find_all(["th", "td"])
            print(f"    cabecera: {[_texto(c) for c in cab]}")
        if len(filas) > 1:
            datos = filas[1].find_all("td")
            print(f"    primera fila ({len(datos)} celdas): {[_texto(c) for c in datos]}")

    # Enlaces que huelen a listado de compras, por si la tabla esta en otra ruta.
    pistas = []
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if re.search(r"cotiz|convoc|requer|compra|listad|proceso", h, re.IGNORECASE):
            pistas.append(h)
    if pistas:
        print("  enlaces con pinta de listado: " + ", ".join(dict.fromkeys(pistas[:8])))

    destino = RAIZ / "data" / f"sonda-{re.sub(r'[^a-z0-9.]+', '-', resp.url.host or 'portal')}.html"
    destino.parent.mkdir(exist_ok=True)
    destino.write_text(resp.text, encoding="utf-8")
    print(f"  HTML guardado en {destino.relative_to(RAIZ)}")


def main() -> None:
    # Sin argumentos: los activos y tambien los apagados, que es justo lo que
    # se quiere volver a mirar de vez en cuando.
    urls = sys.argv[1:] or [
        p["url"] for p in (*GORE_COTIZACIONES_PORTALS.values(),
                           *GORE_COTIZACIONES_APAGADOS.values())
    ]
    for u in urls:
        sondear(u)


if __name__ == "__main__":
    main()
