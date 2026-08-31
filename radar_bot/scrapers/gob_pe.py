"""Compras menores a nivel NACIONAL, desde el buscador de gob.pe.

QUE ES ESTA FUENTE, Y POR QUE VALE

  Las compras por debajo de 8 UIT no pasan por SEACE, asi que `ocds_oece` no
  las ve. El barrido del 31/08/2026 demostro que los portales propios casi no
  existen: el de Madre de Dios es el unico sistema de cotizaciones en linea
  vivo del pais.

  Pero el articulo 50 del Reglamento de la Ley 32069 obliga a indagar el
  mercado, y cientos de entidades publican esa indagacion -- "solicitud de
  cotizacion", "solicitud de informacion" -- como publicacion en gob.pe, la
  plataforma unica del Estado. CENARES (las compras medicas nacionales),
  UGELs, municipalidades, beneficencias: todo en un mismo sitio.

  gob.pe no anuncia API, pero su buscador la tiene: `/busquedas.json` devuelve
  JSON limpio con filtros de tipo, categoria y FECHA. Medido: entre 90 y 170
  publicaciones de compras por semana segun la consulta. Y responde tanto al
  VPS como a una conexion peruana, asi que corre en la pasada horaria normal.

LO QUE NO TRAE: LA FECHA DE CIERRE

  El plazo esta dentro del PDF adjunto, no en los metadatos. NO SE INVENTA
  -- una fecha inventada es peor que ninguna --: la fila queda con
  `fecha_cierre` NULL y el panel la muestra durante una ventana corta desde su
  publicacion (ver `licitaciones_para_usuario`). Una cotizacion real dura dias,
  no semanas, asi que la ventana refleja la realidad sin fabricar un dato.

POR QUE FILTRA ANTES DE GUARDAR

  El mismo buscador devuelve concursos CAS, comunicados y resoluciones. Ya se
  vio lo que pasa cuando eso entra a `licitaciones`: datos_abiertos guardo 17
  fichas de catalogo que ningun cliente podia ver y el parte anunciaba "17
  nuevas". Aqui el filtro es explicito y con pruebas: se guarda lo que es una
  compra y se descarta lo que es empleo o burocracia.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import httpx

from shared.db import upsert_licitacion, log_scraping_start, log_scraping_end

log = logging.getLogger("radar.gob_pe")

API = "https://www.gob.pe/busquedas.json"
BASE = "https://www.gob.pe"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "es-PE,es;q=0.9",
}

# Cada consulta pesca publicaciones que las otras no etiquetan. Se unen por id,
# asi que el solape no duplica. La categoria sola no basta: etiquetar es
# opcional y muchas entidades no lo hacen.
CONSULTAS = [
    {"categoria[]": "53-contrataciones-del-estado"},
    {"term": "solicitud de cotizacion"},
    {"term": "indagacion de mercado"},
    {"term": "8 uit"},
]

DIAS_VENTANA = 3      # se repesca lo de los ultimos dias; el id deduplica
MAX_HOJAS = 6         # 25 items por hoja; 6 hojas cubren de sobra la ventana

_MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
          "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
          "octubre": 10, "noviembre": 11, "diciembre": 12}

_RE_FECHA = re.compile(r"(\d{1,2})\s+de\s+([a-zA-Z]+)\s+de\s+(\d{4})")
_RE_HREF = re.compile(r'href="([^"]+)"')

# Lo que ES una compra. Sobre titulo + extracto, sin tildes y en minusculas.
_RE_COMPRA = re.compile(
    r"cotizacion|cotizaciones|indagacion de mercado|solicitud de informacion"
    r"|8\s*uit|adquisicion|requerimiento de bienes|requerimiento de servicio"
    r"|estudio de mercado")

# Lo que NO lo es aunque la consulta lo devuelva. Gana sobre la inclusion:
# un "comunicado sobre la solicitud de cotizacion X" sigue sin ser una compra.
_RE_RUIDO = re.compile(
    r"concurso publico de meritos|practicas pre|practicante|\bcas\b"
    r"|comunicado|fe de erratas|resolucion|directiva|seleccion de personal"
    r"|convocatoria de personal|resultado")


def _normalizar(texto: str) -> str:
    tabla = str.maketrans("áéíóúÁÉÍÓÚñÑ", "aeiouAEIOUnN")
    return (texto or "").translate(tabla).lower()


def _fecha_es(texto: str | None) -> datetime | None:
    """'28 de agosto de 2026' -> datetime. None si no se entiende.

    None y no una fecha de relleno: el panel usa la fecha de publicacion para
    decidir la ventana de visibilidad, y una inventada mostraria como fresca
    una compra ya cerrada.
    """
    m = _RE_FECHA.search(texto or "")
    if not m:
        return None
    mes = _MESES.get(_normalizar(m.group(2)))
    if not mes:
        return None
    try:
        return datetime(int(m.group(3)), mes, int(m.group(1)))
    except ValueError:
        return None


def _es_compra(titulo: str, extracto: str) -> bool:
    texto = _normalizar(f"{titulo} {extracto}")
    if _RE_RUIDO.search(texto):
        return False
    return bool(_RE_COMPRA.search(texto))


def _parsear_item(item: dict) -> dict | None:
    """Un resultado del buscador -> licitacion, o None si no es una compra.

    Los items no comparten forma: los hay sin titulo, sin fecha o sin enlace
    (fichas de institucion, paginas sueltas). Todo lo dudoso se descarta:
    mejor perder una publicacion rara que guardar otra fila invisible.
    """
    titulo = (item.get("name_with_parent") or "").strip()
    if not titulo:
        # A veces el titulo solo viene dentro del anchor de `url`.
        titulo = re.sub(r"<[^>]+>", "", item.get("url") or "").strip()
    entidad = (item.get("content_sub_title_card") or "").strip()
    fecha_pub = _fecha_es(item.get("publication"))
    if not titulo or not entidad or not fecha_pub:
        return None
    if not _es_compra(titulo, item.get("content") or ""):
        return None

    href = ""
    m = _RE_HREF.search(item.get("url") or "")
    if m:
        href = m.group(1)
        if href.startswith("/"):
            href = BASE + href

    # El deposito del documento (el PDF con el plazo y los requisitos).
    pdf = item.get("action_url") or ""
    bases = [pdf] if pdf.startswith("http") else []

    from radar_bot.scrapers.orchestrator import _detectar_depto
    depto = _detectar_depto(f"{entidad} {titulo}")

    return {
        "id": f"gobpe_{item.get('id')}",
        "fuente": "gob_pe",
        "tipo": "cotizacion",
        "nomenclatura": None,
        "entidad": entidad[:300],
        "entidad_tipo": None,
        "objeto": titulo[:500],
        "monto_referencial": None,
        "departamento": depto,
        "fecha_publicacion": fecha_pub,
        # El plazo vive dentro del PDF. NULL a proposito: no se inventa.
        "fecha_cierre": None,
        "url": href,
        "bases_urls": bases,
        "estado": "convocado",
    }


async def scrape_gob_pe(user_id: int = 0) -> list[dict]:
    log_id = await log_scraping_start("gob_pe")
    desde = (datetime.now() - timedelta(days=DIAS_VENTANA)).strftime("%Y-%m-%d")

    vistos: set[str] = set()
    nuevas: list[dict] = []
    encontradas = 0
    errores = 0
    respuestas_vivas = 0

    async with httpx.AsyncClient(timeout=25, headers=HEADERS,
                                 follow_redirects=True) as client:
        for consulta in CONSULTAS:
            for hoja in range(1, MAX_HOJAS + 1):
                params = {"contenido[]": "publicaciones", "desde": desde,
                          "sheet": hoja, **consulta}
                try:
                    resp = await client.get(API, params=params)
                    if resp.status_code != 200:
                        errores += 1
                        log.warning("gob.pe respondio %s a %s",
                                    resp.status_code, consulta)
                        break
                    respuestas_vivas += 1
                    items = (resp.json().get("data", {}).get("attributes", {})
                             .get("results") or [])
                except Exception as e:
                    errores += 1
                    log.warning("gob.pe fallo con %s: %s", consulta, e)
                    break

                if not items:
                    break
                for item in items:
                    try:
                        data = _parsear_item(item)
                    except Exception as e:
                        errores += 1
                        log.error("item %s: %s", item.get("id"), e)
                        continue
                    if not data or data["id"] in vistos:
                        continue
                    vistos.add(data["id"])
                    encontradas += 1
                    if await upsert_licitacion(data):
                        nuevas.append(data)

    # El mismo trato honesto que las demas fuentes: cero con el servicio vivo
    # se dice, no se confunde con un fin de semana tranquilo.
    detalle = None
    if respuestas_vivas == 0:
        detalle = f"CAIDA -- ninguna consulta a {API} respondio 200"
    elif encontradas == 0:
        detalle = (f"SIN EXTRAER -- {respuestas_vivas} respuestas 200 y "
                   f"ninguna publicacion de compra en {DIAS_VENTANA} dias: "
                   f"o cambio el formato del JSON o cambio el volumen")
    if detalle:
        log.warning("gob_pe: %s", detalle)
    await log_scraping_end(log_id, encontradas, len(nuevas), errores, detalle)
    log.info("GOB.PE: %d encontradas, %d nuevas, %d errores",
             encontradas, len(nuevas), errores)
    return nuevas
