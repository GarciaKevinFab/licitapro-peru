"""Scraper para datosabiertos.gob.pe — Portal de Datos Abiertos del Estado Peruano.

Usa la API CKAN del portal para descubrir datasets de contrataciones del OSCE
y de entidades individuales (municipalidades, gobiernos regionales, etc.).

Endpoints funcionales:
- package_list: lista todos los datasets (4000+)
- package_show: detalle de un dataset con sus recursos descargables
- group_list: lista grupos tematicos

Estrategia:
1. Usa package_list para obtener todos los nombres de datasets
2. Filtra por nombres relacionados a contrataciones/OSCE
3. Para cada dataset relevante, obtiene recursos descargables (XLSX, CSV)
4. Descarga y parsea los recursos del anio actual
5. Filtra por config del usuario y upsert a BD

Nota: Muchos datasets de OSCE en datosabiertos apuntan a bi.seace.gob.pe
(portal Pentaho de CONOSCE). Los recursos XLSX que se descargan directamente
desde datosabiertos.gob.pe son generalmente de entidades individuales.
"""
import hashlib
import io
import logging
import re
from datetime import date, datetime

import httpx
import openpyxl
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from shared.db import get_config, log_scraping_end, log_scraping_start, upsert_licitacion
from shared import fechas

log = logging.getLogger("radar.datos_abiertos")

# CKAN API base
CKAN_BASE = "https://www.datosabiertos.gob.pe/api/3/action"

# HTML search endpoint (Drupal, not CKAN search)
SEARCH_URL = "https://www.datosabiertos.gob.pe/search/type/dataset"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
}

# OSCE datasets on datosabiertos that have useful XLSX resources
# (discovered via testing the API)
KEY_OSCE_DATASETS = [
    "contratos-de-las-entidades-organismo-supervisor-de-las-contrataciones-del-estado-osce",
    "plan-anual-de-contrataciones-de-las-entidades-organismo-supervisor-de-las-contrataciones-del",
    "datos-de-la-adjudicaci%C3%B3n-organismo-supervisor-de-las-contrataciones-del-estado-osce",
    "proveedores-adjudicados-organismo-supervisor-de-las-contrataciones-del-estado-osce",
    "consorcios-adjudicados-organismo-supervisor-de-las-contrataciones-del-estado-osce",
    "%C3%B3rdenes-de-compra-yo-servicios-de-las-entidades-organismo-supervisor-de-las-contrataciones",
    "entidades-contratantes-organismo-supervisor-de-las-contrataciones-del-estado-osce-0",
]

# Mapeo de departamentos
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
    "TAMBOPATA": "Madre de Dios", "PUERTO MALDONADO": "Madre de Dios",
    "HUANCAYO": "Junín", "SATIPO": "Junín",
}


def _normalize_depto(raw: str | None) -> str | None:
    if not raw:
        return None
    return DEPTOS_MAP.get(raw.strip().upper())


def _detect_depto_from_text(texto: str) -> str | None:
    """Intenta detectar departamento desde el texto libre."""
    upper = texto.upper()
    for key, val in DEPTOS_MAP.items():
        if key in upper:
            return val
    return None


def _parse_monto(val) -> float | None:
    if val is None:
        return None
    try:
        v = float(str(val).replace(",", "").strip())
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _parse_fecha(val) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime.combine(val, datetime.min.time())
    text = str(val).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%Y-%m-%d",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return fechas.desde_texto(text, fmt)
        except ValueError:
            continue
    return None


def _detect_tipo(text: str) -> str:
    n = (text or "").upper()
    if "LICITACION" in n or "LP-" in n:
        return "LP"
    if "ADJUDICACION" in n or "AS-" in n:
        return "AS"
    if "SUBASTA" in n or "SIE" in n:
        return "SIE"
    if "CONCURSO" in n or "CP-" in n:
        return "CP"
    if "DIRECTA" in n or "CD-" in n:
        return "CD"
    if "COMPARACION" in n or "CATALOGO" in n:
        return "CdP"
    if "ORDEN" in n:
        return "CdP"
    return "otro"


