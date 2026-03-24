"""Cliente API OCDS — contratacionesabiertas.osce.gob.pe — datos estructurados."""
import logging
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from shared.db import upsert_licitacion, log_scraping_start, log_scraping_end, get_config

log = logging.getLogger("radar.ocds")

BASE_URL = "https://contratacionesabiertas.osce.gob.pe/api"
RELEASES_URL = f"{BASE_URL}/ocds/releases"


def parse_ocds_release(release: dict) -> dict | None:
    """Convierte un release OCDS al formato interno."""
    try:
        tender = release.get("tender", {})
        buyer = release.get("buyer", {})
        
        ocid = release.get("ocid", "")
        titulo = tender.get("title", "")
        descripcion = tender.get("description", "")
        entidad = buyer.get("name", "")
        
        if not titulo and not descripcion:
            return None

        monto = None
        moneda = "PEN"
        value = tender.get("value", {})
        if value:
            monto = value.get("amount")
            moneda = value.get("currency", "PEN")

        fecha_pub = release.get("date")
        fecha_cierre = None
        tender_period = tender.get("tenderPeriod", {})
        if tender_period:
            fecha_cierre = tender_period.get("endDate")

        # Detectar departamento del buyer address
        depto = None
        address = buyer.get("address", {})
        if address:
            region = address.get("region", "") or address.get("locality", "")
            if region:
                from radar_bot.scrapers.seace import detectar_departamento
                depto = detectar_departamento(entidad, region)

        # Tipo de procedimiento
        method = tender.get("procurementMethod", "")
        proc_category = tender.get("procurementMethodDetails", "")
        tipo = _map_procurement_method(method, proc_category)

        status = tender.get("status", "active")
        estado = "convocado" if status == "active" else status

        return {
            "id": f"ocds_{ocid[-16:]}" if ocid else None,
            "fuente": "ocds_api",
            "tipo": tipo,
            "nomenclatura": ocid,
            "entidad": entidad,
            "entidad_tipo": None,
            "objeto": titulo or descripcion,
            "descripcion": descripcion,
            "monto_referencial": monto,
            "moneda": moneda,
            "fecha_publicacion": fecha_pub,
            "fecha_cierre": fecha_cierre,
            "estado": estado,
            "departamento": depto,
            "url": f"https://contratacionesabiertas.osce.gob.pe/process/{ocid}",
        }
    except Exception as e:
        log.error(f"Error parsing OCDS release: {e}")
        return None


def _map_procurement_method(method: str, details: str) -> str:
    m = (method + " " + details).upper()
    if "LICITACION" in m or "OPEN" in m:
        return "LP"
    if "ADJUDICACION" in m or "SIMPLIFIED" in m:
        return "AS"
    if "SUBASTA" in m:
        return "SIE"
    if "CONCURSO" in m:
        return "CP"
    if "DIRECTA" in m or "DIRECT" in m:
        return "CD"
    return "otro"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=30))
async def scrape_ocds(user_id: int = 0, page_size: int = 50) -> list[dict]:
    """Consulta API OCDS y retorna licitaciones nuevas."""
    log_id = await log_scraping_start("ocds_api")
    config = await get_config(user_id)
    keywords = config["keywords"] if config else []
    regiones = config["regiones"] if config else []

    nuevas = []
    encontradas = 0
    errores = 0

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Intentar diferentes endpoints de la API OCDS
            urls_to_try = [
                f"{RELEASES_URL}?page=1&pageSize={page_size}",
                f"{BASE_URL}/releases?page=1&limit={page_size}",
                f"{BASE_URL}/v1/releases?limit={page_size}",
            ]
            
            data = None
            for url in urls_to_try:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                except Exception:
                    continue

            if not data:
                log.warning("OCDS API: no se pudo obtener datos de ningún endpoint")
                await log_scraping_end(log_id, 0, 0, 1, "No API endpoint responded")
                return []

            # OCDS puede devolver releases en diferentes estructuras
            releases = data.get("releases", data.get("results", data.get("data", [])))
            if isinstance(releases, dict):
                releases = releases.get("releases", [])

            for release in releases:
                encontradas += 1
                parsed = parse_ocds_release(release)
                if not parsed or not parsed["id"]:
                    continue

                # Filtros
                if regiones and parsed["departamento"] and parsed["departamento"] not in regiones:
                    continue
                
                from radar_bot.scrapers.seace import match_keywords
                if keywords and not match_keywords(
                    f"{parsed['objeto']} {parsed['entidad']}", keywords
                ):
                    continue

                is_new = await upsert_licitacion(parsed)
                if is_new:
                    nuevas.append(parsed)

    except Exception as e:
        errores += 1
        log.error(f"OCDS scraping failed: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    log.info(f"OCDS: {encontradas} encontradas, {len(nuevas)} nuevas, {errores} errores")
    return nuevas
