"""Scraper para SEACE 3.0 -- Buscador Publico de Procedimientos de Seleccion.

SEACE uses PrimeFaces JSF with reCAPTCHA v3 for ALL search queries.
Without a valid reCAPTCHA token, the server silently ignores search requests.

Current approach:
1. Attempt the JSF AJAX POST anyway (in case reCAPTCHA enforcement changes).
2. Parse the AJAX XML response for any datatable rows.
3. If no results, log clearly that reCAPTCHA is blocking.

NOTE: To properly scrape SEACE, one of these approaches would be needed:
- Browser automation (Playwright/Selenium) that solves reCAPTCHA v3 naturally
- A reCAPTCHA solving service API
- Official SEACE data export/API (not currently available publicly)
For now, the GORE cotizaciones portals and other sources fill the gap.
"""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from shared.db import get_config, log_scraping_end, log_scraping_start, upsert_licitacion

log = logging.getLogger("radar.seace")

SEACE_URLS = [
    "https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml",
    "https://prod6.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.5",
}

AJAX_HEADERS = {
    "Faces-Request": "partial/ajax",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

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


def generar_id(nomenclatura: str, entidad: str) -> str:
    raw = f"{nomenclatura}_{entidad}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def parse_fecha(text: str) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S",
                "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_monto(text: str) -> float | None:
    from shared.config import parse_monto as _pm
    return _pm(text)


def detectar_departamento(texto: str) -> str | None:
    upper = texto.upper()
    for key, val in DEPTOS.items():
        if key in upper:
            return val
    return None


def detectar_tipo_entidad(entidad: str) -> str:
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
    return "otro"


def detectar_tipo_proc(nomenclatura: str) -> str:
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


def match_keywords(texto: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    texto_lower = texto.lower()
    return any(kw.lower() in texto_lower for kw in keywords)


def _extract_viewstate(html: str) -> str:
    """Extract javax.faces.ViewState from HTML."""
    match = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', html)
    return match.group(1) if match else ""


def _is_noise_row(textos: list[str]) -> bool:
    """Check if a table row is noise (form labels, empty states, nav items)."""
    combined = " ".join(textos).lower()
    noise = [
        "no se encontraron datos", "sin informacion", "seleccione",
        "nombre o sigla", "departamento", "distrito", "provincia",
        "objeto de contratacion", "descripcion del objeto",
        "modalidad de seleccion", "tipo de seleccion",
        "fecha de publicacion", "fecha de convocatoria",
    ]
    return any(n in combined for n in noise)


def _parse_jsf_xml_table(xml_text: str) -> list[dict]:
    """Parse PrimeFaces datatable rows from JSF AJAX XML response.

    SEACE returns partial XML with CDATA sections containing HTML.
    We extract and parse any actual data rows (not form/filter rows).
    """
    import warnings

    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

    results = []
    cdata_blocks = re.findall(r'<!\[CDATA\[(.*?)\]\]>', xml_text, re.DOTALL)

    for block in cdata_blocks:
        if 'ui-datatable' not in block and 'ui-widget-content' not in block:
            continue

        soup = BeautifulSoup(block, "lxml")
        rows = soup.find_all("tr", class_=re.compile(r"ui-widget-content"))

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            textos = [c.get_text(strip=True) for c in cells]

            # Skip noise rows (form labels, empty state messages)
            if _is_noise_row(textos):
                continue

            # Validate: at least one cell should have substantial text
            if not any(len(t) > 15 for t in textos):
                continue

            link = row.find("a", href=True)
            href = link["href"] if link else ""

            results.append({
                "textos": textos,
                "link": href,
            })

    return results


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=3, max=15))
async def scrape_seace(user_id: int = 0) -> list[dict]:
    """Scrape SEACE buscador publico via JSF AJAX.

    Tries multiple SEACE server instances (prod2, prod6).
    Attempts JSF AJAX search; if reCAPTCHA blocks (likely), returns empty.
    Returns list of new licitaciones matching user filters.
    """
    log_id = await log_scraping_start("seace_3.0")
    config = await get_config(user_id)
    keywords = config["keywords"] if config else []
    regiones = config["regiones"] if config else []
    monto_min = config["monto_min"] if config else 0
    monto_max = config["monto_max"] if config else 999999999

    nuevas = []
    encontradas = 0
    errores = 0

    hoy = date.today()
    fecha_inicio = (hoy - timedelta(days=7)).strftime("%d/%m/%Y")
    fecha_fin = hoy.strftime("%d/%m/%Y")

    for base_url in SEACE_URLS:
        if encontradas > 0:
            break

        try:
            async with httpx.AsyncClient(
                timeout=30, headers=HEADERS, follow_redirects=True, verify=False
            ) as client:
                # Step 1: GET the page to get cookies + ViewState
                resp = await client.get(base_url)
                if resp.status_code != 200:
                    continue
                html = resp.text
                viewstate = _extract_viewstate(html)
                if not viewstate:
                    log.warning(f"No ViewState from {base_url}")
                    continue

                # Step 2: Try JSF AJAX search (Procedimientos de Seleccion tab)
                form_id = "tbBuscador:idFormBuscarProceso"
                search_fields = {
                    f"{form_id}:anioConvocatoria_input": str(hoy.year),
                    f"{form_id}:anioConvocatoria_focus": "",
                    f"{form_id}:departamento_input": "",
                    f"{form_id}:departamento_focus": "",
                    f"{form_id}:distrito_input": "",
                    f"{form_id}:distrito_focus": "",
                    f"{form_id}:descripcionObjeto": "",
                    f"{form_id}:hddNumeroRuc": "",
                    f"{form_id}:nombreEntidad": "",
                    f"{form_id}:numeroConvocatoria": "",
                    f"{form_id}:numeroSeleccion": "",
                    f"{form_id}:CUI": "",
                    f"{form_id}:codigoSnip": "",
                    f"{form_id}:dfechaInicio_input": fecha_inicio,
                    f"{form_id}:dfechaFin_input": fecha_fin,
                    f"{form_id}:tokenBusProSel": "",
                    f"{form_id}:numPositionTabView": "0",
                    f"{form_id}:ipClienteIpify": "",
                }

                ajax_data = {
                    "javax.faces.partial.ajax": "true",
                    "javax.faces.source": f"{form_id}:btnBuscarSel",
                    "javax.faces.partial.execute": "@all",
                    "javax.faces.partial.render": f"{form_id}:pnlResultadosSeleccion",
                    "javax.faces.behavior.event": "action",
                    "javax.faces.partial.event": "click",
                    form_id: form_id,
                    f"{form_id}:btnBuscarSel": f"{form_id}:btnBuscarSel",
                    "javax.faces.ViewState": viewstate,
                    **search_fields,
                }

                headers = {**HEADERS, **AJAX_HEADERS, "Referer": base_url}

                try:
                    resp2 = await client.post(base_url, data=ajax_data, headers=headers)
                    if resp2.status_code == 200 and len(resp2.text) > 300:
                        rows = _parse_jsf_xml_table(resp2.text)
                        if rows:
                            log.info(f"SEACE AJAX returned {len(rows)} data rows")
                        for row_data in rows:
                            try:
                                textos = row_data["textos"]
                                nomenclatura = textos[0] if len(textos) > 0 else ""
                                entidad = textos[1] if len(textos) > 1 else ""
                                objeto = textos[2] if len(textos) > 2 else ""
                                monto_text = textos[3] if len(textos) > 3 else ""
                                fecha_pub_text = textos[4] if len(textos) > 4 else ""
                                fecha_cierre_text = textos[5] if len(textos) > 5 else ""

                                if not entidad or not objeto or len(objeto) < 10:
                                    continue

                                encontradas += 1
                                monto = parse_monto(monto_text)
                                depto = detectar_departamento(f"{entidad} {objeto}")

                                if regiones and depto and depto not in regiones:
                                    continue
                                if monto and (monto < monto_min or monto > monto_max):
                                    continue
                                if keywords and not match_keywords(f"{objeto} {entidad}", keywords):
                                    continue

                                url = row_data["link"]
                                if url and not url.startswith("http"):
                                    url = f"https://prod2.seace.gob.pe{url}"

                                lid = generar_id(nomenclatura or objeto[:30], entidad)
                                data = {
                                    "id": f"seace_{lid}",
                                    "fuente": "seace_3.0",
                                    "tipo": detectar_tipo_proc(nomenclatura),
                                    "nomenclatura": nomenclatura,
                                    "entidad": entidad,
                                    "entidad_tipo": detectar_tipo_entidad(entidad),
                                    "objeto": objeto[:500],
                                    "monto_referencial": monto,
                                    "moneda": "PEN",
                                    "departamento": depto,
                                    "fecha_publicacion": parse_fecha(fecha_pub_text),
                                    "fecha_cierre": parse_fecha(fecha_cierre_text),
                                    "url": url or base_url,
                                    "estado": "convocado",
                                }
                                is_new = await upsert_licitacion(data)
                                if is_new:
                                    nuevas.append(data)
                            except Exception as e:
                                errores += 1
                                log.debug(f"Error parsing SEACE row: {e}")
                except Exception as e:
                    log.debug(f"SEACE AJAX request failed: {e}")

        except Exception as e:
            errores += 1
            log.warning(f"SEACE {base_url}: {e}")

    detail = ""
    if encontradas == 0:
        detail = "reCAPTCHA v3 blocks search results. Requires browser automation or captcha service."
        log.info(f"SEACE: {detail}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores, detail)
    log.info(f"SEACE: {encontradas} encontradas, {len(nuevas)} nuevas, {errores} errores")
    return nuevas