def _gen_id(prefix: str, *parts) -> str:
    raw = f"{prefix}_{'_'.join(str(p) for p in parts)}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _match_keywords(texto: str, keywords: list[str]) -> bool:
    # Delega en el helper compartido, que ignora tildes: las fuentes de OSCE
    # traen la acentuacion corrupta y el match con tilde nunca ocurria.
    from shared.config import match_keywords
    return match_keywords(texto, keywords)


async def _fetch_package_list(client: httpx.AsyncClient) -> list[str]:
    """Obtiene la lista completa de datasets."""
    try:
        resp = await client.get(f"{CKAN_BASE}/package_list")
        if resp.status_code != 200:
            log.warning(f"CKAN package_list returned {resp.status_code}")
            return []
        data = resp.json()
        if not data.get("success"):
            return []
        return data["result"]
    except Exception as e:
        log.error(f"CKAN package_list failed: {e}")
        return []


async def _fetch_package_show(client: httpx.AsyncClient, name: str) -> dict | None:
    """Obtiene detalle de un dataset."""
    try:
        resp = await client.get(f"{CKAN_BASE}/package_show", params={"id": name})
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("success"):
            return None
        result = data["result"]
        # API returns list or dict depending on version
        if isinstance(result, list):
            return result[0] if result else None
        return result
    except Exception as e:
        log.error(f"CKAN package_show failed for {name}: {e}")
        return None


async def _discover_contratacion_datasets(
    client: httpx.AsyncClient,
) -> list[dict]:
    """Descubre datasets relevantes de contrataciones.

    Retorna lista de {name, title, resources: [{name, format, url}]}
    """
    all_packages = await _fetch_package_list(client)
    if not all_packages:
        return []

    # Filtrar por palabras clave en el nombre del dataset
    contrat_keywords = [
        "contrat", "licit", "adquisic", "compra", "orden",
        "osce", "seace", "pac-plan",
    ]
    relevant_names = [
        p for p in all_packages
        if any(kw in p.lower() for kw in contrat_keywords)
    ]

    log.info(f"DATOS_ABIERTOS: {len(relevant_names)} datasets with contratacion keywords")

    datasets = []
    # Limit to avoid too many API calls
    for name in relevant_names[:30]:
        pkg = await _fetch_package_show(client, name)
        if not pkg:
            continue

        resources = []
        for r in pkg.get("resources", []):
            fmt = (r.get("format") or "").lower()
            url = r.get("url", "")
            rname = r.get("name", "")

            # Solo descargar XLSX/CSV directos (no links a Pentaho dashboards)
            if fmt in ("csv", "xls", "xlsx") and "pentaho" not in url.lower():
                resources.append({
                    "name": rname,
                    "format": fmt,
                    "url": url,
                })

        if resources:
            datasets.append({
                "name": name,
                "title": pkg.get("title", name),
                "org": (pkg.get("organization") or {}).get("title", ""),
                "notes": pkg.get("notes", ""),
                "resources": resources,
                "modified": pkg.get("metadata_modified", ""),
            })

    return datasets


async def _scrape_html_search(
    client: httpx.AsyncClient,
) -> list[dict]:
    """Scrape la pagina de busqueda HTML de datosabiertos para datasets recientes.

    El portal usa Drupal/DKAN con vistas paginadas. Los resultados aparecen
    como h2 con links dentro de un div.view-dkan-datasets.
    """
    results = []
    seen_names = set()

    search_queries = [
        "contratacion",
        "licitacion",
        "ordenes compra",
    ]

    for query in search_queries:
        try:
            resp = await client.get(
                SEARCH_URL,
                params={"query": query, "sort_by": "changed"},
            )
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Los resultados estan en h2 con links a /dataset/{name}
            view = soup.find(class_="view-dkan-datasets")
            if not view:
                view = soup.find(id="main-wrapper") or soup

            for h2 in view.find_all("h2"):
                link = h2.find("a", href=True)
                if not link:
                    continue

                href = link["href"]
                title = h2.get_text(strip=True)

                # Extract dataset name from URL
                name_match = re.search(r"/dataset/([^/?]+)", href)
                dataset_name = name_match.group(1) if name_match else ""

                if not dataset_name or dataset_name in seen_names:
                    continue
                seen_names.add(dataset_name)

                full_url = href if href.startswith("http") else \
                    f"https://www.datosabiertos.gob.pe{href}"

                results.append({
                    "title": title,
                    "name": dataset_name,
                    "org": "",
                    "url": full_url,
                })

        except Exception as e:
            log.error(f"HTML search failed for '{query}': {e}")

    return results


