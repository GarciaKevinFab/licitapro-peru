"""Scraper OCDS — descarga XLSX de convocatorias desde CONOSCE/OSCE.

Fuente: https://conosce.osce.gob.pe/buscador/assets/.../convocatorias/{year}/
Datos: XLSX con todas las convocatorias del anio (publicadas por OSCE via SEACE).
Estructura: codigoentidad, entidad, proceso, objetocontractual, montoreferencial,
            departamento_item, fecha_convocatoria, etc.

Estrategia:
1. Descarga el XLSX del anio actual (y opcionalmente el anterior)
2. Parsea fila por fila usando openpyxl en modo read_only (streaming)
3. Filtra por: departamento, keywords, monto segun config del usuario
4. Upsert nuevas licitaciones a la BD
5. Cache local: guarda el archivo en disco y chequea Content-Length antes de re-descargar
"""
import logging
import hashlib
import os
import io
from datetime import datetime, date
from pathlib import Path

import httpx
import openpyxl
from tenacity import retry, stop_after_attempt, wait_exponential

from shared.db import upsert_licitacion, log_scraping_start, log_scraping_end, get_config

log = logging.getLogger("radar.ocds")

# URL directa al XLSX de convocatorias por anio
CONVOCATORIAS_URL = (
    "https://conosce.osce.gob.pe/buscador/assets/67ae6c4a"
    "/reportes/convocatorias/{year}/CONOSCE_CONVOCATORIAS{year}_0.xlsx"
)

