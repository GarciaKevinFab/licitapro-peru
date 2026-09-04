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
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from shared import fechas
from shared.banderas import calcular, umbrales_por_tipo
from shared.config import DEPARTAMENTOS, normalizar
from shared.db import log_scraping_end, log_scraping_start, refrescar_licitacion

log = logging.getLogger("radar.ocds_oece")

API = "https://contratacionesabiertas.oece.gob.pe/api/v1/releases"

# ---------------------------------------------------------- pasarela opcional
# POR QUE HACE FALTA SALIR POR OTRO SITIO
#
#   OECE responde 403 a todo el trafico de este servidor -- API y portada por
#   igual -- porque sale por una IP de datacenter fuera de Peru. Desde una
#   conexion peruana la misma URL da 200. No es el formato ni las cabeceras:
#   probado con User-Agent de navegador y con Referer, mismo 403.
#
#   Con OECE_PROXY_URL configurada, las peticiones van a un Worker de
#   Cloudflare (tools/worker-oece.js) que las hace desde su red y devuelve la
#   respuesta tal cual.
#
#   Sin configurar se va directo, como siempre: asi el desarrollo desde una
#   maquina peruana no necesita nada, y si algun dia OECE deja de bloquear
#   basta con vaciar la variable.
_PROXY = (os.getenv("OECE_PROXY_URL") or "").rstrip("/")
_PROXY_SECRETO = os.getenv("OECE_PROXY_SECRETO") or ""


def _destino() -> tuple[str, dict]:
    """URL y cabeceras a usar: por la pasarela si esta puesta, o directo."""
    if _PROXY and _PROXY_SECRETO:
        return f"{_PROXY}/api/v1/releases", {**HEADERS, "x-oece-secreto": _PROXY_SECRETO}
    # Media configuracion es peor que ninguna: con URL y sin secreto el Worker
    # responde 401, y pareceria que quien nos bloquea es OECE.
    if _PROXY and not _PROXY_SECRETO:
        log.warning(
            "OECE_PROXY_URL esta puesta pero falta OECE_PROXY_SECRETO: se ignora "
            "la pasarela y se va directo, que probablemente de 403."
        )
    return API, HEADERS

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
    "concurso publico abreviado": "CPA",
    "concurso publico para consultoria": "CP",
    "concurso publico de servicios": "CP",
    "adjudicacion simplificada": "AS",
    "subasta inversa electronica": "SIE",
    "contratacion directa": "CD",
    "comparacion de precios": "CdP",
    # Regimen anterior a la Ley 30225: no se convocan ya, pero llenan el
    # historico. Se mapean para poder nombrarlos en la ficha. Ojo: NO entran ni
    # en CODIGOS_ABREVIADOS ni en CODIGOS_ORDINARIOS de shared.banderas, asi que
    # siguen sin bandera de plazo -- sus plazos legales eran los de otra ley y
    # no tenemos una norma medida con la que compararlos.
    "adjudicacion directa selectiva": "ADS",
    "adjudicacion de menor cuantia": "AMC",
    "adjudicacion selectiva": "ASEL",
    "regimen especial": "RE",
    "convenio": "CONV",
    "contratacion internacional": "CI",
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
    # Coincidencia parcial: "Concurso Publico de Obras" -> CP. Se prueban las
    # claves mas largas primero porque "concurso publico" es prefijo de
    # "concurso publico abreviado": al reves, toda abreviada caeria en CP y se
    # mezclaria con los ordinarios, que tienen plazos legales muy distintos.
    for k, v in sorted(TIPO_POR_METODO.items(), key=lambda kv: -len(kv[0])):
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


def _ruc_de(parte: dict) -> str | None:
    for extra in parte.get("additionalIdentifiers") or []:
        if extra.get("scheme") == "PE-RUC" and extra.get("id"):
            return str(extra["id"])
    return None


def _ruc(release: dict, rol: str = "buyer") -> str | None:
    """RUC de la parte con ese rol.

    Antes devolvia el de la PRIMERA parte con RUC, sin mirar el rol. En una
    convocatoria solo hay una parte y coincidia por casualidad; en un release ya
    adjudicado estan la entidad Y el proveedor, asi que podia entregar el RUC
    del ganador como si fuera el de la entidad. Con las banderas eso importa: la
    de ganador recurrente cruza los dos RUC, y si se intercambian el resultado
    es ruido con apariencia de dato.
    """
    for parte in release.get("parties") or []:
        if rol in (parte.get("roles") or []):
            ruc = _ruc_de(parte)
            if ruc:
                return ruc
    return None