async def _download_and_parse_xlsx(
    client: httpx.AsyncClient, url: str, config: dict
) -> list[dict]:
    """Descarga un XLSX y extrae licitaciones filtradas.

    Intenta parsear columnas comunes de contratacion.
    """
    keywords = config.get("keywords", [])
    regiones = config.get("regiones", [])
    monto_min = config.get("monto_min", 0) or 0
    monto_max = config.get("monto_max", 999999999) or 999999999

    try:
        resp = await client.get(url, follow_redirects=True)
        if resp.status_code != 200:
            return []
        if len(resp.content) < 500:
            return []
        # Skip very large files (> 50MB) to avoid memory issues
        if len(resp.content) > 50_000_000:
            log.warning(f"DATOS_ABIERTOS: Skipping large file ({len(resp.content)} bytes): {url}")
            return []
    except Exception as e:
        log.error(f"DATOS_ABIERTOS: Download failed: {e}")
        return []

    results = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(resp.content), read_only=True)
        ws = wb.active

        # Read header to understand column layout
        header = []
        header_map = {}
        row_num = 0

        for row in ws.iter_rows(values_only=True):
            row_num += 1
            cols = list(row) + [None] * 10

            if row_num == 1:
                header = [str(c or "").lower().strip() for c in cols]
                # Map known column names to indices
                for i, h in enumerate(header):
                    if "entidad" in h and "tipo" not in h and "codigo" not in h and "ruc" not in h:
                        header_map["entidad"] = i
                    elif "descripcion" in h and "proceso" in h:
                        header_map["desc_proceso"] = i
                    elif "descripcion" in h and "item" in h:
                        header_map["desc_item"] = i
                    elif "descripcion" in h and "desc_proceso" not in header_map:
                        header_map["descripcion"] = i
                    elif "objeto" in h:
                        header_map["objeto"] = i
                    elif "monto" in h and "referencial" in h:
                        header_map["monto"] = i
                    elif "monto" in h and "monto" not in header_map:
                        header_map["monto"] = i
                    elif "departamento" in h and "ent" not in h:
                        header_map["depto"] = i
                    elif "departamento" in h:
                        header_map["depto_ent"] = i
                    elif "proceso" in h and "tipo" not in h and "desc" not in h:
                        header_map["proceso"] = i
                    elif "nomenclatura" in h or "codigo" in h and "convocatoria" in h:
                        header_map["nomenclatura"] = i
                    elif "fecha" in h and ("convocatoria" in h or "publicacion" in h):
                        header_map["fecha_pub"] = i
                    elif "fecha" in h and ("cierre" in h or "propuesta" in h):
                        header_map["fecha_cierre"] = i
                    elif "estado" in h:
                        header_map["estado"] = i
                    elif h == "tipoprocesoseleccion" or "tipo" in h and "seleccion" in h:
                        header_map["tipo_proc"] = i

                if "entidad" not in header_map:
                    log.info(f"DATOS_ABIERTOS: No 'entidad' column found in {url}")
                    wb.close()
                    return []
                continue

            if row_num > 10000:
                break  # Limit to avoid processing huge files

            # Extract values using header map
            # `cols=cols` ata la fila de ESTA vuelta en la definicion. Hoy da
            # igual -- la funcion se define y se llama dentro de la misma
            # iteracion --, pero sin esa atadura el closure leeria la variable
            # del bucle cuando se ejecuta y no cuando se define: el dia que
            # alguien guarde este `_get` para llamarlo mas tarde, todas las
            # copias devolverian los datos de la ULTIMA fila del fichero. Es un
            # fallo que no revienta, solo mezcla licitaciones.
            def _get(key, default="", cols=cols):
                idx = header_map.get(key)
                if idx is None:
                    return default
                val = cols[idx] if idx < len(cols) else None
                return str(val).strip() if val else default

            entidad = _get("entidad")
            desc = _get("desc_proceso") or _get("descripcion") or _get("objeto")
            desc_item = _get("desc_item")
            monto = _parse_monto(cols[header_map["monto"]] if "monto" in header_map else None)
            depto = _normalize_depto(_get("depto") or _get("depto_ent") or None)
            proceso = _get("proceso") or _get("nomenclatura")
            tipo_proc = _get("tipo_proc")
            fecha_pub_raw = cols[header_map["fecha_pub"]] if "fecha_pub" in header_map else None
            fecha_cierre_raw = cols[header_map["fecha_cierre"]] if "fecha_cierre" in header_map else None

            if not entidad or not desc:
                continue

            # Try to detect departamento from text if not in columns
            if not depto:
                depto = _detect_depto_from_text(f"{entidad} {desc}")

            # Filtros
            if regiones and depto and depto not in regiones:
                continue
            if monto and (monto < monto_min or monto > monto_max):
                continue
            texto = f"{desc} {desc_item} {entidad}"
            if keywords and not _match_keywords(texto, keywords):
                continue

            results.append({
                "id": _gen_id("datosgob", proceso or desc[:40], entidad[:30]),
                "fuente": "datos_abiertos",
                "tipo": _detect_tipo(proceso + " " + tipo_proc),
                "nomenclatura": proceso,
                "entidad": entidad,
                "entidad_tipo": None,
                "objeto": desc or desc_item,
                "descripcion": desc_item if desc_item != desc else None,
                "monto_referencial": monto,
                "moneda": "PEN",
                "departamento": depto,
                "url": url,
                "estado": "convocado",
                "fecha_publicacion": _parse_fecha(fecha_pub_raw),
                "fecha_cierre": _parse_fecha(fecha_cierre_raw),
            })

        wb.close()
    except Exception as e:
        log.error(f"DATOS_ABIERTOS: XLSX parse error for {url}: {e}")

    return results


