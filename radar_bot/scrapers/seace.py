"""Scraper para SEACE 3.0 — Buscador Público de Procedimientos de Selección."""
import re
import logging
import hashlib
from datetime import datetime
import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential
from shared.db import upsert_licitacion, log_scraping_start, log_scraping_end, get_config

log = logging.getLogger("radar.seace")

SEACE_URL = "https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9",
}


def generar_id(nomenclatura: str, entidad: str) -> str:
    raw = f"{nomenclatura}_{entidad}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def parse_fecha(text: str) -> datetime | None:
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_monto(text: str) -> float | None:
    text = re.sub(r"[^\d.,]", "", text.strip())
    text = text.replace(",", "")
    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def detectar_departamento(entidad: str, texto: str) -> str | None:
    """Intenta detectar el departamento de la entidad o el objeto."""
    deptos = {
        "MADRE DE DIOS": "Madre de Dios", "CUSCO": "Cusco",
        "JUNÍN": "Junín", "JUNIN": "Junín", "LIMA": "Lima",
        "AREQUIPA": "Arequipa", "PUNO": "Puno", "PIURA": "Piura",
        "LA LIBERTAD": "La Libertad", "LAMBAYEQUE": "Lambayeque",
        "LORETO": "Loreto", "UCAYALI": "Ucayali", "ANCASH": "Áncash",
        "CAJAMARCA": "Cajamarca", "HUANUCO": "Huánuco", "HUÁNUCO": "Huánuco",
        "ICA": "Ica", "TACNA": "Tacna", "MOQUEGUA": "Moquegua",
        "TUMBES": "Tumbes", "AMAZONAS": "Amazonas", "PASCO": "Pasco",
        "AYACUCHO": "Ayacucho", "APURIMAC": "Apurímac", "APURÍMAC": "Apurímac",
        "HUANCAVELICA": "Huancavelica", "SAN MARTIN": "San Martín",
        "SAN MARTÍN": "San Martín", "CALLAO": "Callao",
    }
    combined = f"{entidad} {texto}".upper()
    for key, val in deptos.items():
        if key in combined:
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
    if "HOSPITAL" in e or "ESSALUD" in e or "RED DE SALUD" in e:
        return "hosp"
    if "PODER JUDICIAL" in e or "CORTE" in e:
        return "pj"
    if any(x in e for x in ["EJÉRCITO", "MARINA", "FUERZA AÉREA", "PNP", "POLICÍA"]):
        return "ffaa"
    return "otro"


def match_keywords(texto: str, keywords: list[str]) -> bool:
    texto_lower = texto.lower()
    return any(kw.lower() in texto_lower for kw in keywords)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=30))
async def scrape_seace(user_id: int = 0) -> list[dict]:
    """Scrape SEACE buscador público. Returns list of new licitaciones."""
    log_id = await log_scraping_start("seace_3.0")
    config = await get_config(user_id)
    keywords = config["keywords"] if config else []
    regiones = config["regiones"] if config else []
    monto_min = config["monto_min"] if config else 0
    monto_max = config["monto_max"] if config else 999999999

    nuevas = []
    encontradas = 0
    errores = 0

    try:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            # Paso 1: GET para obtener ViewState (JSF)
            resp = await client.get(SEACE_URL)
            soup = BeautifulSoup(resp.text, "lxml")
            
            viewstate = ""
            vs_input = soup.find("input", {"name": "javax.faces.ViewState"})
            if vs_input:
                viewstate = vs_input.get("value", "")

            # Paso 2: POST con filtros para buscar
            # SEACE usa JSF/PrimeFaces, el form submission es complejo
            # Intentamos un approach simplificado con el buscador público
            form_data = {
                "javax.faces.partial.ajax": "true",
                "javax.faces.source": "tbBuscador:idFormBuscarProceso:btnBuscarSelAccworking",
                "javax.faces.ViewState": viewstate,
                "tbBuscador:idFormBuscarProceso": "tbBuscador:idFormBuscarProceso",
            }

            # Si el POST JSF falla, hacemos scraping de la página renderizada
            # SEACE renderiza resultados en una tabla HTML
            resp2 = await client.post(SEACE_URL, data=form_data)
            
            if resp2.status_code != 200:
                # Fallback: intentar parsear la página directamente
                log.warning(f"SEACE POST returned {resp2.status_code}, parsing initial page")
                soup2 = soup
            else:
                soup2 = BeautifulSoup(resp2.text, "lxml")

            # Parsear tabla de resultados
            tabla = soup2.find("table", {"role": "grid"}) or soup2.find("tbody", class_=re.compile("ui-datatable"))
            
            if not tabla:
                # Intento alternativo: buscar todas las filas con datos de procedimientos
                filas = soup2.find_all("tr", class_=re.compile("ui-widget-content"))
            else:
                filas = tabla.find_all("tr")

            for fila in filas:
                try:
                    celdas = fila.find_all("td")
                    if len(celdas) < 5:
                        continue

                    # Extraer datos de las celdas
                    textos = [c.get_text(strip=True) for c in celdas]
                    
                    # La estructura típica de SEACE:
                    # Nomenclatura | Entidad | Objeto | Monto | Fecha Pub | Fecha Cierre | Estado
                    nomenclatura = textos[0] if len(textos) > 0 else ""
                    entidad = textos[1] if len(textos) > 1 else ""
                    objeto = textos[2] if len(textos) > 2 else ""
                    monto_text = textos[3] if len(textos) > 3 else ""
                    
                    if not entidad or not objeto:
                        continue

                    encontradas += 1
                    monto = parse_monto(monto_text)
                    depto = detectar_departamento(entidad, objeto)

                    # Filtros
                    if regiones and depto and depto not in regiones:
                        continue
                    if monto and (monto < monto_min or monto > monto_max):
                        continue
                    if keywords and not match_keywords(f"{objeto} {entidad}", keywords):
                        continue

                    # Extraer link a las bases si existe
                    link = fila.find("a", href=True)
                    url = link["href"] if link else ""
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
                        "objeto": objeto,
                        "monto_referencial": monto,
                        "departamento": depto,
                        "url": url,
                        "estado": "convocado",
                    }

                    is_new = await upsert_licitacion(data)
                    if is_new:
                        nuevas.append(data)

                except Exception as e:
                    errores += 1
                    log.error(f"Error parsing row: {e}")

    except Exception as e:
        errores += 1
        log.error(f"SEACE scraping failed: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    log.info(f"SEACE: {encontradas} encontradas, {len(nuevas)} nuevas, {errores} errores")
    return nuevas


def detectar_tipo_proc(nomenclatura: str) -> str:
    n = nomenclatura.upper()
    if "LP-" in n or "LICITACION" in n:
        return "LP"
    if "AS-" in n or "ADJUDICACION" in n:
        return "AS"
    if "SIE-" in n or "SUBASTA" in n:
        return "SIE"
    if "CP-" in n or "CONCURSO" in n:
        return "CP"
    if "CD-" in n or "CONTRATACION DIRECTA" in n:
        return "CD"
    if "CdP" in n or "COMPARACION" in n:
        return "CdP"
    return "otro"
