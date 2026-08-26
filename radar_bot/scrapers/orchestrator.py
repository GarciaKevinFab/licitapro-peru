"""Orquestador de scrapers -- ejecuta todos los scrapers de forma modular.

Fuentes implementadas (verificadas):
0. OCDS OECE           -- API oficial contratacionesabiertas.oece.gob.pe (PRINCIPAL,
                          tiempo real, unica con convocatorias vigentes)
1. SEACE 3.0           -- Buscador publico oficial OSCE (JSF AJAX). Bloqueado por
                          reCAPTCHA v3: se mantiene por si cambia, hoy devuelve 0.
2. GORE Portals        -- Portales regionales de cotizacion (MDD, etc.)
3. Peru Compras        -- Catalogo electronico y acuerdos marco
4. Poder Judicial      -- Portal de contrataciones PJ
5. EsSalud             -- Portal de contrataciones hospitalarias
6. SBS                 -- Superintendencia de Banca y Seguros
7. Transparencia       -- Consulta amigable MEF + PAC
8. Municipalidades     -- Portales de gobiernos locales
9. OCDS/CONOSCE        -- XLSX convocatorias de conosce.osce.gob.pe (datos abiertos OSCE)
10. CONOSCE Contratos  -- XLSX contratos + PAC de conosce.osce.gob.pe (Pentaho BI)
11. Datos Abiertos     -- datosabiertos.gob.pe CKAN API + XLSX resources
"""
import re
import logging
import asyncio
import hashlib
from datetime import datetime, date
import httpx
from bs4 import BeautifulSoup
from shared.db import (
    upsert_licitacion, log_scraping_start, log_scraping_end, get_config
)

log = logging.getLogger("radar.orchestrator")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.5",
}

# ==================== Utilidades compartidas ====================

def _gen_id(fuente: str, *parts) -> str:
    raw = f"{fuente}_{'_'.join(str(p) for p in parts)}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _parse_fecha(text: str):
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S",
                "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_monto(text: str):
    # Delega en el parser compartido: el anterior arrancaba todo caracter no
    # numerico y convertia "COT-123-2026" en S/ 1,232,026.
    from shared.config import parse_monto
    return parse_monto(text)


DEPTOS = {
    "MADRE DE DIOS": "Madre de Dios", "CUSCO": "Cusco", "JUNIN": "Junín",
    "JUNÍN": "Junín", "LIMA": "Lima", "AREQUIPA": "Arequipa", "PUNO": "Puno",
    "PIURA": "Piura", "LA LIBERTAD": "La Libertad", "LAMBAYEQUE": "Lambayeque",
    "LORETO": "Loreto", "UCAYALI": "Ucayali", "ANCASH": "Áncash",
    "CAJAMARCA": "Cajamarca", "HUANUCO": "Huánuco", "HUÁNUCO": "Huánuco",
    "ICA": "Ica", "TACNA": "Tacna", "MOQUEGUA": "Moquegua",
    "TUMBES": "Tumbes", "AMAZONAS": "Amazonas", "PASCO": "Pasco",
    "AYACUCHO": "Ayacucho", "APURIMAC": "Apurímac", "APURÍMAC": "Apurímac",
    "HUANCAVELICA": "Huancavelica", "SAN MARTIN": "San Martín",
    "SAN MARTÍN": "San Martín", "CALLAO": "Callao",
    "TAMBOPATA": "Madre de Dios", "PUERTO MALDONADO": "Madre de Dios",
    "HUANCAYO": "Junín", "SATIPO": "Junín",
}


def _detectar_depto(texto: str) -> str | None:
    upper = texto.upper()
    for key, val in DEPTOS.items():
        if key in upper:
            return val
    return None


