"""Contratos menores (<= 8 UIT) de TODO el pais, desde la herramienta digital
del OECE para contratos menores (Ley 32069).

Fuente: https://prod6.seace.gob.pe/buscador-publico/
API:    https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico

QUE ES Y POR QUE ES LA FUENTE QUE FALTABA

  Las compras por debajo de 8 UIT no pasan por SEACE clasico, asi que
  `ocds_oece` no las ve, y hasta hoy se cazaban portal a portal: el de Madre
  de Dios, el de Cusco, gob.pe. La Ley 32069 creo una herramienta unica del
  OECE donde las entidades publican sus requerimientos menores e invitan a
  cotizar. Al 12/03/2026 la usaban 356 entidades con 44.270 requerimientos
  (noticia del OECE); el 2026-10-07 el buscador publico devolvia 2.445
  requerimientos VIGENTES en las 25 regiones (Lima 714, Arequipa 281, La
  Libertad 195, Cusco 130, Junin 127 ... Tumbes 2).

  Y a diferencia de la API OCDS, esta RESPONDE AL VPS: comprobado el
  2026-10-07, 200 sin cabecera de sesion. Por eso corre en la pasada horaria
  del servidor y no en el puente.

COMO SE LEE

  El buscador (`/contrataciones/buscador`, GET, sin sesion) acepta anio,
  estado, departamento, palabra clave y paginacion de hasta 200 por pagina
  (`listar-departamento` da los codigos; sus comentarios de OpenAPI estan
  en el propio bundle de la aplicacion). Se recorre POR DEPARTAMENTO, y no
  todo el pais de una vez, porque la lista NO trae la region y el
  departamento es la columna por la que filtran las alertas: pedirlo por
  region es la unica forma de saberlo sin una peticion extra por fila.

  La lista trunca `nomEntidad` y `desObjetoContrato` a 38 caracteres
  ("INSTITUTO GEOLOGICO, MINERO Y METALURG"). El detalle
  (`/contrataciones/listar-completo?id_contrato=`) trae el nombre y el objeto
  enteros, la provincia/distrito y los items. Se pide SOLO para las filas que
  aun no estan en la base: la primera cosecha son ~2.400 peticiones (unos 40
  minutos, medido), y despues solo las nuevas del dia. Las ya guardadas solo
  se reescriben si cambio el plazo para cotizar; `refrescar_licitacion` no
  toca entidad ni objeto al reencontrar una fila, asi que el texto truncado
  de la lista nunca pisa al entero del detalle.

LO QUE NO TRAE

  Monto: la herramienta no publica valor referencial en la parte publica
  (`precioTotal` llega vacio). Se guarda NULL, no cero.
  Bases: el servicio de archivos publicos responde 404 sin sesion. La URL de
  la fila lleva al detalle publico, donde el proveedor ya logueado los ve.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from shared import fechas
from shared.config import DEPARTAMENTOS, normalizar
from shared.db import connection, log_scraping_end, log_scraping_start, refrescar_licitacion

log = logging.getLogger("radar.oece_menores")

FUENTE = "oece_menores"
API = "https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico"
PORTAL = "https://prod6.seace.gob.pe/buscador-publico"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": f"{PORTAL}/",
}

ESTADO_VIGENTE = 2          # maestras/listar-estados-contrato-cotizacion
PAGINA = 200                # maximo que acepta page_size (probado)
MAX_PAGINAS_POR_DEPTO = 10  # Lima tenia 714 vigentes: 4 paginas. 10 es tope de seguridad.
PAUSA = 0.15                # entre peticiones; es un servicio publico del Estado

# Codigos de `maestras/listar-departamento` el 2026-10-07: alfabetico, 1..25.
# Se verifica al arrancar contra la API y se avisa si cambio; no se adivina.
CODIGOS_DEPARTAMENTO = {
    1: "Amazonas", 2: "Áncash", 3: "Apurímac", 4: "Arequipa", 5: "Ayacucho",
    6: "Cajamarca", 7: "Callao", 8: "Cusco", 9: "Huancavelica", 10: "Huánuco",
    11: "Ica", 12: "Junín", 13: "La Libertad", 14: "Lambayeque", 15: "Lima",
    16: "Loreto", 17: "Madre de Dios", 18: "Moquegua", 19: "Pasco", 20: "Piura",
    21: "Puno", 22: "San Martín", 23: "Tacna", 24: "Tumbes", 25: "Ucayali",
}

# `nomObjetoContrato` -> la misma categoria que usa OCDS en `licitaciones.categoria`.
CATEGORIA = {"Bien": "goods", "Servicio": "services", "Obra": "works",
             "Consultoría de Obra": "works"}

_NOMBRE_APP = {normalizar(d): d for d in DEPARTAMENTOS}


def _fecha(valor: str | None) -> datetime | None:
    """'07/10/2026 13:04:34' (hora de Lima) -> datetime naive de Lima."""
    if not valor:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return fechas.desde_texto(valor.strip(), fmt)
        except ValueError:
            continue
    return None


def _id(id_contrato) -> str:
    return f"oecem_{id_contrato}"


def _tipo_entidad(entidad: str) -> str:
    # Mismo criterio que el resto de fuentes; importado aqui para no atar este
    # modulo al orquestador al cargar.
    from radar_bot.scrapers.orchestrator import _detectar_tipo_entidad
    return _detectar_tipo_entidad(entidad)


def parsear(item: dict, departamento: str, detalle: dict | None = None) -> dict | None:
    """Una fila del buscador (+ su detalle si lo hay) como licitacion."""
    id_contrato = item.get("idContrato")
    if not id_contrato:
        return None

    cab = (detalle or {}).get("uitContratoCompletoProjection") or {}
    entidad = (cab.get("nomEntidad") or item.get("nomEntidad") or "").strip()
    objeto = (cab.get("desObjetoContrato") or item.get("desObjetoContrato") or "").strip()
    if not entidad or not objeto:
        return None

    items = (detalle or {}).get("uitContratoItemProjectionList") or []
    # "LIMA/LIMA/SAN BORJA": departamento/provincia/distrito del primer item.
    ubicacion = (items[0].get("nomDistritoExt") or "") if items else ""
    partes = [p.strip() for p in ubicacion.split("/")] if ubicacion else []
    depto_detalle = _NOMBRE_APP.get(normalizar(partes[0])) if partes else None

    objeto_cat = cab.get("nomObjetoContrato") or item.get("nomObjetoContrato") or ""
    return {
        "id": _id(id_contrato),
        "fuente": FUENTE,
        "tipo": "CM",
        "nomenclatura": (cab.get("nroDescripcion") or item.get("desContratacion") or "")[:200],
        "entidad": entidad[:300],
        "entidad_tipo": _tipo_entidad(entidad),
        "entidad_ruc": None,
        "objeto": f"[{objeto_cat.upper()}] {objeto}"[:2000] if objeto_cat else objeto[:2000],
        "monto_referencial": None,
        "moneda": "PEN",
        # La region por la que se pidio la pagina manda: es el dato de la
        # entidad. El detalle solo la confirma o la rellena si faltara.
        "departamento": departamento or depto_detalle,
        "provincia": partes[1] if len(partes) > 1 else None,
        "fecha_publicacion": _fecha(cab.get("fecPublica") or item.get("fecPublica")),
        "fecha_cierre": _fecha(item.get("fecFinCotizacion")),
        "estado": "convocado",
        "url": f"{PORTAL}/detail/{id_contrato}",
        "bases_urls": [],
        "categoria": CATEGORIA.get(objeto_cat),
    }


_NO_GUARDADA = object()


async def _ya_guardadas(ids: list[str]) -> dict[str, datetime | None]:
    """id -> fecha_cierre guardada, para las filas que ya existen.

    Sirve para dos cosas: saber a cuales pedir el detalle (solo a las nuevas)
    y no reescribir las que no cambiaron. Hay ~2.400 vigentes en cada pasada
    y cada `refrescar_licitacion` son dos viajes a Supabase: escribirlas todas
    cada hora eran ocho minutos de pasada para no cambiar nada. Lo unico que
    puede moverse en una fila ya guardada es el plazo para cotizar, y eso se
    compara aqui antes de tocar la base.
    """
    if not ids:
        return {}
    async with connection() as conn:
        filas = await conn.fetch(
            "SELECT id, fecha_cierre FROM licitaciones WHERE id = ANY($1::text[])", ids)
    return {f["id"]: f["fecha_cierre"] for f in filas}


async def _get(cliente: httpx.AsyncClient, ruta: str, **params) -> dict | list | None:
    r = await cliente.get(f"{API}{ruta}", params=params or None)
    r.raise_for_status()
    return r.json()


async def _verificar_codigos(cliente: httpx.AsyncClient) -> None:
    """Si el OECE renumera los departamentos, mejor saberlo que etiquetar mal."""
    try:
        maestra = await _get(cliente, "/maestras/listar-departamento")
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo leer la maestra de departamentos: %s", e)
        return
    for fila in maestra or []:
        esperado = CODIGOS_DEPARTAMENTO.get(fila.get("id"))
        real = _NOMBRE_APP.get(normalizar(fila.get("nom") or ""))
        if esperado != real:
            log.error("CODIGO DE DEPARTAMENTO CAMBIO en OECE: %s -> %r (aqui %r). "
                      "Revisar CODIGOS_DEPARTAMENTO.", fila.get("id"), real, esperado)


async def scrape_oece_menores(user_id: int = 0) -> list[dict]:
    log_id = await log_scraping_start(FUENTE)
    anio = fechas.hoy().year
    nuevas: list[dict] = []
    encontradas = errores = 0
    paginas_vivas = 0
    detalle_error: str | None = None

    async with httpx.AsyncClient(timeout=45, headers=HEADERS, verify=False) as cliente:
        await _verificar_codigos(cliente)
        for codigo, departamento in CODIGOS_DEPARTAMENTO.items():
            for page in range(1, MAX_PAGINAS_POR_DEPTO + 1):
                try:
                    data = await _get(cliente, "/contrataciones/buscador", anio=anio,
                                      lista_estado_contrato=ESTADO_VIGENTE,
                                      codigo_departamento=codigo, campo_orden=1,
                                      orden=2, page=page, page_size=PAGINA)
                except Exception as e:  # noqa: BLE001
                    errores += 1
                    detalle_error = f"{departamento} pagina {page}: {str(e)[:120]}"
                    log.warning("OECE menores %s pagina %s: %s", departamento, page, e)
                    break
                paginas_vivas += 1
                filas = (data or {}).get("data") or []
                if not filas:
                    break

                ids = [_id(f.get("idContrato")) for f in filas if f.get("idContrato")]
                conocidas = await _ya_guardadas(ids)
                for item in filas:
                    encontradas += 1
                    detalle = None
                    if _id(item.get("idContrato")) not in conocidas:
                        try:
                            detalle = await _get(cliente, "/contrataciones/listar-completo",
                                                 id_contrato=item["idContrato"])
                            await asyncio.sleep(PAUSA)
                        except Exception as e:  # noqa: BLE001
                            errores += 1
                            log.debug("detalle %s: %s", item.get("idContrato"), e)
                    try:
                        lic = parsear(item, departamento, detalle)
                        if not lic:
                            continue
                        previa = conocidas.get(lic["id"], _NO_GUARDADA)
                        if previa is not _NO_GUARDADA and previa == lic["fecha_cierre"]:
                            continue   # ya guardada y sin cambios: no se toca
                        if await refrescar_licitacion(lic):
                            nuevas.append(lic)
                    except Exception as e:  # noqa: BLE001
                        errores += 1
                        log.error("fila %s: %s", item.get("idContrato"), e)

                total = ((data or {}).get("pageable") or {}).get("totalElements") or 0
                if page * PAGINA >= total:
                    break
                await asyncio.sleep(PAUSA)

    if paginas_vivas == 0:
        detalle_error = f"CAIDA -- ninguna consulta a {API} respondio | {detalle_error or ''}"
    elif encontradas == 0:
        detalle_error = ("SIN EXTRAER -- el buscador respondio y no devolvio ninguna "
                         "contratacion vigente: o cambio el JSON o cambio el filtro")
    if detalle_error:
        log.warning("%s: %s", FUENTE, detalle_error)
    await log_scraping_end(log_id, encontradas, len(nuevas), errores, detalle_error)
    log.info("OECE MENORES: %d vigentes, %d nuevas, %d errores",
             encontradas, len(nuevas), errores)
    return nuevas
