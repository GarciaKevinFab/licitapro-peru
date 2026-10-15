"""Contrataciones menores a 8 UIT de la Municipalidad Provincial de Tambopata.

Fuente: https://portal.munitambopata.gob.pe/servicios/contratacion-bienes-servicios

QUE ES Y POR QUE VALE

  La MPT (Puerto Maldonado, Madre de Dios) publica sus estudios de mercado
  menores a 8 UIT en su propio portal: unas diez por dia habil, con uno o dos
  dias para cotizar. No pasan por SEACE, asi que `ocds_oece` no las ve (de la
  MPT solo llegaban 26 filas, todas procesos de SEACE). La fuente vieja
  `municipalidades` apuntaba a www.munitambopata.gob.pe/contrataciones, que ya
  no existe, y esta apagada.

  La pagina es una aplicacion React que lee una API publica en JSON
  (/api/v1/convocatorias): no hay HTML que raspar. Cada convocatoria trae su
  numero de E/M, objeto, tipo (BIENES, SERVICIOS ...), inicio, cierre,
  ampliacion y dos PDF: el "expediente" -- la solicitud de cotizacion del SIGA
  con los items -- y el TDR o las especificaciones.

  Responde al VPS (comprobado el 2026-10-15: 200 en 1,9 s), asi que corre en la
  pasada horaria del servidor y no necesita el puente.

LO QUE SE GUARDA

  Solo las abiertas (estado ACTIVA o AMPLIADA). Para cada una:
    * la licitacion, con el cierre real (la ampliacion si la hay);
    * la SC y el TDR en licitacion_documentos (tipos "sc" y "eett"), igual que
      los del GOREMAD: la web llena la SC sobre su propio PDF
      (shared/llenar_siga.py) y adjunta el TDR firmado y sellado;
    * los items de la SC en `descripcion` ("DESC (CANT UNIDAD)" por linea),
      para que la propuesta nazca con ellos (shared/cotizacion.sembrar).

ENVIAR

  El portal recibe la postulacion en un formulario sin captcha (RUC, correo,
  razon social y un ZIP de hasta 40 MB). LicitaPro NO lo envia: arma el ZIP y
  da los datos; el postor lo sube. El formulario no se puede enlazar por
  convocatoria (su buscador no lee la URL), asi que el panel da el portal y el
  numero de E/M para pegar en su buscador.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime

import httpx

from shared import fechas
from shared.db import log_scraping_end, log_scraping_start, refrescar_licitacion

log = logging.getLogger("radar.mpt_tambopata")

BASE = "https://portal.munitambopata.gob.pe"
API = BASE + "/api/v1/convocatorias"
PORTAL = BASE + "/servicios/contratacion-bienes-servicios"
ENTIDAD = "Municipalidad Provincial de Tambopata"
FUENTE = "mpt_tambopata"
ABIERTAS = {"ACTIVA", "AMPLIADA"}
POR_PAGINA = 100
MAX_PAGINAS = 4      # ~400 convocatorias: las abiertas son las mas recientes

_RE_EM = re.compile(r"E/M\s*N\.?\s*[ºo°]?\s*(\d+)\s*-\s*(\d{4})", re.IGNORECASE)
_RE_CONV = re.compile(r"Convocatoria\s*(\d+)", re.IGNORECASE)


def _hora_lima(texto: str | None) -> datetime | None:
    """"2026-10-16T21:30:00.000Z" -> 2026-10-16 16:30 (hora de pared de Lima)."""
    if not texto:
        return None
    try:
        return (datetime.fromisoformat(texto.replace("Z", "+00:00"))
                .astimezone(fechas.LIMA).replace(tzinfo=None))
    except ValueError:
        return None


def _id(uuid: str) -> str:
    return "mpt_" + hashlib.md5(f"{FUENTE}_{uuid}".encode()).hexdigest()[:16]


def _url(ruta: str | None) -> str | None:
    if not ruta:
        return None
    return ruta if ruta.startswith("http") else BASE + "/" + ruta.lstrip("/")


def numero_em(nomenclatura: str | None) -> str:
    """"E/M-4201-2026-MPT" -> "E/M N.º 4201-2026", como lo escribe la entidad
    (referencia del anexo 05 y texto para su buscador)."""
    m = re.match(r"E/M-(\d+)-(\d{4})", nomenclatura or "")
    return f"E/M N.º {m.group(1)}-{m.group(2)}" if m else (nomenclatura or "")


def a_licitacion(c: dict) -> dict | None:
    """Una convocatoria de la API como licitacion. None si no es utilizable."""
    if (c.get("estado") or "").upper() not in ABIERTAS or not c.get("id"):
        return None
    texto = c.get("descripcion") or ""
    m = _RE_EM.search(c.get("estudioMercado") or texto)
    if not m:
        return None
    numero, anio = int(m.group(1)), m.group(2)
    conv = _RE_CONV.search(c.get("estudioMercado") or "")
    sufijo = f"-C{conv.group(1)}" if conv and conv.group(1) != "1" else ""
    # El objeto es la descripcion sin "Convocatoria 1 – E/M N.º 4201-2026-".
    m_txt = _RE_EM.search(texto)
    objeto = (texto[m_txt.end():] if m_txt else texto).lstrip(" -–—").strip() or texto
    tipo_bien = (c.get("type") or "BIEN/SERVICIO").upper()
    expediente, tdr = _url(c.get("expediente")), _url(c.get("tdr"))
    return {
        "id": _id(c["id"]),
        "fuente": FUENTE,
        "tipo": "cotizacion",
        "nomenclatura": f"E/M-{numero}-{anio}-MPT{sufijo}",
        "entidad": ENTIDAD,
        "entidad_tipo": "muni",
        "entidad_ruc": None,
        "objeto": f"[{tipo_bien}] {objeto}"[:500],
        "monto_referencial": None,
        "moneda": "PEN",
        "departamento": "Madre de Dios",
        "fecha_publicacion": _hora_lima(c.get("fechaInicio")),
        "fecha_cierre": _hora_lima(c.get("fechaAmpliacion")) or _hora_lima(c.get("fechaFin")),
        "url": PORTAL,
        "bases_urls": [u for u in (expediente, tdr) if u],
        "estado": "convocado",
        # Lo que la web necesita y no tiene columna propia: se pasa aparte.
        "_documentos": [d for d in (
            {"tipo": "sc", "categoria": "SOLICITUD DE COTIZACIÓN", "nombre": "expediente.pdf",
             "url": expediente},
            {"tipo": "eett", "categoria": "TÉRMINOS DE REFERENCIA / EETT", "nombre": "tdr.pdf",
             "url": tdr}) if d["url"]],
    }


def descripcion_desde_sc(contenido: bytes) -> str | None:
    """Los items de la SC del SIGA como "DESC (CANT UNIDAD)", uno por linea."""
    from shared.cotizacion import cantidad_legible
    from shared.llenar_siga import leer_items_siga
    try:
        items = leer_items_siga(contenido)
    except Exception as e:  # noqa: BLE001
        log.info("MPT: la SC no se pudo leer: %s", e)
        return None
    lineas = []
    for it in items:
        # Los parentesis del nombre se cambian por corchetes: `items_de_texto`
        # lee la cantidad del ULTIMO "(...)" de la linea.
        desc = re.sub(r"\s+", " ", it["descripcion"]).replace("(", "[").replace(")", "]")
        unidad = f" {it['unidad']}" if it.get("unidad") else ""
        lineas.append(f"{desc} ({cantidad_legible(it['cantidad'])}{unidad})")
    return "\n".join(lineas) or None


async def _completar(client, data: dict) -> None:
    """Guarda la SC y el TDR, y los items de la SC si la fila aun no los tiene."""
    from radar_bot.scrapers.orchestrator import _guardar_documentos
    from shared.db import connection, contenido_documento
    await _guardar_documentos(client, data["id"], data["_documentos"])
    async with connection() as conn:
        tiene = await conn.fetchval("SELECT descripcion FROM licitaciones WHERE id=$1",
                                    data["id"])
    if tiene:
        return
    sc = await contenido_documento(data["id"], "sc")
    desc = descripcion_desde_sc(bytes(sc)) if sc else None
    if desc:
        async with connection() as conn:
            await conn.execute("UPDATE licitaciones SET descripcion=$2 WHERE id=$1",
                               data["id"], desc)


async def scrape_mpt_tambopata(user_id: int = 0) -> list[dict]:
    from radar_bot.scrapers.orchestrator import HEADERS, Sonda

    log_id = await log_scraping_start(FUENTE)
    sonda = Sonda(FUENTE)
    nuevas: list[dict] = []
    encontradas = errores = 0
    respondio = False

    async with httpx.AsyncClient(timeout=30, headers=HEADERS,
                                 follow_redirects=True) as client:
        for pagina in range(1, MAX_PAGINAS + 1):
            resp = await sonda.get(client, f"{API}?page={pagina}&limit={POR_PAGINA}&search=")
            if resp is None:
                break
            try:
                cuerpo = resp.json()
            except ValueError:
                errores += 1
                log.error("MPT: la API no devolvio JSON")
                break
            respondio = True
            ahora = fechas.ahora()
            abiertas = [d for d in (a_licitacion(c) for c in cuerpo.get("data") or [])
                        if d and (d["fecha_cierre"] is None or d["fecha_cierre"] > ahora)]
            for data in abiertas:
                encontradas += 1
                try:
                    fila = {k: v for k, v in data.items() if not k.startswith("_")}
                    if await refrescar_licitacion(fila):
                        nuevas.append(fila)
                    await _completar(client, data)
                except Exception as e:  # noqa: BLE001
                    errores += 1
                    log.error("MPT: %s: %s", data["nomenclatura"], e)
            # La API va de la mas reciente a la mas antigua: una pagina sin
            # ninguna abierta significa que ya no quedan mas atras.
            if not abiertas or pagina >= (cuerpo.get("lastPage") or 1):
                break

    detalle = None if respondio else sonda.diagnostico(0)
    if detalle:
        log.warning("%s: %s", FUENTE, detalle)
    await log_scraping_end(log_id, encontradas, len(nuevas), errores + sonda.errores, detalle)
    log.info("MPT TAMBOPATA: %d abiertas, %d nuevas, %d errores",
             encontradas, len(nuevas), errores)
    return nuevas