def _detectar_tipo_entidad(entidad: str) -> str:
    e = entidad.upper()
    if "GOBIERNO REGIONAL" in e or "GORE" in e:
        return "gore"
    if "MUNICIPALIDAD" in e:
        return "muni"
    if "MINISTERIO" in e:
        return "min"
    if "UNIVERSIDAD" in e:
        return "univ"
    if any(x in e for x in ["HOSPITAL", "ESSALUD", "RED DE SALUD", "SALUD"]):
        return "hosp"
    if any(x in e for x in ["PODER JUDICIAL", "CORTE SUPERIOR", "CORTE SUPREMA"]):
        return "pj"
    if any(x in e for x in ["EJERCITO", "MARINA", "FUERZA AEREA", "PNP", "POLICIA"]):
        return "ffaa"
    if "SBS" in e or "SUPERINTENDENCIA DE BANCA" in e:
        return "sbs"
    return "otro"


def _match_keywords(texto: str, keywords: list[str]) -> bool:
    # Delega en el helper compartido, que ignora tildes: las fuentes de OSCE
    # traen la acentuacion corrupta y el match con tilde nunca ocurria.
    from shared.config import match_keywords
    return match_keywords(texto, keywords)


def _detectar_tipo_proc(nomenclatura: str) -> str:
    n = nomenclatura.upper()
    if "LP-" in n or "LICITACION" in n:
        return "LP"
    if "AS-" in n or "ADJUDICACION SIMPLIFICADA" in n:
        return "AS"
    if "SIE-" in n or "SUBASTA" in n:
        return "SIE"
    if "CP-" in n or "CONCURSO" in n:
        return "CP"
    if "CD-" in n or "CONTRATACION DIRECTA" in n:
        return "CD"
    if "COMPARACION" in n or "CDP" in n:
        return "CdP"
    if "COTIZACION" in n:
        return "cotizacion"
    return "otro"


async def _get_filters(user_id: int) -> dict:
    config = await get_config(user_id)
    return {
        "keywords": config["keywords"] if config else [],
        "keywords_excluir": (config["keywords_excluir"] if config else []) or [],
        "regiones": config["regiones"] if config else [],
        "monto_min": config["monto_min"] if config else 0,
        "monto_max": config["monto_max"] if config else 999999999,
    }


# Noise words that indicate navigation items, not actual procurement content
NOISE_PATTERNS = [
    "portal de transparencia", "tv en vivo", "directorio telefonico",
    "imagen institucional", "servicios en linea", "documentos de transparencia",
    "gerencias y oficinas", "informacion institucional", "mapa del sitio",
    "mesa de partes", "libro de reclamaciones", "inicio", "home",
    "nosotros", "contacto", "facebook", "twitter", "youtube",
    "declaraciones juradas", "ordenanzas regionales", "resoluciones",
    "convocatoria cas", "convocatorias cas", "resultados cas",
    "directorio", "galeria", "noticias", "agenda", "eventos",
    "acceso a la informacion", "plan operativo", "presupuesto institucional",
    "rendicion de cuentas", "audiencia publica", "gestion por procesos",
]


def _apply_filters(entidad, objeto, monto, depto, filters) -> bool:
    """True = passes filters (should be processed)."""
    regiones = filters["regiones"]
    keywords = filters["keywords"]
    monto_min = filters["monto_min"]
    monto_max = filters["monto_max"]

    # Quality filter: reject items that are nav links or garbage
    if len(objeto) < 20:
        return False

    obj_lower = objeto.lower()
    if any(nw in obj_lower for nw in NOISE_PATTERNS):
        return False

    if regiones and depto and depto not in regiones:
        return False
    if monto and (monto < monto_min or monto > monto_max):
        return False
    if keywords and not _match_keywords(f"{objeto} {entidad}", keywords):
        return False
    # Exclusiones del usuario. Van DESPUES del match positivo: sirven para
    # matar falsos positivos por homonimia -- "servidores" matchea tanto un
    # servidor informatico como los servidores publicos de una entidad.
    excluir = filters.get("keywords_excluir") or []
    if excluir and _match_keywords(f"{objeto} {entidad}", excluir):
        return False
    return True


# ==================== ORQUESTADOR PRINCIPAL ====================

