"""Orquestador de scrapers — ejecuta todos los scrapers de forma modular.

Fuentes implementadas:
1. SEACE 3.0        — Buscador público oficial OSCE
2. OSCE API OCDS    — API de contrataciones abiertas OSCE
3. Contratos ≤8UIT  — Contrataciones directas menores
4. Perú Compras     — Catálogo electrónico y acuerdos marco
5. CONOSCE/OSCE     — Portal de datos abiertos OSCE
6. Datos Abiertos   — datosabiertos.gob.pe datasets
7. GOREs            — Portales regionales de cotización
8. Poder Judicial   — sap.pj.gob.pe contrataciones
9. Transparencia    — Consulta amigable MEF + PAC
10. SBS             — Superintendencia de Banca y Seguros
11. EsSalud         — Portal de contrataciones hospitalarias
12. Municipalidades — SECIGRA, portales de gobiernos locales
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

# ──────────────────── Utilidades compartidas ────────────────────

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
    if not text:
        return None
    text = re.sub(r"[^\d.,]", "", str(text).strip())
    text = text.replace(",", "")
    try:
        v = float(text)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


DEPTOS = {
    "MADRE DE DIOS": "Madre de Dios", "CUSCO": "Cusco", "JUNÍN": "Junín",
    "JUNIN": "Junín", "LIMA": "Lima", "AREQUIPA": "Arequipa", "PUNO": "Puno",
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
    if any(x in e for x in ["EJÉRCITO", "MARINA", "FUERZA AÉREA", "PNP", "POLICÍA"]):
        return "ffaa"
    if "SBS" in e or "SUPERINTENDENCIA DE BANCA" in e:
        return "sbs"
    return "otro"


def _match_keywords(texto: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    texto_lower = texto.lower()
    return any(kw.lower() in texto_lower for kw in keywords)


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
        "regiones": config["regiones"] if config else [],
        "monto_min": config["monto_min"] if config else 0,
        "monto_max": config["monto_max"] if config else 999999999,
    }


def _apply_filters(entidad, objeto, monto, depto, filters) -> bool:
    """True = pasa los filtros (debe procesarse)."""
    regiones = filters["regiones"]
    keywords = filters["keywords"]
    monto_min = filters["monto_min"]
    monto_max = filters["monto_max"]

    # Filtro de calidad: descartar items que son solo nav links o basura
    if len(objeto) < 20:
        return False
    noise_words = [
        "portal de transparencia", "tv en vivo", "directorio telefónico",
        "imagen institucional", "servicios en línea", "documentos de transparencia",
        "gerencias y oficinas", "información institucional", "mapa del sitio",
        "mesa de partes", "libro de reclamaciones", "inicio", "home",
        "nosotros", "contacto", "facebook", "twitter", "youtube",
    ]
    obj_lower = objeto.lower()
    if any(nw in obj_lower for nw in noise_words):
        return False

    if regiones and depto and depto not in regiones:
        return False
    if monto and (monto < monto_min or monto > monto_max):
        return False
    if keywords and not _match_keywords(f"{objeto} {entidad}", keywords):
        return False
    return True


# ──────────────────── ORQUESTADOR PRINCIPAL ────────────────────

async def run_all_scrapers(user_id: int = 0) -> dict:
    """Ejecuta todos los scrapers disponibles. Si uno falla, los otros siguen."""
    results = {
        "timestamp": datetime.now().isoformat(),
        "total_nuevas": 0,
        "por_fuente": {},
        "errores": [],
    }

    scrapers = [
        ("seace_3.0", _run_seace),
        ("osce_api", _run_osce_api),
        ("contratos_menores", _run_contratos_menores),
        ("peru_compras", _run_peru_compras),
        ("gore_portals", _run_gore_portals),
        ("poder_judicial", _run_poder_judicial),
        ("essalud", _run_essalud),
        ("sbs", _run_sbs),
        ("transparencia_mef", _run_transparencia_mef),
        ("municipalidades", _run_municipalidades),
        ("conosce_osce", _run_conosce),
        ("datos_abiertos", _run_datos_abiertos),
    ]

    for nombre, func in scrapers:
        try:
            nuevas = await func(user_id)
            count = len(nuevas) if nuevas else 0
            results["por_fuente"][nombre] = count
            results["total_nuevas"] += count
            log.info(f"✅ {nombre}: {count} nuevas")
        except Exception as e:
            results["errores"].append(f"{nombre}: {str(e)[:120]}")
            results["por_fuente"][nombre] = -1
            log.error(f"❌ {nombre} falló: {e}")

        await asyncio.sleep(2)

    log.info(
        f"Scraping completo: {results['total_nuevas']} nuevas, "
        f"{len(results['errores'])} errores"
    )
    return results


# ──────────────────── 1. SEACE 3.0 ────────────────────

async def _run_seace(user_id):
    from radar_bot.scrapers.seace import scrape_seace
    return await scrape_seace(user_id)


# ──────────────────── 2. OSCE API (OCDS) ────────────────────

async def _run_osce_api(user_id):
    """OSCE API — contratacionesabiertas.osce.gob.pe y API SEACE."""
    log_id = await log_scraping_start("osce_api")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0
    hoy = date.today().isoformat()

    apis = [
        # OSCE OCDS API
        f"https://contratacionesabiertas.osce.gob.pe/api/v1/releases?releaseDate={hoy}&limit=50",
        # OSCE API alternativa
        "https://contratacionesabiertas.osce.gob.pe/api/v1/records?limit=50",
        # SEACE API pública (si existe)
        f"https://prod2.seace.gob.pe/seacebus-uiwd-pub/rest/buscadorPublico?fecha={hoy}",
    ]

    try:
        async with httpx.AsyncClient(timeout=25, headers=HEADERS, follow_redirects=True) as client:
            for api_url in apis:
                try:
                    resp = await client.get(api_url)
                    if resp.status_code != 200:
                        continue

                    data = resp.json()
                    releases = (
                        data.get("releases", []) or
                        data.get("records", []) or
                        data.get("data", []) or
                        (data if isinstance(data, list) else [])
                    )

                    for rel in releases[:50]:
                        try:
                            # OCDS format
                            tender = rel.get("tender", rel.get("compiledRelease", {}).get("tender", {}))
                            buyer = rel.get("buyer", rel.get("compiledRelease", {}).get("buyer", {}))
                            if not tender:
                                continue

                            entidad = buyer.get("name", rel.get("entidad", ""))
                            objeto = tender.get("title", tender.get("description", rel.get("objeto", "")))
                            if not entidad or not objeto:
                                continue

                            encontradas += 1
                            monto = _parse_monto(str(tender.get("value", {}).get("amount", "")))
                            depto = _detectar_depto(f"{entidad} {objeto}")

                            if not _apply_filters(entidad, objeto, monto, depto, filters):
                                continue

                            lid = _gen_id("osce", tender.get("id", rel.get("ocid", objeto[:30])), entidad)
                            licit = {
                                "id": f"osce_{lid}",
                                "fuente": "osce_api",
                                "tipo": _detectar_tipo_proc(tender.get("procurementMethodDetails", "")),
                                "nomenclatura": tender.get("id", ""),
                                "entidad": entidad,
                                "entidad_tipo": _detectar_tipo_entidad(entidad),
                                "objeto": objeto,
                                "monto_referencial": monto,
                                "moneda": tender.get("value", {}).get("currency", "PEN"),
                                "departamento": depto,
                                "fecha_publicacion": _parse_fecha(rel.get("date", "")),
                                "fecha_cierre": _parse_fecha(tender.get("tenderPeriod", {}).get("endDate", "")),
                                "url": f"https://contratacionesabiertas.osce.gob.pe/tender/{tender.get('id', '')}",
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)
                        except Exception:
                            errores += 1
                    if encontradas > 0:
                        break  # Ya encontró datos, no seguir con otras APIs
                except Exception:
                    continue

    except Exception as e:
        errores += 1
        log.warning(f"OSCE API: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores,
                           "" if encontradas > 0 else "APIs no respondieron con datos")
    return nuevas


# ──────────────────── 3. CONTRATOS MENORES ≤8 UIT ────────────────────

async def _run_contratos_menores(user_id):
    """Contrataciones directas ≤ 8 UIT del SEACE."""
    log_id = await log_scraping_start("contratos_menores")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml",
        "https://prod6.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml",
    ]

    try:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    soup = BeautifulSoup(resp.text, "lxml")

                    # Buscar tabla de resultados
                    filas = soup.find_all("tr", class_=re.compile(r"ui-widget-content"))
                    for fila in filas:
                        celdas = fila.find_all("td")
                        if len(celdas) < 4:
                            continue

                        textos = [c.get_text(strip=True) for c in celdas]
                        entidad = textos[1] if len(textos) > 1 else ""
                        objeto = textos[2] if len(textos) > 2 else ""
                        monto_txt = textos[3] if len(textos) > 3 else ""

                        if not entidad or not objeto:
                            continue
                        encontradas += 1
                        monto = _parse_monto(monto_txt)
                        depto = _detectar_depto(f"{entidad} {objeto}")

                        if not _apply_filters(entidad, objeto, monto, depto, filters):
                            continue

                        lid = _gen_id("cm", textos[0] or objeto[:30], entidad)
                        licit = {
                            "id": f"cm_{lid}",
                            "fuente": "contratos_menores",
                            "tipo": "contrato_menor",
                            "nomenclatura": textos[0],
                            "entidad": entidad,
                            "entidad_tipo": _detectar_tipo_entidad(entidad),
                            "objeto": objeto,
                            "monto_referencial": monto,
                            "departamento": depto,
                            "url": url,
                            "estado": "convocado",
                        }
                        is_new = await upsert_licitacion(licit)
                        if is_new:
                            nuevas.append(licit)
                    if encontradas > 0:
                        break
                except Exception:
                    errores += 1

    except Exception as e:
        errores += 1
        log.warning(f"Contratos menores: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── 4. PERÚ COMPRAS ────────────────────

async def _run_peru_compras(user_id):
    """Perú Compras — Catálogos Electrónicos y Acuerdos Marco."""
    log_id = await log_scraping_start("peru_compras")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    try:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            # Portal principal de convocatorias
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
                    tables = soup.find_all("table")
                    for table in tables:
                        rows = table.find_all("tr")[1:]  # Skip header
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            objeto = " ".join(textos[:3])
                            if len(objeto) < 10:
                                continue

                            encontradas += 1
                            entidad = "Perú Compras - Central de Compras Públicas"
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

                    # También buscar links/cards
                    cards = soup.find_all("div", class_=re.compile(r"card|convocatoria|item"))
                    for card in cards:
                        titulo = card.find(["h3", "h4", "h5", "a", "strong"])
                        if not titulo:
                            continue
                        objeto = titulo.get_text(strip=True)
                        if len(objeto) < 10:
                            continue

                        encontradas += 1
                        link = card.find("a", href=True)
                        href = link["href"] if link else ""
                        if href and not href.startswith("http"):
                            href = f"https://www.perucompras.gob.pe{href}"

                        lid = _gen_id("pc", objeto[:40])
                        licit = {
                            "id": f"pc_{lid}",
                            "fuente": "peru_compras",
                            "tipo": "catalogo_electronico",
                            "entidad": "Perú Compras",
                            "entidad_tipo": "otro",
                            "objeto": objeto[:500],
                            "departamento": _detectar_depto(objeto),
                            "url": href or url,
                            "estado": "convocado",
                        }
                        is_new = await upsert_licitacion(licit)
                        if is_new:
                            nuevas.append(licit)

                except Exception:
                    errores += 1

    except Exception as e:
        errores += 1
        log.warning(f"Perú Compras: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── 5. GORE PORTALS ────────────────────

async def _run_gore_portals(user_id):
    """Portales de Gobiernos Regionales — cotizaciones y contrataciones."""
    log_id = await log_scraping_start("gore_portals")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    # Portales de cotización y contratación por región
    gore_portals = {
        "Madre de Dios": [
            "http://cotizaciones.regionmadrededios.gob.pe/",
            "https://www.regionmadrededios.gob.pe/portal/contrataciones",
            "https://www.regionmadrededios.gob.pe/portal/convocatorias",
        ],
        "Junín": [
            "https://www.regionjunin.gob.pe/tema/convocatorias_cas/",
            "https://www.regionjunin.gob.pe/tema/contrataciones/",
            "https://www.regionjunin.gob.pe/portal/contrataciones",
        ],
        "Cusco": [
            "https://www.regioncusco.gob.pe/contrataciones/",
            "https://www.regioncusco.gob.pe/convocatorias/",
        ],
        "Lima": [
            "https://www.regionlima.gob.pe/contrataciones",
        ],
        "Arequipa": [
            "https://www.regionarequipa.gob.pe/contrataciones",
        ],
        "Puno": [
            "https://www.regionpuno.gob.pe/contrataciones",
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
            for region, urls in gore_portals.items():
                # Filtrar por regiones del usuario
                if filters["regiones"] and region not in filters["regiones"]:
                    continue

                for url in urls:
                    try:
                        resp = await client.get(url)
                        if resp.status_code != 200:
                            continue

                        soup = BeautifulSoup(resp.text, "lxml")

                        # Parsear tablas de cotizaciones
                        for table in soup.find_all("table"):
                            rows = table.find_all("tr")[1:]  # Skip header
                            for row in rows:
                                cells = row.find_all("td")
                                if len(cells) < 4:
                                    continue

                                textos = [c.get_text(strip=True) for c in cells]

                                # Detectar formato GORE MDD: TIPO|AÑO|NUM|RUBRO|CONCEPTO|FECHA|ACCIONES
                                if len(textos) >= 6 and textos[1].isdigit() and len(textos[1]) == 4:
                                    tipo_bien = textos[0]  # BIENES/SERVICIOS
                                    numero = textos[2]
                                    rubro = textos[3]
                                    concepto = textos[4]
                                    fecha_raw = textos[5]

                                    if not concepto or len(concepto) < 10:
                                        continue

                                    objeto = f"[{tipo_bien}] {concepto}"
                                    # Parse fechas: "INI:24/03/2026FIN:25/03/2026 17:00"
                                    fecha_cierre = None
                                    fin_match = re.search(r"FIN:(\d{2}/\d{2}/\d{4})", fecha_raw)
                                    if fin_match:
                                        fecha_cierre = _parse_fecha(fin_match.group(1))

                                    link = row.find("a", href=True)
                                    href = link["href"] if link else ""
                                    if href and not href.startswith("http"):
                                        base = url.rstrip("/")
                                        href = f"{base}/{href.lstrip('/')}"
                                else:
                                    # Formato genérico
                                    objeto = " | ".join(t for t in textos if len(t) > 5)
                                    if len(objeto) < 20:
                                        continue
                                    fecha_cierre = None
                                    for t in textos:
                                        if not fecha_cierre:
                                            fecha_cierre = _parse_fecha(t)
                                    link = row.find("a", href=True)
                                    href = link["href"] if link else ""
                                    if href and not href.startswith("http"):
                                        href = f"{url.rstrip('/')}/{href.lstrip('/')}"
                                    numero = ""

                                encontradas += 1
                                entidad = f"Gobierno Regional de {region}"
                                monto = None
                                for t in textos:
                                    m = _parse_monto(t)
                                    if m and m > 50:
                                        monto = m
                                        break

                                if not _apply_filters(entidad, objeto, monto, region, filters):
                                    continue

                                lid = _gen_id("gore", numero or objeto[:40], region)
                                licit = {
                                    "id": f"gore_{lid}",
                                    "fuente": "gore_portals",
                                    "tipo": "cotizacion",
                                    "nomenclatura": f"COT-{numero}-{region[:3].upper()}" if numero else "",
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

                        # También buscar listados, cards, links
                        items = soup.find_all(["li", "div", "article"],
                                              class_=re.compile(r"item|cotizacion|convocatoria|post|entry"))
                        for item in items:
                            titulo_el = item.find(["a", "h3", "h4", "h5", "strong"])
                            if not titulo_el:
                                continue
                            objeto = titulo_el.get_text(strip=True)
                            if len(objeto) < 10:
                                continue

                            encontradas += 1
                            link = item.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"{url.rstrip('/')}/{href.lstrip('/')}"

                            lid = _gen_id("gore", objeto[:40], region)
                            licit = {
                                "id": f"gore_{lid}",
                                "fuente": "gore_portals",
                                "tipo": "cotizacion",
                                "entidad": f"Gobierno Regional de {region}",
                                "entidad_tipo": "gore",
                                "objeto": objeto[:500],
                                "departamento": region,
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                    except Exception:
                        errores += 1
                        continue

    except Exception as e:
        errores += 1
        log.warning(f"GORE portals: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── 6. PODER JUDICIAL ────────────────────

async def _run_poder_judicial(user_id):
    """Portal del Poder Judicial — contrataciones y adquisiciones."""
    log_id = await log_scraping_start("poder_judicial")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://sap.pj.gob.pe/",
        "https://www.pj.gob.pe/wps/wcm/connect/CorteSuprema/s_Corte_Suprema_Inicio/as_inicio/as_enlaces_702/as_imagen_702_1",
        "https://www.pj.gob.pe/wps/wcm/connect/cij-juris/s_cij_jurisprudencia_nuevo/as_contrataciones/",
        "https://cea.pj.gob.pe/convocatorias",
        "https://cea.pj.gob.pe/procesos",
    ]

    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
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
                            if len(objeto) < 10:
                                continue

                            encontradas += 1
                            entidad = "Poder Judicial del Perú"
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
                                href = f"https://www.pj.gob.pe{href}"

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

                    # Links a convocatorias
                    for link in soup.find_all("a", href=True):
                        text = link.get_text(strip=True)
                        if any(kw in text.lower() for kw in
                               ["convocatoria", "licitación", "adjudicación", "contratación",
                                "adquisición", "proceso de selección"]):
                            encontradas += 1
                            lid = _gen_id("pj", text[:40])
                            licit = {
                                "id": f"pj_{lid}",
                                "fuente": "poder_judicial",
                                "tipo": _detectar_tipo_proc(text),
                                "entidad": "Poder Judicial del Perú",
                                "entidad_tipo": "pj",
                                "objeto": text[:500],
                                "departamento": _detectar_depto(text),
                                "url": link["href"] if link["href"].startswith("http")
                                       else f"https://www.pj.gob.pe{link['href']}",
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception:
                    errores += 1

    except Exception as e:
        errores += 1
        log.warning(f"Poder Judicial: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── 7. ESSALUD ────────────────────

async def _run_essalud(user_id):
    """EsSalud — Portal de contrataciones hospitalarias."""
    log_id = await log_scraping_start("essalud")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://www.essalud.gob.pe/contrataciones-y-adquisiciones/",
        "https://www.essalud.gob.pe/transparencia/contrataciones.html",
        "https://ww1.essalud.gob.pe/contrataciones/",
        "https://www.essalud.gob.pe/convocatorias/",
    ]

    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
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
                            if len(objeto) < 10:
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

                    # Links a documentos/convocatorias
                    for link in soup.find_all("a", href=True):
                        text = link.get_text(strip=True)
                        href = link["href"]
                        if any(kw in text.lower() for kw in
                               ["proceso", "convocatoria", "licitación", "adjudicación",
                                "bases", "contratación"]) and len(text) > 15:
                            encontradas += 1
                            lid = _gen_id("essalud", text[:40])
                            if not href.startswith("http"):
                                href = f"https://www.essalud.gob.pe{href}"
                            licit = {
                                "id": f"essalud_{lid}",
                                "fuente": "essalud",
                                "tipo": _detectar_tipo_proc(text),
                                "entidad": "EsSalud",
                                "entidad_tipo": "hosp",
                                "objeto": text[:500],
                                "departamento": _detectar_depto(text),
                                "url": href,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception:
                    errores += 1

    except Exception as e:
        errores += 1
        log.warning(f"EsSalud: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── 8. SBS ────────────────────

async def _run_sbs(user_id):
    """SBS — Superintendencia de Banca, Seguros y AFP."""
    log_id = await log_scraping_start("sbs")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://www.sbs.gob.pe/app/pp/AdquisicionesYContrataciones/Paginas/LicitacionConvocatoria.aspx",
        "https://www.sbs.gob.pe/app/pp/AdquisicionesYContrataciones/",
        "https://www.sbs.gob.pe/Portals/0/jer/SUB_PORTAL_LICITACIONES/",
    ]

    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
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
                            if len(objeto) < 10:
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

                except Exception:
                    errores += 1

    except Exception as e:
        errores += 1
        log.warning(f"SBS: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── 9. TRANSPARENCIA MEF ────────────────────

async def _run_transparencia_mef(user_id):
    """Consulta Amigable MEF y Portal de Transparencia."""
    log_id = await log_scraping_start("transparencia_mef")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://www.transparencia.gob.pe/contrataciones/pte_transparencia_contrataciones.aspx",
        "https://www.transparencia.gob.pe/contrataciones/pte_transparencia_contrataciones.aspx?id_entidad=&id_tema=38",
    ]

    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
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
                            if not entidad or not objeto:
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

                except Exception:
                    errores += 1

    except Exception as e:
        errores += 1
        log.warning(f"Transparencia MEF: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── 10. MUNICIPALIDADES ────────────────────

async def _run_municipalidades(user_id):
    """Portales de municipalidades — contrataciones locales."""
    log_id = await log_scraping_start("municipalidades")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    # Municipalidades clave en las regiones del usuario
    munis = {
        "Madre de Dios": [
            ("Municipalidad Provincial de Tambopata", "https://www.munitambopata.gob.pe/contrataciones"),
            ("Municipalidad Provincial de Tambopata", "https://www.munitambopata.gob.pe/convocatorias"),
        ],
        "Junín": [
            ("Municipalidad Provincial de Huancayo", "https://www.munihuancayo.gob.pe/contrataciones"),
            ("Municipalidad Provincial de Huancayo", "https://www.munihuancayo.gob.pe/portal/contrataciones"),
        ],
        "Cusco": [
            ("Municipalidad Provincial de Cusco", "https://www.cusco.gob.pe/contrataciones/"),
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
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
                                if len(objeto) < 10:
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

                    except Exception:
                        errores += 1

    except Exception as e:
        errores += 1
        log.warning(f"Municipalidades: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── 11. CONOSCE OSCE ────────────────────

async def _run_conosce(user_id):
    """OSCE CONOSCE — BI Portal con datos de contrataciones."""
    log_id = await log_scraping_start("conosce_osce")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    try:
        async with httpx.AsyncClient(timeout=25, headers=HEADERS, follow_redirects=True) as client:
            # Portal CONOSCE con API de indicadores
            urls = [
                "https://portal.osce.gob.pe/osce/conosce/convocatorias",
                "https://portal.osce.gob.pe/osce/conosce",
                "https://portal.osce.gob.pe/osce/content/oportunidades-de-negocio",
            ]

            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = BeautifulSoup(resp.text, "lxml")

                    # Vista de tablas
                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            entidad = textos[0] if textos else ""
                            objeto = textos[1] if len(textos) > 1 else ""
                            if not objeto:
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

                            lid = _gen_id("conosce", objeto[:40], entidad[:20])
                            licit = {
                                "id": f"conosce_{lid}",
                                "fuente": "conosce_osce",
                                "tipo": _detectar_tipo_proc(objeto),
                                "entidad": entidad,
                                "entidad_tipo": _detectar_tipo_entidad(entidad),
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": depto,
                                "url": url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                    # Buscar views-row (Drupal format)
                    items = soup.find_all("div", class_=re.compile(r"views-row"))
                    for item in items:
                        title_el = item.find(["h3", "h4", "a", "strong", "span"])
                        if not title_el:
                            continue
                        objeto = title_el.get_text(strip=True)
                        if len(objeto) < 10:
                            continue

                        encontradas += 1
                        link = item.find("a", href=True)
                        href = link["href"] if link else ""
                        if href and not href.startswith("http"):
                            href = f"https://portal.osce.gob.pe{href}"

                        lid = _gen_id("conosce", objeto[:40])
                        licit = {
                            "id": f"conosce_{lid}",
                            "fuente": "conosce_osce",
                            "tipo": _detectar_tipo_proc(objeto),
                            "entidad": "OSCE",
                            "entidad_tipo": "otro",
                            "objeto": objeto[:500],
                            "departamento": _detectar_depto(objeto),
                            "url": href or url,
                            "estado": "convocado",
                        }
                        is_new = await upsert_licitacion(licit)
                        if is_new:
                            nuevas.append(licit)

                except Exception:
                    errores += 1

    except Exception as e:
        errores += 1
        log.warning(f"CONOSCE: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── 12. DATOS ABIERTOS ────────────────────

async def _run_datos_abiertos(user_id):
    """datosabiertos.gob.pe — Datasets del Estado peruano."""
    log_id = await log_scraping_start("datos_abiertos")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    search_queries = [
        "licitacion",
        "contratacion publica",
        "convocatoria",
        "adquisicion",
    ]

    try:
        async with httpx.AsyncClient(timeout=25, headers=HEADERS, follow_redirects=True) as client:
            for q in search_queries:
                try:
                    resp = await client.get(
                        "https://www.datosabiertos.gob.pe/api/3/action/package_search",
                        params={"q": q, "rows": 20, "sort": "metadata_modified desc"}
                    )
                    if resp.status_code != 200:
                        continue

                    data = resp.json()
                    if not data.get("success"):
                        continue

                    results = data.get("result", {}).get("results", [])
                    for dataset in results:
                        title = dataset.get("title", "")
                        notes = dataset.get("notes", "")
                        org = dataset.get("organization", {}).get("title", "Gobierno del Perú")
                        modified = dataset.get("metadata_modified", "")

                        if not title:
                            continue

                        encontradas += 1
                        objeto = f"{title} — {notes[:200]}" if notes else title
                        depto = _detectar_depto(f"{org} {objeto}")

                        if not _apply_filters(org, objeto, None, depto, filters):
                            continue

                        # Check for downloadable resources (CSV, Excel con datos)
                        resources = dataset.get("resources", [])
                        urls_bases = [r.get("url", "") for r in resources if r.get("url")]

                        lid = _gen_id("datos", dataset.get("id", title[:30]))
                        licit = {
                            "id": f"datos_{lid}",
                            "fuente": "datos_abiertos",
                            "tipo": "dataset",
                            "entidad": org,
                            "entidad_tipo": _detectar_tipo_entidad(org),
                            "objeto": objeto[:500],
                            "departamento": depto,
                            "fecha_publicacion": _parse_fecha(modified[:10]) if modified else None,
                            "url": f"https://www.datosabiertos.gob.pe/dataset/{dataset.get('name', '')}",
                            "bases_urls": urls_bases[:5],
                            "estado": "convocado",
                        }
                        is_new = await upsert_licitacion(licit)
                        if is_new:
                            nuevas.append(licit)

                except Exception:
                    errores += 1

    except Exception as e:
        errores += 1
        log.warning(f"Datos Abiertos: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ──────────────────── FORMAT REPORT ────────────────────

def format_scraping_report(results: dict) -> str:
    """Formatea resultados de scraping para Telegram."""
    lines = [f"📊 **Reporte de Scraping** — {results['timestamp'][:16]}\n"]

    fuente_labels = {
        "seace_3.0": "🏛️ SEACE 3.0",
        "osce_api": "📡 OSCE API (OCDS)",
        "contratos_menores": "📋 Contratos ≤8 UIT",
        "peru_compras": "🛒 Perú Compras",
        "gore_portals": "🗺️ GOREs Regionales",
        "poder_judicial": "⚖️ Poder Judicial",
        "essalud": "🏥 EsSalud",
        "sbs": "🏦 SBS",
        "transparencia_mef": "📊 Transparencia MEF",
        "municipalidades": "🏘️ Municipalidades",
        "conosce_osce": "📈 CONOSCE OSCE",
        "datos_abiertos": "📂 Datos Abiertos",
    }

    for fuente, count in results["por_fuente"].items():
        label = fuente_labels.get(fuente, fuente)
        if count == -1:
            emoji = "❌"
            text = "Error"
        elif count == 0:
            emoji = "⚪"
            text = "Sin nuevas"
        else:
            emoji = "✅"
            text = f"{count} nuevas"
        lines.append(f"{emoji} {label}: {text}")

    lines.append(f"\n📈 **Total nuevas: {results['total_nuevas']}**")

    if results["errores"]:
        lines.append(f"⚠️ Errores: {len(results['errores'])}")

    return "\n".join(lines)
