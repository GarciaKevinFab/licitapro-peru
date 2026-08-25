"""Scraper OCDS OECE -- API oficial de Contrataciones Abiertas del Estado peruano.

Fuente: https://contratacionesabiertas.oece.gob.pe/api/v1/releases
Es el portal OCDS del OECE (ex OSCE), alimentado desde SEACE v3. Publica en
tiempo real: al momento de escribir esto el `publishedDate` del paquete coincide
con el minuto de la consulta.

Por que este scraper reemplaza al de SEACE y al XLSX de CONOSCE:

  - SEACE 3.0 exige reCAPTCHA v3 en toda busqueda. No se evade: no hay scraper
    viable contra ese portal.
  - El XLSX de CONOSCE (`ocds_api.py`) es un volcado historico con retraso. Su
    `fechapresentacionpropuesta` mas reciente es anterior a hoy, asi que no
    puede sostener alertas. Sigue siendo util como historico de precios.
  - Esta API si trae convocatorias vivas: en un muestreo de 300 releases, 132
    seguian abiertas.

Notas de la estructura OCDS, verificadas contra la API:

  - `tenderPeriod` SIEMPRE trae startDate == endDate (240/240 en el muestreo):
    es la fecha de convocatoria, nunca una ventana de ofertas. No sirve como
    fecha de cierre.
  - `enquiryPeriod.endDate` es el unico plazo accionable publicado (presente en
    ~92% de los releases). Es el cierre de consultas y observaciones.
  - `items[].statusDetails` lleva el estado real: CONVOCADO / ADJUDICADO /
    CONTRATADO.
  - `documents[]` trae URLs directas a las bases en PDF servidas por SEACE, sin
    CAPTCHA.
  - Un mismo proceso (`ocid`) se republica a diario con contenido identico, asi
    que hay que deduplicar por ocid y refrescar la fila en vez de insertar.
  - El texto viene en UTF-8 correcto, a diferencia del XLSX de CONOSCE, que
    llega con la acentuacion corrupta.
"""
import logging
import hashlib
from datetime import datetime, timedelta, timezone

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from shared.db import log_scraping_start, log_scraping_end, get_config, refrescar_licitacion
from shared.config import DEPARTAMENTOS, normalizar, match_keywords

log = logging.getLogger("radar.ocds_oece")

API = "https://contratacionesabiertas.oece.gob.pe/api/v1/releases"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Hora de Lima. Las columnas de la BD son TIMESTAMP sin zona, asi que las fechas
# de la API (que vienen con offset -05:00) se convierten y se les quita el tzinfo.
TZ_LIMA = timezone(timedelta(hours=-5))

# procurementMethodDetails -> codigo corto de TIPOS_PROCEDIMIENTO
TIPO_POR_METODO = {
    "licitacion publica": "LP",
    "licitacion publica abreviada": "LPA",
    "concurso publico": "CP",
    "concurso publico abreviado": "CP",
    "concurso publico para consultoria": "CP",
    "concurso publico de servicios": "CP",
    "adjudicacion simplificada": "AS",
    "subasta inversa electronica": "SIE",
    "contratacion directa": "CD",
    "comparacion de precios": "CdP",
}

ESTADO_POR_DETALLE = {
    "convocado": "convocado",
    "adjudicado": "adjudicado",
    "contratado": "contratado",
    "desierto": "desierto",
    "cancelado": "cancelado",
    "nulo": "cancelado",
}

# Provincias y ciudades frecuentes que no llevan el nombre del departamento.
ALIAS_DEPTO = {
    "puerto maldonado": "Madre de Dios",
    "tambopata": "Madre de Dios",
    "huancayo": "Junín",
    "satipo": "Junín",
    "chanchamayo": "Junín",
    "la oroya": "Junín",
    "tarma": "Junín",
    "chulucanas": "Piura",
    "morropon": "Piura",
    "iquitos": "Loreto",
    "trujillo": "La Libertad",
    "chiclayo": "Lambayeque",
    "huaraz": "Áncash",
    "chimbote": "Áncash",
    "juliaca": "Puno",
    "callao": "Callao",
    "lima metropolitana": "Lima",
}