CACHE_DIR = Path(os.getenv("CACHE_DIR", "/tmp/licitapro_cache"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
}

# Columnas esperadas en el XLSX (indice base-0)
COL_ENTIDAD = 2
COL_TIPO_ENTIDAD = 3
COL_CODIGO_CONV = 4
COL_DESC_PROCESO = 5
COL_PROCESO = 6       # nomenclatura tipo "AS-SM-8-2025-MDP/CS-1"
COL_TIPO_COMPRA = 7
COL_OBJETO_CONTRACTUAL = 8
COL_TIPO_PROC_SELECCION = 11
COL_MONTO_REF = 12
COL_DESC_ITEM = 14
COL_ESTADO_ITEM = 16
COL_MONTO_ITEM = 20
COL_MONEDA = 21
COL_DEPTO_ITEM = 22
COL_FECHA_CONV = 25
COL_FECHA_PROPUESTA = 27

# Mapeo de departamento XLSX -> formato config del usuario
DEPTOS_MAP = {
    "MADRE DE DIOS": "Madre de Dios", "CUSCO": "Cusco", "JUNIN": "Junín",
    "JUNÍN": "Junín", "LIMA": "Lima", "AREQUIPA": "Arequipa", "PUNO": "Puno",
    "PIURA": "Piura", "LA LIBERTAD": "La Libertad", "LAMBAYEQUE": "Lambayeque",
    "LORETO": "Loreto", "UCAYALI": "Ucayali", "ANCASH": "Áncash",
    "ÁNCASH": "Áncash", "CAJAMARCA": "Cajamarca", "HUANUCO": "Huánuco",
    "HUÁNUCO": "Huánuco", "ICA": "Ica", "TACNA": "Tacna",
    "MOQUEGUA": "Moquegua", "TUMBES": "Tumbes", "AMAZONAS": "Amazonas",
    "PASCO": "Pasco", "AYACUCHO": "Ayacucho", "APURIMAC": "Apurímac",
    "APURÍMAC": "Apurímac", "HUANCAVELICA": "Huancavelica",
    "SAN MARTIN": "San Martín", "SAN MARTÍN": "San Martín",
    "CALLAO": "Callao",
}


def _normalize_depto(raw: str | None) -> str | None:
    """Normaliza el departamento del XLSX al formato del config."""
    if not raw:
        return None
    upper = raw.strip().upper()
    return DEPTOS_MAP.get(upper)


def _parse_monto(val) -> float | None:
    """Parsea monto referencial (puede ser float, str, o None)."""
    if val is None:
        return None
    try:
        v = float(val)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _parse_fecha(val) -> datetime | None:
    """Parsea fecha del XLSX (puede ser datetime, str, o None)."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime.combine(val, datetime.min.time())
    text = str(val).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _detect_tipo(nomenclatura: str) -> str:
    """Detecta tipo de procedimiento de la nomenclatura."""
    n = (nomenclatura or "").upper()
    if "LP-" in n or "LICITACION" in n:
        return "LP"
    if "AS-" in n or "ADJUDICACION" in n:
        return "AS"
    if "SIE-" in n or "SUBASTA" in n:
        return "SIE"
    if "CP-" in n or "CP " in n or "CONCURSO" in n:
        return "CP"
    if "CD-" in n or "CONTRATACION DIRECTA" in n:
        return "CD"
    if "COMPARACION" in n or "CDP" in n or "CdP" in n:
        return "CdP"
    if "CONV-" in n or "CONVENIO" in n:
        return "otro"
    return "otro"


def _gen_id(codigo_conv: str, proceso: str) -> str:
    """Genera ID unico para la convocatoria."""
    raw = f"ocds_{codigo_conv}_{proceso}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _match_keywords(texto: str, keywords: list[str]) -> bool:
    # Delega en el helper compartido, que ignora tildes: las fuentes de OSCE
    # traen la acentuacion corrupta y el match con tilde nunca ocurria.
    from shared.config import match_keywords
    return match_keywords(texto, keywords)


async def _download_xlsx(client: httpx.AsyncClient, year: int) -> bytes | None:
    """Descarga el XLSX del anio. Usa cache local si el tamano no cambio."""
    url = CONVOCATORIAS_URL.format(year=year)
    cache_file = CACHE_DIR / f"convocatorias_{year}.xlsx"
    cache_size_file = CACHE_DIR / f"convocatorias_{year}.size"

    # Primero hacer HEAD para ver el tamano
    try:
        head = await client.head(url)
        if head.status_code != 200:
            log.warning(f"OCDS: HEAD {url} returned {head.status_code}")
            return None
        remote_size = int(head.headers.get("content-length", 0))
    except Exception as e:
        log.warning(f"OCDS: HEAD failed for {url}: {e}")
        remote_size = 0

    # Verificar cache
    if cache_file.exists() and cache_size_file.exists():
        try:
            cached_size = int(cache_size_file.read_text().strip())
            if remote_size > 0 and cached_size == remote_size:
                log.info(f"OCDS: Using cached file for {year} ({remote_size} bytes)")
                return cache_file.read_bytes()
        except (ValueError, OSError):
            pass

    # Descargar
    log.info(f"OCDS: Downloading convocatorias {year} from {url}")
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            log.warning(f"OCDS: GET {url} returned {resp.status_code}")
            return None
        data = resp.content
        log.info(f"OCDS: Downloaded {len(data)} bytes for {year}")
    except Exception as e:
        log.error(f"OCDS: Download failed for {year}: {e}")
        return None

    # Guardar en cache
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
        cache_size_file.write_text(str(len(data)))
    except OSError as e:
        log.warning(f"OCDS: Cache write failed: {e}")

    return data


def _parse_xlsx_rows(xlsx_data: bytes, config: dict) -> list[dict]:
    """Parsea el XLSX y retorna licitaciones que pasan los filtros."""
    keywords = config.get("keywords", [])
    regiones = config.get("regiones", [])
    monto_min = config.get("monto_min", 0) or 0
    monto_max = config.get("monto_max", 999999999) or 999999999

    results = []
    seen_procesos = set()  # Deduplicar por proceso (un proceso puede tener N items)

    wb = openpyxl.load_workbook(io.BytesIO(xlsx_data), read_only=True)
    ws = wb.active

    row_num = 0
    for row in ws.iter_rows(values_only=True):
        row_num += 1
        if row_num == 1:
            continue  # Saltar header

        # Extraer columnas con proteccion de indice
        cols = list(row) + [None] * 30  # Padding para evitar IndexError

        entidad = str(cols[COL_ENTIDAD] or "").strip()
        tipo_entidad = str(cols[COL_TIPO_ENTIDAD] or "").strip()
        codigo_conv = str(cols[COL_CODIGO_CONV] or "").strip()
        desc_proceso = str(cols[COL_DESC_PROCESO] or "").strip()
        proceso = str(cols[COL_PROCESO] or "").strip()
        objeto = str(cols[COL_OBJETO_CONTRACTUAL] or "").strip()
        tipo_proc = str(cols[COL_TIPO_PROC_SELECCION] or "").strip()
        monto_raw = cols[COL_MONTO_REF]
        desc_item = str(cols[COL_DESC_ITEM] or "").strip()
        estado_item = str(cols[COL_ESTADO_ITEM] or "").strip()
        depto_raw = str(cols[COL_DEPTO_ITEM] or "").strip() if cols[COL_DEPTO_ITEM] else None
        fecha_conv_raw = cols[COL_FECHA_CONV]
        fecha_prop_raw = cols[COL_FECHA_PROPUESTA]

        # Saltar si ya procesamos este proceso (evitar duplicados por items)
        if proceso in seen_procesos:
            continue
        seen_procesos.add(proceso)

        # Datos basicos requeridos
        if not entidad or not (desc_proceso or desc_item):
            continue

        monto = _parse_monto(monto_raw)
        depto = _normalize_depto(depto_raw)

        # FILTROS
        # 1. Departamento
        if regiones and depto and depto not in regiones:
            continue

        # 2. Monto
        if monto and (monto < monto_min or monto > monto_max):
            continue

        # 3. Keywords
        texto_busqueda = f"{desc_proceso} {desc_item} {entidad} {objeto}"
        if keywords and not _match_keywords(texto_busqueda, keywords):
            continue

        # Construir objeto de licitacion
        lid = _gen_id(codigo_conv, proceso)
        tipo = _detect_tipo(proceso + " " + tipo_proc)
        fecha_pub = _parse_fecha(fecha_conv_raw)
        fecha_cierre = _parse_fecha(fecha_prop_raw)

        # URL al detalle en CONOSCE
        url = f"https://conosce.osce.gob.pe/buscador/detalle?convocatoria={codigo_conv}"

        results.append({
            "id": lid,
            "fuente": "ocds_conosce",
            "tipo": tipo,
            "nomenclatura": proceso,
            "entidad": entidad,
            "entidad_tipo": tipo_entidad.lower() if tipo_entidad else None,
            "objeto": desc_proceso or desc_item,
            "descripcion": desc_item,
            "monto_referencial": monto,
            "moneda": "PEN",
            "departamento": depto,
            "url": url,
            "estado": _normalize_estado(estado_item),
            "fecha_publicacion": fecha_pub,
            "fecha_cierre": fecha_cierre,
        })

    wb.close()
    return results


def _normalize_estado(estado: str) -> str:
    """Normaliza el estado del item al formato interno."""
    if not estado:
        return "convocado"
    e = estado.upper()
    if "CONTRATADO" in e:
        return "adjudicado"
    if "DESIERTO" in e:
        return "desierto"
    if "NULO" in e or "CANCELADO" in e:
        return "cancelado"
    return "convocado"


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=5, max=30))
async def scrape_ocds(user_id: int = 0) -> list[dict]:
    """Descarga XLSX de convocatorias de CONOSCE/OSCE y retorna nuevas licitaciones.

    Descarga el anio actual. Si estamos en enero-febrero, tambien descarga
    el anio anterior para capturar convocatorias recientes.
    """
    log_id = await log_scraping_start("ocds_conosce")
    config = await get_config(user_id)
    if not config:
        config = {}

    nuevas = []
    encontradas = 0
    errores = 0
    error_detalle = None

    try:
        async with httpx.AsyncClient(
            timeout=120, headers=HEADERS, follow_redirects=True
        ) as client:
            current_year = date.today().year
            years = [current_year]
            # En enero/febrero tambien revisamos el anio anterior
            if date.today().month <= 2:
                years.append(current_year - 1)

            for year in years:
                try:
                    xlsx_data = await _download_xlsx(client, year)
                    if not xlsx_data:
                        log.warning(f"OCDS: No data for year {year}")
                        continue

                    parsed = _parse_xlsx_rows(xlsx_data, config)
                    encontradas += len(parsed)
                    log.info(
                        f"OCDS: {len(parsed)} licitaciones matched filters for {year}"
                    )

                    for lic in parsed:
                        try:
                            is_new = await upsert_licitacion(lic)
                            if is_new:
                                nuevas.append(lic)
                        except Exception as e:
                            errores += 1
                            log.error(f"OCDS: upsert error: {e}")

                except Exception as e:
                    errores += 1
                    error_detalle = str(e)[:200]
                    log.error(f"OCDS: Error processing year {year}: {e}")

    except Exception as e:
        errores += 1
        error_detalle = str(e)[:200]
        log.error(f"OCDS scraping failed: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores, error_detalle)
    log.info(
        f"OCDS: {encontradas} encontradas, {len(nuevas)} nuevas, {errores} errores"
    )
    return nuevas
