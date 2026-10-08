"""Compras menores a 8 UIT del IMA, el instituto de agua del Gobierno Regional
de Cusco.

Fuente: https://www.ima.org.pe/adquisiciones-bienes-servicios-v2/s.html

QUE ES Y POR QUE VALE

  El Instituto de Manejo de Agua y Medio Ambiente (IMA) es un organo del
  Gobierno Regional de Cusco con presupuesto propio, y publica sus solicitudes
  de cotizacion -- bienes y servicios por debajo de 8 UIT -- en una tabla
  HTML de su web, con numero, descripcion, fecha de publicacion, fecha y HORA
  de cierre y el PDF con los terminos. Esas compras no pasan por SEACE, asi
  que `ocds_oece` no las ve, y el IMA no publica en gob.pe, asi que `gob_pe`
  tampoco.

  Responde al VPS (comprobado el 2026-10-07: 200 y 33 KB), asi que corre en la
  pasada horaria del servidor y no necesita el puente.

LO QUE SE GUARDA Y LO QUE NO

  Solo las VIGENTES. La pagina marca cada fila como VIGENTE o VENCIDO, y la
  mayoria de las veinte de la primera pagina estan vencidas: guardarlas la
  primera vez haria que el parte dijera "19 nuevas" de cotizaciones que nadie
  puede presentar, que es exactamente el ruido que ya se pago con
  datos_abiertos. Lo vencido se queda en la web del IMA.

  Se lee solo la primera pagina. La tabla va de la mas reciente a la mas
  antigua y tiene veinte filas por pagina; las vigentes caben de sobra.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from shared import fechas
from shared.db import log_scraping_end, log_scraping_start, refrescar_licitacion

log = logging.getLogger("radar.ima_cusco")

URL = "https://www.ima.org.pe/adquisiciones-bienes-servicios-v2/s.html"
BASE = "https://www.ima.org.pe/"
ENTIDAD = "Instituto de Manejo de Agua y Medio Ambiente - IMA (Gobierno Regional de Cusco)"
FUENTE = "ima_cusco"

_RE_NUMERO = re.compile(r"N[°º]?\s*(\d+)\s*-\s*(\d{4})", re.IGNORECASE)
_RE_PUBLICADO = re.compile(r"Publicado el\s+(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
_RE_FECHA = re.compile(r"(\d{2}/\d{2}/\d{4})")
_RE_HORA = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]\.?M\.?)", re.IGNORECASE)


def _fecha(texto: str) -> datetime | None:
    try:
        return fechas.desde_texto(texto, "%d/%m/%Y")
    except ValueError:
        return None


def _cierre(texto: str) -> datetime | None:
    """Fecha y hora de cierre a partir de la celda PLAZO ("11/02/2026 4:30 PM").

    La hora importa: una cotizacion que cierra a las 4:30 de la tarde deja de
    verse en el panel a esa hora y no a medianoche, que es cuando ya no sirve.
    Sin hora legible se guarda el dia a las 23:59, que es lo mas tarde que
    puede cerrar sin inventar una hora concreta.
    """
    m = _RE_FECHA.search(texto)
    if not m:
        return None
    dia = _fecha(m.group(1))
    if dia is None:
        return None
    h = _RE_HORA.search(texto)
    if not h:
        return dia.replace(hour=23, minute=59)
    hora, minuto = int(h.group(1)), int(h.group(2))
    if h.group(3).upper().startswith("P") and hora < 12:
        hora += 12
    if h.group(3).upper().startswith("A") and hora == 12:
        hora = 0
    return dia.replace(hour=hora, minute=minuto)


def _id(numero: str, anio: str) -> str:
    return "ima_" + hashlib.md5(f"ima_cusco_{numero}-{anio}".encode()).hexdigest()[:16]


def parsear_listado(html: str) -> list[dict]:
    """Las filas de la tabla, ya como licitaciones. Las vencidas no entran."""
    soup = BeautifulSoup(html, "html.parser")
    tabla = soup.find("table", class_="tabla")
    if tabla is None:
        return []

    salida = []
    for fila in tabla.find_all("tr"):
        celdas = fila.find_all("td")
        if len(celdas) < 4:
            continue

        descripcion_td, tipo_td, plazo_td = celdas[1], celdas[2], celdas[3]
        plazo_txt = plazo_td.get_text(" ", strip=True)
        if "VENCIDO" in plazo_txt.upper():
            continue

        titulo = descripcion_td.find("strong")
        titulo_txt = titulo.get_text(" ", strip=True) if titulo else ""
        m = _RE_NUMERO.search(titulo_txt)
        if not m:
            continue
        numero, anio = m.group(1), m.group(2)

        # El texto de la celda menos el titulo y menos la linea "| Publicado el
        # dd/mm/aaaa |": lo que queda es el objeto.
        texto = descripcion_td.get_text("\n", strip=True)
        lineas = [
            ln.strip(" |") for ln in texto.split("\n")
            if ln.strip() and ln.strip() != titulo_txt
            and not _RE_PUBLICADO.search(ln)
        ]
        objeto = " ".join(lineas).strip()
        if len(objeto) < 10:
            continue

        pub = _RE_PUBLICADO.search(texto)
        fecha_pub = _fecha(pub.group(1)) if pub else None
        fecha_cierre = _cierre(plazo_txt)

        tipo_bien = tipo_td.get_text(" ", strip=True).upper() or "BIEN/SERVICIO"

        pdf = ""
        enlace = fila.find("a", href=True)
        if enlace:
            pdf = enlace["href"]
            if not pdf.startswith("http"):
                pdf = BASE + pdf.lstrip("/")

        salida.append({
            "id": _id(numero, anio),
            "fuente": FUENTE,
            "tipo": "cotizacion",
            "nomenclatura": f"SC-{numero}-{anio}-IMA",
            "entidad": ENTIDAD,
            "entidad_tipo": "gore",
            "entidad_ruc": None,
            "objeto": f"[{tipo_bien}] {objeto}"[:500],
            "monto_referencial": None,
            "moneda": "PEN",
            "departamento": "Cusco",
            "fecha_publicacion": fecha_pub,
            "fecha_cierre": fecha_cierre,
            "url": pdf or URL,
            "bases_urls": [pdf] if pdf else [],
            "estado": "convocado",
        })
    return salida


async def scrape_ima_cusco(user_id: int = 0) -> list[dict]:
    # Importados aqui y no arriba: orchestrator importa este modulo (a traves
    # de _run_ima_cusco) y este necesita su Sonda y sus cabeceras. Es el mismo
    # arreglo que usa gob_pe.
    from radar_bot.scrapers.orchestrator import HEADERS, Sonda

    log_id = await log_scraping_start(FUENTE)
    sonda = Sonda(FUENTE)
    nuevas: list[dict] = []
    encontradas = 0
    errores = 0

    async with httpx.AsyncClient(timeout=25, headers=HEADERS,
                                 follow_redirects=True) as client:
        resp = await sonda.get(client, URL)
        if resp is not None:
            try:
                filas = parsear_listado(resp.text)
            except Exception as e:  # noqa: BLE001
                errores += 1
                filas = []
                log.error("IMA Cusco: la tabla no se pudo leer: %s", e)
            for data in filas:
                encontradas += 1
                try:
                    # Se refresca y no solo se inserta: los plazos se prorrogan.
                    if await refrescar_licitacion(data):
                        nuevas.append(data)
                except Exception as e:  # noqa: BLE001
                    errores += 1
                    log.error("IMA Cusco: fila %s: %s", data["id"], e)

    # La sonda distingue CAIDA de SIN EXTRAER. Pero aqui hay un tercer caso
    # normal: la pagina responde, la tabla esta, y simplemente no hay ninguna
    # vigente hoy. Eso no es averia y no se anota como tal.
    detalle = None
    if resp is None or (encontradas == 0 and 'class="tabla"' not in resp.text):
        detalle = sonda.diagnostico(0)
    if detalle:
        log.warning("%s: %s", FUENTE, detalle)
    await log_scraping_end(log_id, encontradas, len(nuevas),
                           errores + sonda.errores, detalle)
    log.info("IMA CUSCO: %d vigentes, %d nuevas, %d errores",
             encontradas, len(nuevas), errores)
    return nuevas