def _gen_id(ocid: str) -> str:
    """ID estable derivado del ocid: el mismo proceso cae siempre en la misma fila."""
    return hashlib.md5(f"ocds_oece_{ocid}".encode()).hexdigest()[:16]


def _fecha(valor) -> datetime | None:
    """ISO-8601 con offset -> datetime naive en hora de Lima."""
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ_LIMA).replace(tzinfo=None)
    return dt


def _tipo(metodo: str | None) -> str | None:
    if not metodo:
        return None
    clave = normalizar(metodo).strip()
    if clave in TIPO_POR_METODO:
        return TIPO_POR_METODO[clave]
    # Coincidencia parcial: "Concurso Publico de Obras" -> CP
    for k, v in TIPO_POR_METODO.items():
        if clave.startswith(k) or k in clave:
            return v
    return None


def _tipo_entidad(entidad: str) -> str:
    e = normalizar(entidad)
    if "gobierno regional" in e:
        return "gore"
    if "municipalidad" in e:
        return "muni"
    if "ministerio de defensa" in e:
        return "ffaa"
    if "ministerio" in e:
        return "min"
    if "universidad" in e:
        return "univ"
    if any(x in e for x in ("hospital", "essalud", "red de salud", "instituto nacional de salud")):
        return "hosp"
    if any(x in e for x in ("poder judicial", "corte superior", "corte suprema")):
        return "pj"
    if any(x in e for x in ("ejercito", "marina de guerra", "fuerza aerea", "policia")):
        return "ffaa"
    return "otro"


# Nombres de departamento normalizados una sola vez, al importar el modulo.
_DEPTOS_NORM = [(normalizar(d), d) for d in DEPARTAMENTOS]


def _departamento(texto: str) -> str | None:
    """Detecta el departamento en el texto libre del proceso."""
    t = normalizar(texto)
    # "DEPARTAMENTO DE PIURA" es la forma mas confiable: se busca primero.
    for norm, real in _DEPTOS_NORM:
        if f"departamento de {norm}" in t or f"region {norm}" in t:
            return real
    for alias, real in ALIAS_DEPTO.items():
        if alias in t:
            return real
    for norm, real in _DEPTOS_NORM:
        if norm in t:
            return real
    return None


def _ruc(release: dict) -> str | None:
    for parte in release.get("parties") or []:
        for extra in parte.get("additionalIdentifiers") or []:
            if extra.get("scheme") == "PE-RUC" and extra.get("id"):
                return str(extra["id"])
    return None


def _parsear(release: dict) -> dict | None:
    """Release OCDS -> fila de `licitaciones`. None si le falta lo esencial."""
    ocid = release.get("ocid")
    tender = release.get("tender") or {}
    if not ocid or not tender:
        return None

    entidad = (release.get("buyer") or {}).get("name") or (
        tender.get("procuringEntity") or {}
    ).get("name")
    objeto = tender.get("description") or tender.get("title")
    items = tender.get("items") or []
    if not objeto and items:
        objeto = items[0].get("description")
    if not entidad or not objeto:
        return None

    # El monto llega como 0.0 cuando la entidad no lo publica: eso es "sin dato",
    # no "gratis". Guardarlo como 0 haria que el filtro de monto lo descarte mal.
    valor = tender.get("value") or {}
    monto = valor.get("amount")
    monto = float(monto) if monto else None

    detalle = normalizar(items[0].get("statusDetails") or "") if items else ""
    estado = ESTADO_POR_DETALLE.get(detalle, "convocado")

    bases = [
        d["url"] for d in (tender.get("documents") or [])
        if d.get("url") and d.get("documentType") in (None, "biddingDocuments")
    ]

    # El departamento casi nunca es un campo propio: se deduce del texto, donde
    # suele aparecer como "... - DEPARTAMENTO DE PIURA".
    texto_ubicacion = " ".join(
        [entidad, objeto] + [str(i.get("description") or "") for i in items[:4]]
    )

    return {
        "id": _gen_id(ocid),
        "fuente": "ocds_oece",
        "tipo": _tipo(tender.get("procurementMethodDetails")),
        "nomenclatura": tender.get("title"),
        "entidad": entidad,
        "entidad_tipo": _tipo_entidad(entidad),
        "entidad_ruc": _ruc(release),
        "objeto": objeto[:2000],
        "monto_referencial": monto,
        "moneda": valor.get("currency") or "PEN",
        "fecha_publicacion": _fecha(tender.get("datePublished")) or _fecha(release.get("date")),
        # enquiryPeriod.endDate es el unico plazo accionable que publica la API.
        "fecha_cierre": _fecha((tender.get("enquiryPeriod") or {}).get("endDate")),
        "estado": estado,
        "departamento": _departamento(texto_ubicacion),
        "url": f"{API}?ocid={ocid}",
        "bases_urls": bases,
    }


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=12))
async def _pagina(cliente: httpx.AsyncClient, page: int) -> dict:
    r = await cliente.get(API, params={"page": page}, timeout=45)
    r.raise_for_status()
    return r.json()