def _adjudicacion(release: dict) -> dict:
    """Postores, ganador y monto adjudicado. Vacio si aun no se resolvio.

    `parties` lista a TODOS los postores con el rol 'tenderer' (se vieron hasta
    58 en un mismo proceso), asi que contarlos da el numero real de
    participantes. Es el dato con el que se ve un proceso de postor unico.
    """
    partes = release.get("parties") or []
    tenderers = [p for p in partes if "tenderer" in (p.get("roles") or [])]
    ganadores = [p for p in partes if "supplier" in (p.get("roles") or [])]

    monto = 0.0
    for a in release.get("awards") or []:
        valor = (a.get("value") or {}).get("amount")
        if valor:
            monto += float(valor)
        else:
            # Algunos awards no traen `value` propio y el importe vive en los
            # items. Sumarlos evita perder la cifra en esos casos.
            for it in a.get("items") or []:
                total = (it.get("totalValue") or {}).get("amount")
                if total:
                    monto += float(total)

    datos = {}
    # Solo se informa el numero de postores si el proceso ya se adjudico: en una
    # convocatoria abierta `parties` trae solo a la entidad, y un 0 ahi se leeria
    # como "nadie se presento" cuando en realidad es "todavia no se sabe".
    if ganadores or "award" in (release.get("tag") or []):
        datos["numero_postores"] = len(tenderers) or None
        if ganadores:
            datos["proveedor_ganador"] = ganadores[0].get("name")
            datos["proveedor_ruc"] = _ruc_de(ganadores[0])
    if monto:
        datos["monto_adjudicado"] = round(monto, 2)
    return datos


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
        # La propia API cuenta los dias segun su norma; recalcularlos entre
        # fechas introduciria un error nuestro donde la fuente no tiene ninguno.
        "plazo_consultas_dias": (tender.get("enquiryPeriod") or {}).get("durationInDays"),
        # goods / services / works. Determina el plazo legal de pago: bienes y
        # servicios tienen los 10 dias habiles de la Ley 32069; las obras se
        # rigen por reglas propias.
        "categoria": tender.get("mainProcurementCategory"),
        **_adjudicacion(release),
    }


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=12))
async def _pagina(cliente: httpx.AsyncClient, page: int) -> dict:
    url, cabeceras = _destino()
    r = await cliente.get(url, params={"page": page}, headers=cabeceras, timeout=45)
    r.raise_for_status()
    return r.json()


async def scrape_ocds_oece(
    max_paginas: int = 400,
    dias_atras: int = 10,
) -> list[dict]:
    """Trae TODAS las convocatorias de la ventana. No filtra por usuario.

    Aqui no se aplica ningun filtro de negocio, y es deliberado: las
    licitaciones son datos publicos que valen para todos los inquilinos, asi
    que se scrapean una vez y se guardan enteras. Cada usuario las recorta al
    leer, en `licitaciones_para_usuario`.

    Filtrar aqui era un error caro: la ingesta usaba la config del usuario 1,
    de modo que el pozo compartido quedaba reducido a los rubros, las dos
    regiones y el tope de monto de UN cliente. Cualquier otro suscriptor abria
    un panel vacio y concluia, con razon, que el producto no funciona.

    Los releases vienen ordenados por fecha descendente y son 20 por pagina, asi
    que basta con cortar en cuanto una pagina entera queda fuera de la ventana
    de `dias_atras`, en vez de recorrer el historico completo. `max_paginas` es
    solo un tope de seguridad: quien manda es la fecha.
    """
    log_id = await log_scraping_start("ocds_oece")
    corte = fechas.ahora() - timedelta(days=dias_atras)
    # Los umbrales se leen UNA vez por pasada, no por licitacion: salen de un
    # percentil sobre toda la tabla y no cambian a mitad del recorrido.
    umbrales = await umbrales_por_tipo()
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
                    lic["banderas"], lic["banderas_nivel"] = calcular(lic, umbrales)
                    encontradas += 1

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
        log.exception(f"OCDS OECE fallo: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores, detalle_error)

    # NO PUDE ENTRAR NO ES LO MISMO QUE NO HAY NADA
    #
    #   Devolver [] tras un fallo hace que el orquestador lo cuente como cero y
    #   que el reporte diario escriba "Sin nuevas": indistinguible de un dia
    #   tranquilo. Asi es como esta fuente -- la UNICA con convocatorias
    #   vigentes -- estuvo doce corridas caida sin que el reporte lo dijera. El
    #   403 quedaba anotado en el log de scraping, que no mira nadie.
    #
    #   Relanzar hace que el orquestador la marque con -1 y salga "Error", que
    #   es lo que de verdad paso.
    #
    #   Solo si NO se parseo ni un release: si hubo algunos y luego fallo una
    #   pagina, se devuelve lo conseguido. Media pasada buena vale mas que una
    #   excepcion, y el fallo parcial ya queda en el log.
    if errores and encontradas == 0:
        raise RuntimeError(
            f"OCDS OECE inalcanzable, ningun release obtenido: {detalle_error}"
        )

    log.info(
        f"OCDS OECE: {len(vistos)} procesos revisados, {encontradas} parseados, "
        f"{len(nuevas)} nuevas"
    )
    return nuevas