async def run_all_scrapers(user_id: int = 0) -> dict:
    """Ejecuta todos los scrapers disponibles. Si uno falla, los otros siguen."""
    results = {
        "timestamp": datetime.now().isoformat(),
        "total_nuevas": 0,
        "por_fuente": {},
        "errores": [],
    }

    scrapers = [
        # Fuente principal: unica que entrega convocatorias vigentes.
        ("ocds_oece", _run_ocds_oece),
        ("seace_3.0", _run_seace),
        ("gore_portals", _run_gore_portals),
        ("peru_compras", _run_peru_compras),
        ("poder_judicial", _run_poder_judicial),
        ("essalud", _run_essalud),
        ("sbs", _run_sbs),
        ("transparencia_mef", _run_transparencia_mef),
        ("municipalidades", _run_municipalidades),
        # ocds_conosce y conosce_contratos quedaron FUERA de las alertas: son
        # volcados XLSX con retraso y producian 0 convocatorias vigentes (291
        # filas, ninguna postulable). Sus datos con monto se migraron a
        # historico_precios, que es donde si valen: alimentan el estimador de
        # precios de prep_bot. Las funciones siguen definidas para reengancharlas
        # a esa tabla cuando toque.
        ("datos_abiertos", _run_datos_abiertos),
    ]

    for nombre, func in scrapers:
        try:
            nuevas = await func(user_id)
            count = len(nuevas) if nuevas else 0
            results["por_fuente"][nombre] = count
            results["total_nuevas"] += count
            log.info(f"[OK] {nombre}: {count} nuevas")
        except Exception as e:
            results["errores"].append(f"{nombre}: {str(e)[:120]}")
            results["por_fuente"][nombre] = -1
            log.error(f"[FAIL] {nombre}: {e}")

        await asyncio.sleep(2)

    # Puntuar lo recien capturado. Sin esto las licitaciones quedan con
    # score_viabilidad en NULL y el bot las lista por fecha, no por relevancia.
    try:
        from radar_bot.scorer import recalcular_scores_pendientes
        results["scoreadas"] = await recalcular_scores_pendientes()
    except Exception as e:
        results["errores"].append(f"scoring: {str(e)[:120]}")
        results["scoreadas"] = 0
        log.error(f"[FAIL] scoring: {e}")

    log.info(
        f"Scraping completo: {results['total_nuevas']} nuevas, "
        f"{results.get('scoreadas', 0)} scoreadas, "
        f"{len(results['errores'])} errores"
    )
    return results


# ==================== 1. SEACE 3.0 ====================

async def _run_seace(user_id):
    from radar_bot.scrapers.seace import scrape_seace
    return await scrape_seace(user_id)


# ==================== 2. GORE PORTALS ====================

# Registry of known GORE cotizaciones portals (verified working)
GORE_COTIZACIONES_PORTALS = {
    "Madre de Dios": {
        "url": "http://cotizaciones.regionmadrededios.gob.pe/",
        "type": "cotizaciones_app",
        "entidad": "Gobierno Regional de Madre de Dios",
    },
}

# Generic GORE portals (HTML scraping, lower reliability)
GORE_GENERIC_PORTALS = {
    "Junín": [
        "https://www.regionjunin.gob.pe/pagina/id/contrataciones_y_adquisiciones/",
    ],
    "Cusco": [
        "https://www.regioncusco.gob.pe/contrataciones/",
    ],
}