async def scrape_ocds_oece(
    user_id: int = 0,
    max_paginas: int = 40,
    dias_atras: int = 10,
) -> list[dict]:
    """Recorre la API OCDS de la mas reciente hacia atras y guarda lo relevante.

    Los releases vienen ordenados por fecha descendente y son 20 por pagina, asi
    que basta con cortar en cuanto una pagina entera queda fuera de la ventana
    de `dias_atras`, en vez de recorrer el historico completo.
    """
    log_id = await log_scraping_start("ocds_oece")
    config = await get_config(user_id)
    keywords = list(config["keywords"]) if config and config["keywords"] else []
    excluir = list(config["keywords_excluir"]) if config and config["keywords_excluir"] else []
    regiones = list(config["regiones"]) if config and config["regiones"] else []
    monto_min = config["monto_min"] if config else 0
    monto_max = config["monto_max"] if config else 999_999_999

    corte = datetime.now() - timedelta(days=dias_atras)
    vistos: set[str] = set()
    nuevas: list[dict] = []
    encontradas = 0
    errores = 0
    detalle_error = None

    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as cliente:
            for page in range(1, max_paginas + 1):
                try:
                    data = await _pagina(cliente, page)
                except Exception as e:
                    errores += 1
                    detalle_error = f"pagina {page}: {str(e)[:150]}"
                    log.warning(f"Fallo la pagina {page}: {e}")
                    break

                releases = data.get("releases") or []
                if not releases:
                    break

                fuera_de_ventana = 0
                for release in releases:
                    ocid = release.get("ocid")
                    if not ocid or ocid in vistos:
                        continue
                    vistos.add(ocid)

                    fecha_release = _fecha(release.get("date"))
                    if fecha_release and fecha_release < corte:
                        fuera_de_ventana += 1
                        continue

                    lic = _parsear(release)
                    if not lic:
                        continue
                    encontradas += 1

                    if regiones and lic["departamento"] and lic["departamento"] not in regiones:
                        continue
                    monto = lic["monto_referencial"]
                    if monto is not None and not (monto_min <= monto <= monto_max):
                        continue
                    texto_match = f"{lic['objeto']} {lic['entidad']} {lic['nomenclatura'] or ''}"
                    if keywords and not match_keywords(texto_match, keywords):
                        continue
                    # Exclusiones: matan homonimos como "servidores" (informaticos
                    # vs. servidores publicos), que producen falsos positivos caros.
                    if excluir and match_keywords(texto_match, excluir):
                        continue

                    if await refrescar_licitacion(lic):
                        nuevas.append(lic)

                # Toda la pagina quedo fuera de la ventana: como vienen ordenados
                # por fecha descendente, lo que sigue es aun mas viejo.
                if fuera_de_ventana == len(releases):
                    log.info(f"Corte en pagina {page}: releases anteriores a {corte:%Y-%m-%d}")
                    break

                if not (data.get("links") or {}).get("next"):
                    break

    except Exception as e:
        errores += 1
        detalle_error = str(e)[:200]
        log.error(f"OCDS OECE fallo: {e}", exc_info=True)

    await log_scraping_end(log_id, encontradas, len(nuevas), errores, detalle_error)
    log.info(
        f"OCDS OECE: {len(vistos)} procesos revisados, {encontradas} parseados, "
        f"{len(nuevas)} nuevas"
    )
    return nuevas