def _build_catalog_entry(dataset: dict) -> list[dict]:
    # YA NO SE LLAMA, Y NO DEBE VOLVER A LLAMARSE PARA ALERTAS.
    #
    #   Lo que devuelve es la ficha de un dataset del catalogo, no una
    #   convocatoria: sin monto, sin fecha de cierre y con la entidad vacia.
    #   Insertarlo en `licitaciones` llenaba la tabla de filas invisibles y
    #   hacia que el parte anunciara novedades que ningun cliente podia ver.
    #
    #   Se conserva porque la transformacion en si es correcta y valdria para
    #   un catalogo de FUENTES; lo que estaba mal era el destino.
    """Construye entries de catalogo desde un dataset metadata (sin descargar XLSX).

    Util para datasets que solo tienen metadata y no recursos descargables.
    """
    title = dataset.get("title", "")
    org = dataset.get("org", "Estado Peruano")
    notes = dataset.get("notes", "")
    name = dataset.get("name", "")

    if not title:
        return []

    depto = _detect_depto_from_text(f"{org} {title} {notes}")

    return [{
        "id": _gen_id("datosgob_cat", name or title[:30]),
        "fuente": "datos_abiertos",
        "tipo": "otro",
        "nomenclatura": name,
        "entidad": org,
        "entidad_tipo": None,
        "objeto": f"{title}. {notes[:150]}" if notes else title,
        "descripcion": notes[:300] if notes else None,
        "monto_referencial": None,
        "moneda": "PEN",
        "departamento": depto,
        "url": f"https://www.datosabiertos.gob.pe/dataset/{name}" if name else "",
        "estado": "convocado",
        "fecha_publicacion": _parse_fecha(dataset.get("modified")),
        "fecha_cierre": None,
    }]


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=5, max=30))
async def scrape_datos_abiertos(user_id: int = 0) -> list[dict]:
    """Scrape datosabiertos.gob.pe CKAN API para licitaciones.

    1. Descubre datasets de contrataciones via CKAN package_list
    2. Descarga y parsea XLSX resources que sean archivos directos
    3. Tambien scrape la busqueda HTML para datasets recientes
    4. Filtra y upsert a BD
    """
    log_id = await log_scraping_start("datos_abiertos")
    config = await get_config(user_id)
    if not config:
        config = {}

    nuevas = []
    encontradas = 0
    errores = 0
    error_detalle = None
    # Se inicializa aqui y no donde se usa: el diagnostico del final lo lee
    # pase lo que pase, y si la pasada revienta antes de llegar al buscador
    # una variable sin definir convertiria un fallo de red en un NameError,
    # que apunta al sitio equivocado.
    catalogo = 0

    try:
        async with httpx.AsyncClient(
            timeout=60, headers=HEADERS, follow_redirects=True
        ) as client:

            # --- 1. CKAN API: descubrir datasets ---
            datasets = await _discover_contratacion_datasets(client)
            log.info(f"DATOS_ABIERTOS: {len(datasets)} datasets with downloadable resources")

            # --- 2. Descargar y parsear XLSX resources ---
            for ds in datasets:
                for resource in ds["resources"]:
                    url = resource["url"]
                    fmt = resource["format"]

                    if fmt not in ("xlsx", "xls"):
                        continue  # Solo soportamos XLSX por ahora

                    try:
                        parsed = await _download_and_parse_xlsx(client, url, config)
                        encontradas += len(parsed)

                        for lic in parsed:
                            try:
                                is_new = await upsert_licitacion(lic)
                                if is_new:
                                    nuevas.append(lic)
                            except Exception as e:
                                errores += 1
                                log.error(f"DATOS_ABIERTOS upsert error: {e}")

                    except Exception as e:
                        errores += 1
                        log.error(f"DATOS_ABIERTOS resource error ({url}): {e}")

            # --- 3. Buscador HTML: SOLO para saber si el portal responde ---
            #
            # UN DATASET DEL CATALOGO NO ES UNA CONVOCATORIA, Y SE ESTABA
            # GUARDANDO COMO SI LO FUERA
            #
            #   Este bloque cogia cada resultado del buscador -- que son
            #   FICHAS DE CATALOGO: "Ordenes de compra del GORE Ancash",
            #   "Lista de ordenes del PSI" -- y las insertaba en
            #   `licitaciones`. Comprobado en produccion: 17 filas asi, con
            #   `entidad` vacia, sin monto, sin fecha de cierre y con el
            #   departamento mal (una del Gobierno Regional del CALLAO quedo
            #   etiquetada como Ica).
            #
            #   Ninguna se ve: el panel filtra por `fecha_cierre > NOW()` y
            #   sin fecha eso es falso. Asi que la fuente aportaba CERO
            #   licitaciones visibles mientras el parte diario anunciaba "17
            #   nuevas", que se lee como una fuente sana. Es el mismo engano
            #   que el "Sin nuevas" de las fuentes caidas, al reves.
            #
            #   Y era una bomba de relojeria: el dia que alguien relajara ese
            #   filtro, los clientes verian "Lista de ordenes del PSI" como
            #   una licitacion a la que presentarse.
            #
            #   Se sigue consultando el buscador porque sirve para saber si el
            #   portal esta vivo, pero lo que devuelve ya no se guarda.
            try:
                search_results = await _scrape_html_search(client)
                catalogo = len(search_results)
                log.info(f"DATOS_ABIERTOS: {catalogo} fichas de catalogo "
                         f"(no se guardan: no son convocatorias)")
            except Exception as e:
                errores += 1
                log.error(f"DATOS_ABIERTOS HTML search error: {e}")

    except Exception as e:
        errores += 1
        error_detalle = str(e)[:200]
        log.error(f"DATOS_ABIERTOS scraping failed: {e}")

    # Sin esto la fuente volveria a mentir por omision: 0 encontradas y 0
    # errores es indistinguible de "hoy no habia convocatorias".
    if not error_detalle and encontradas == 0:
        error_detalle = (
            f"SIN EXTRAER -- ninguna convocatoria de los recursos XLSX. El "
            f"buscador devolvio {catalogo} fichas de catalogo, que no son "
            f"convocatorias y no se guardan.")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores, error_detalle)
    log.info(
        f"DATOS_ABIERTOS: {encontradas} encontradas, {len(nuevas)} nuevas, {errores} errores"
    )
    return nuevas