async def _scrape_gore_cotizaciones_app(
    client: httpx.AsyncClient, region: str, portal_info: dict, filters: dict
) -> tuple[int, int, list[dict]]:
    """Scrape a GORE cotizaciones web app (like regionmadrededios.gob.pe/cotizaciones).

    These portals have a structured table with columns:
    TIPO | ANO | NUM | RUBRO | CONCEPTO | FECHAS | ACCIONES

    Returns (encontradas, errores, nuevas).
    """
    url = portal_info["url"]
    entidad = portal_info["entidad"]
    encontradas = 0
    errores = 0
    nuevas = []

    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return 0, 1, []

        soup = BeautifulSoup(resp.text, "lxml")

        # Parse the main table
        table = soup.find("table")
        if not table:
            return 0, 0, []

        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 6:
                continue

            textos = [c.get_text(strip=True) for c in cells]

            # Expected format: TIPO | ANO | NUM | RUBRO | CONCEPTO | FECHAS | DETALLES
            tipo_bien = textos[0]  # BIENES or SERVICIOS
            anio = textos[1]
            numero = textos[2]
            rubro = textos[3]
            concepto = textos[4]
            fecha_raw = textos[5]

            # Validate: anio should be a 4-digit year
            if not (anio.isdigit() and len(anio) == 4):
                continue

            # Skip empty or too-short concepts
            if not concepto or len(concepto) < 10:
                continue

            encontradas += 1
            objeto = f"[{tipo_bien}] {concepto}"

            # Parse dates: "INI: 27/03/2026 FIN: 28/03/2026 15:00"
            fecha_cierre = None
            fecha_pub = None

            ini_match = re.search(r"INI:\s*(\d{2}/\d{2}/\d{4})", fecha_raw)
            if ini_match:
                fecha_pub = _parse_fecha(ini_match.group(1))

            fin_match = re.search(r"FIN:\s*(\d{2}/\d{2}/\d{4})", fecha_raw)
            if fin_match:
                fecha_cierre = _parse_fecha(fin_match.group(1))

            # Get the detail link
            link = row.find("a", href=True)
            href = link["href"] if link else ""
            if href and not href.startswith("http"):
                base = url.rstrip("/")
                href = f"{base}/{href.lstrip('/')}"

            # Apply filters
            monto = None  # MDD cotizaciones don't show monto in listing
            if not _apply_filters(entidad, objeto, monto, region, filters):
                continue

            lid = _gen_id("gore", f"{anio}-{numero}", region)
            licit = {
                "id": f"gore_{lid}",
                "fuente": "gore_portals",
                "tipo": "cotizacion",
                "nomenclatura": f"COT-{numero}-{anio}-GOREMAD",
                "entidad": entidad,
                "entidad_tipo": "gore",
                "objeto": objeto[:500],
                "monto_referencial": monto,
                "departamento": region,
                "fecha_publicacion": fecha_pub,
                "fecha_cierre": fecha_cierre,
                "url": href or url,
                "estado": "convocado",
            }
            is_new = await upsert_licitacion(licit)
            if is_new:
                nuevas.append(licit)

    except Exception as e:
        errores += 1
        log.warning(f"GORE {region} cotizaciones app: {e}")

    return encontradas, errores, nuevas


async def _scrape_gore_generic(
    client: httpx.AsyncClient, region: str, url: str, filters: dict
) -> tuple[int, int, list[dict]]:
    """Scrape a generic GORE contrataciones page (WordPress/static HTML).

    Looks for actual procurement links (not navigation/menu items).
    Uses strict filtering to avoid false positives.
    """
    encontradas = 0
    errores = 0
    nuevas = []
    entidad = f"Gobierno Regional de {region}"

    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return 0, 1, []

        soup = BeautifulSoup(resp.text, "lxml")

        # Strategy: look for tables with actual procurement data
        for table in soup.find_all("table"):
            rows = table.find_all("tr")[1:]  # Skip header
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue

                textos = [c.get_text(strip=True) for c in cells]

                # Build object text from cells, but validate it's procurement content
                objeto = " | ".join(t for t in textos if 5 < len(t) < 500)
                if len(objeto) < 25:
                    continue

                # Must contain at least one procurement keyword
                procurement_keywords = [
                    "adquisicion", "servicio", "contratacion", "suministro",
                    "consultoria", "obra", "bienes", "equipos", "sistema",
                    "mantenimiento", "alquiler", "arrendamiento", "instalacion",
                    "construccion", "mejoramiento", "implementacion",
                ]
                obj_lower = objeto.lower()
                if not any(kw in obj_lower for kw in procurement_keywords):
                    continue

                encontradas += 1
                monto = None
                fecha_cierre = None
                for t in textos:
                    if not monto:
                        monto = _parse_monto(t)
                    if not fecha_cierre:
                        fecha_cierre = _parse_fecha(t)

                if not _apply_filters(entidad, objeto, monto, region, filters):
                    continue

                link = row.find("a", href=True)
                href = link["href"] if link else ""
                if href and not href.startswith("http"):
                    href = f"{url.rstrip('/')}/{href.lstrip('/')}"

                lid = _gen_id("gore", objeto[:40], region)
                licit = {
                    "id": f"gore_{lid}",
                    "fuente": "gore_portals",
                    "tipo": "cotizacion",
                    "entidad": entidad,
                    "entidad_tipo": "gore",
                    "objeto": objeto[:500],
                    "monto_referencial": monto,
                    "departamento": region,
                    "fecha_cierre": fecha_cierre,
                    "url": href or url,
                    "estado": "convocado",
                }
                is_new = await upsert_licitacion(licit)
                if is_new:
                    nuevas.append(licit)

        # Also look for structured list items (cards, articles) that are actual cotizaciones
        items = soup.find_all(["article", "div", "li"], class_=re.compile(
            r"cotizacion|convocatoria|proceso|licitacion", re.IGNORECASE
        ))
        for item in items:
            titulo_el = item.find(["a", "h3", "h4", "h5", "strong"])
            if not titulo_el:
                continue
            objeto = titulo_el.get_text(strip=True)
            if len(objeto) < 20:
                continue

            # Must contain procurement keywords
            obj_lower = objeto.lower()
            procurement_keywords = [
                "adquisicion", "servicio", "contratacion", "cotizacion",
                "licitacion", "adjudicacion", "concurso",
            ]
            if not any(kw in obj_lower for kw in procurement_keywords):
                continue

            encontradas += 1
            link = item.find("a", href=True)
            href = link["href"] if link else ""
            if href and not href.startswith("http"):
                href = f"{url.rstrip('/')}/{href.lstrip('/')}"

            if not _apply_filters(entidad, objeto, None, region, filters):
                continue

            lid = _gen_id("gore", objeto[:40], region)
            licit = {
                "id": f"gore_{lid}",
                "fuente": "gore_portals",
                "tipo": "cotizacion",
                "entidad": entidad,
                "entidad_tipo": "gore",
                "objeto": objeto[:500],
                "departamento": region,
                "url": href or url,
                "estado": "convocado",
            }
            is_new = await upsert_licitacion(licit)
            if is_new:
                nuevas.append(licit)

    except Exception as e:
        errores += 1
        log.debug(f"GORE {region} generic ({url}): {e}")

    return encontradas, errores, nuevas


async def _run_gore_portals(user_id):
    """Portales de Gobiernos Regionales -- cotizaciones y contrataciones."""
    log_id = await log_scraping_start("gore_portals")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            # 1. Scrape dedicated cotizaciones apps (high quality)
            for region, portal_info in GORE_COTIZACIONES_PORTALS.items():
                if filters["regiones"] and region not in filters["regiones"]:
                    continue

                enc, err, new = await _scrape_gore_cotizaciones_app(
                    client, region, portal_info, filters
                )
                encontradas += enc
                errores += err
                nuevas.extend(new)
                await asyncio.sleep(1)

            # 2. Scrape generic GORE portals (lower quality, strict filtering)
            for region, urls in GORE_GENERIC_PORTALS.items():
                if filters["regiones"] and region not in filters["regiones"]:
                    continue

                for url in urls:
                    try:
                        enc, err, new = await _scrape_gore_generic(
                            client, region, url, filters
                        )
                        encontradas += enc
                        errores += err
                        nuevas.extend(new)
                    except Exception as e:
                        errores += 1
                        # Contar el fallo sin registrar la causa fue justo lo que
                        # mantuvo invisibles los bugs de esquema.
                        log.warning(f"fila descartada: {e}")
                    await asyncio.sleep(1)

    except Exception as e:
        errores += 1
        log.warning(f"GORE portals: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 3. PERU COMPRAS ====================

async def _run_peru_compras(user_id):
    """Peru Compras -- Catalogos Electronicos y Acuerdos Marco."""
    log_id = await log_scraping_start("peru_compras")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    try:
        async with httpx.AsyncClient(
            timeout=30, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            urls_to_try = [
                "https://www.perucompras.gob.pe/convocatorias.htm",
                "https://www.perucompras.gob.pe/subasta/listado.htm",
                "https://www.perucompras.gob.pe/acuerdomarco/listado.htm",
            ]

            for url in urls_to_try:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = BeautifulSoup(resp.text, "lxml")

                    # Buscar tablas con datos de convocatorias
                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]  # Skip header
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            objeto = " ".join(textos[:3])
                            if len(objeto) < 15:
                                continue

                            encontradas += 1
                            entidad = "Peru Compras - Central de Compras Publicas"
                            depto = _detectar_depto(objeto)
                            monto = None
                            for t in textos:
                                m = _parse_monto(t)
                                if m and m > 100:
                                    monto = m
                                    break

                            if not _apply_filters(entidad, objeto, monto, depto, filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://www.perucompras.gob.pe{href}"

                            lid = _gen_id("pc", objeto[:40], str(encontradas))
                            licit = {
                                "id": f"pc_{lid}",
                                "fuente": "peru_compras",
                                "tipo": "acuerdo_marco",
                                "entidad": entidad,
                                "entidad_tipo": "otro",
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": depto,
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"Peru Compras: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 4. PODER JUDICIAL ====================

async def _run_poder_judicial(user_id):
    """Portal del Poder Judicial -- contrataciones y adquisiciones."""
    log_id = await log_scraping_start("poder_judicial")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://cea.pj.gob.pe/convocatorias",
        "https://cea.pj.gob.pe/procesos",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = BeautifulSoup(resp.text, "lxml")

                    # Buscar tablas con procesos
                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            objeto = " | ".join(t for t in textos if len(t) > 5)
                            if len(objeto) < 20:
                                continue

                            encontradas += 1
                            entidad = "Poder Judicial del Peru"
                            depto = _detectar_depto(objeto)
                            monto = None
                            for t in textos:
                                m = _parse_monto(t)
                                if m and m > 100:
                                    monto = m
                                    break

                            if not _apply_filters(entidad, objeto, monto, depto, filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://cea.pj.gob.pe{href}"

                            lid = _gen_id("pj", objeto[:40])
                            licit = {
                                "id": f"pj_{lid}",
                                "fuente": "poder_judicial",
                                "tipo": _detectar_tipo_proc(objeto),
                                "entidad": entidad,
                                "entidad_tipo": "pj",
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": depto,
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"Poder Judicial: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 5. ESSALUD ====================

async def _run_essalud(user_id):
    """EsSalud -- Portal de contrataciones hospitalarias."""
    log_id = await log_scraping_start("essalud")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://www.essalud.gob.pe/contrataciones-y-adquisiciones/",
        "https://www.essalud.gob.pe/convocatorias/",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = BeautifulSoup(resp.text, "lxml")

                    # Tablas de procesos
                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 2:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            objeto = " | ".join(t for t in textos if len(t) > 5)
                            if len(objeto) < 20:
                                continue

                            encontradas += 1
                            monto = None
                            fecha_cierre = None
                            for t in textos:
                                if not monto:
                                    monto = _parse_monto(t)
                                if not fecha_cierre:
                                    fecha_cierre = _parse_fecha(t)

                            depto = _detectar_depto(objeto)
                            if not _apply_filters("EsSalud", objeto, monto, depto, filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://www.essalud.gob.pe{href}"

                            lid = _gen_id("essalud", objeto[:40])
                            licit = {
                                "id": f"essalud_{lid}",
                                "fuente": "essalud",
                                "tipo": _detectar_tipo_proc(objeto),
                                "entidad": "EsSalud",
                                "entidad_tipo": "hosp",
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": depto,
                                "fecha_cierre": fecha_cierre,
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"EsSalud: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 6. SBS ====================

async def _run_sbs(user_id):
    """SBS -- Superintendencia de Banca, Seguros y AFP."""
    log_id = await log_scraping_start("sbs")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://www.sbs.gob.pe/app/pp/AdquisicionesYContrataciones/Paginas/LicitacionConvocatoria.aspx",
        "https://www.sbs.gob.pe/app/pp/AdquisicionesYContrataciones/",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = BeautifulSoup(resp.text, "lxml")

                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 2:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            objeto = " | ".join(t for t in textos if len(t) > 5)
                            if len(objeto) < 20:
                                continue

                            encontradas += 1
                            monto = None
                            for t in textos:
                                m = _parse_monto(t)
                                if m and m > 100:
                                    monto = m
                                    break

                            if not _apply_filters("SBS", objeto, monto, "Lima", filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://www.sbs.gob.pe{href}"

                            lid = _gen_id("sbs", objeto[:40])
                            licit = {
                                "id": f"sbs_{lid}",
                                "fuente": "sbs",
                                "tipo": _detectar_tipo_proc(objeto),
                                "entidad": "Superintendencia de Banca, Seguros y AFP",
                                "entidad_tipo": "sbs",
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": "Lima",
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"SBS: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 7. TRANSPARENCIA MEF ====================

async def _run_transparencia_mef(user_id):
    """Consulta Amigable MEF y Portal de Transparencia."""
    log_id = await log_scraping_start("transparencia_mef")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://www.transparencia.gob.pe/contrataciones/pte_transparencia_contrataciones.aspx",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = BeautifulSoup(resp.text, "lxml")

                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            entidad = textos[0] if textos[0] else ""
                            objeto = textos[1] if len(textos) > 1 else ""
                            if not entidad or not objeto or len(objeto) < 20:
                                continue

                            encontradas += 1
                            monto = None
                            for t in textos[2:]:
                                m = _parse_monto(t)
                                if m:
                                    monto = m
                                    break

                            depto = _detectar_depto(f"{entidad} {objeto}")

                            if not _apply_filters(entidad, objeto, monto, depto, filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://www.transparencia.gob.pe{href}"

                            lid = _gen_id("transp", objeto[:40], entidad[:20])
                            licit = {
                                "id": f"transp_{lid}",
                                "fuente": "transparencia_mef",
                                "tipo": _detectar_tipo_proc(objeto),
                                "entidad": entidad,
                                "entidad_tipo": _detectar_tipo_entidad(entidad),
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": depto,
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"Transparencia MEF: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 8. MUNICIPALIDADES ====================

async def _run_municipalidades(user_id):
    """Portales de municipalidades -- contrataciones locales."""
    log_id = await log_scraping_start("municipalidades")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    munis = {
        "Madre de Dios": [
            ("Municipalidad Provincial de Tambopata", "https://www.munitambopata.gob.pe/contrataciones"),
        ],
        "Junín": [
            ("Municipalidad Provincial de Huancayo", "https://www.munihuancayo.gob.pe/contrataciones"),
        ],
        "Cusco": [
            ("Municipalidad Provincial de Cusco", "https://www.cusco.gob.pe/contrataciones/"),
        ],
    }

    try:
        async with httpx.AsyncClient(
            timeout=15, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for region, portales in munis.items():
                if filters["regiones"] and region not in filters["regiones"]:
                    continue

                for entidad_name, url in portales:
                    try:
                        resp = await client.get(url)
                        if resp.status_code != 200:
                            continue

                        soup = BeautifulSoup(resp.text, "lxml")

                        for table in soup.find_all("table"):
                            rows = table.find_all("tr")[1:]
                            for row in rows:
                                cells = row.find_all("td")
                                if len(cells) < 2:
                                    continue

                                textos = [c.get_text(strip=True) for c in cells]
                                objeto = " | ".join(t for t in textos if len(t) > 5)
                                if len(objeto) < 20:
                                    continue

                                # Must contain procurement keywords
                                obj_lower = objeto.lower()
                                prc_kw = [
                                    "adquisicion", "servicio", "contratacion",
                                    "suministro", "consultoria", "bienes", "obra",
                                ]
                                if not any(kw in obj_lower for kw in prc_kw):
                                    continue

                                encontradas += 1
                                monto = None
                                for t in textos:
                                    m = _parse_monto(t)
                                    if m and m > 50:
                                        monto = m
                                        break

                                if not _apply_filters(entidad_name, objeto, monto, region, filters):
                                    continue

                                lid = _gen_id("muni", objeto[:40], entidad_name[:20])
                                licit = {
                                    "id": f"muni_{lid}",
                                    "fuente": "municipalidades",
                                    "tipo": "cotizacion",
                                    "entidad": entidad_name,
                                    "entidad_tipo": "muni",
                                    "objeto": objeto[:500],
                                    "monto_referencial": monto,
                                    "departamento": region,
                                    "url": url,
                                    "estado": "convocado",
                                }
                                is_new = await upsert_licitacion(licit)
                                if is_new:
                                    nuevas.append(licit)

                    except Exception as e:
                        errores += 1
                        # Contar el fallo sin registrar la causa fue justo lo que
                        # mantuvo invisibles los bugs de esquema.
                        log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"Municipalidades: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 0. OCDS OECE (API oficial, tiempo real) ====================

async def _run_ocds_oece(user_id):
    from radar_bot.scrapers.ocds_oece import scrape_ocds_oece
    # Sin user_id: el pozo es compartido y se filtra al leer.
    return await scrape_ocds_oece()


# ==================== 9. OCDS CONOSCE (Convocatorias XLSX) ====================

async def _run_ocds_conosce(user_id):
    from radar_bot.scrapers.ocds_api import scrape_ocds
    return await scrape_ocds(user_id)


# ==================== 10. CONOSCE Contratos + PAC ====================

async def _run_conosce_contratos(user_id):
    from radar_bot.scrapers.conosce import scrape_conosce
    return await scrape_conosce(user_id)


# ==================== 11. Datos Abiertos ====================

async def _run_datos_abiertos(user_id):
    from radar_bot.scrapers.datos_abiertos import scrape_datos_abiertos
    return await scrape_datos_abiertos(user_id)


# ==================== FORMAT REPORT ====================

def format_scraping_report(results: dict) -> str:
    """Formatea resultados de scraping para Telegram."""
    lines = [f"<b>Reporte de Scraping</b> -- {results['timestamp'][:16]}\n"]

    fuente_labels = {
        "seace_3.0": "SEACE 3.0",
        "gore_portals": "GOREs Regionales",
        "peru_compras": "Peru Compras",
        "poder_judicial": "Poder Judicial",
        "essalud": "EsSalud",
        "sbs": "SBS",
        "transparencia_mef": "Transparencia MEF",
        "municipalidades": "Municipalidades",
        "ocds_conosce": "OCDS/CONOSCE",
        "conosce_contratos": "CONOSCE Contratos+PAC",
        "datos_abiertos": "Datos Abiertos",
    }

    for fuente, count in results["por_fuente"].items():
        label = fuente_labels.get(fuente, fuente)
        if count == -1:
            status = "Error"
        elif count == 0:
            status = "Sin nuevas"
        else:
            status = f"{count} nuevas"
        lines.append(f"  {label}: {status}")

    lines.append(f"\n<b>Total nuevas: {results['total_nuevas']}</b>")

    if results["errores"]:
        lines.append(f"Errores: {len(results['errores'])}")

    return "\n".join(lines)
