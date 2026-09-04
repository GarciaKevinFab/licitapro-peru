"""Scraper para Contratos Menores ≤8 UIT — prod6.seace.gob.pe."""
import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from shared.db import get_config, log_scraping_end, log_scraping_start, upsert_licitacion

log = logging.getLogger("radar.contratos_menores")

# prod6 es una React app — buscar las API calls internas
BASE_URL = "https://prod6.seace.gob.pe"
API_PATHS = [
    "/api/v1/contrataciones",
    "/api/contrataciones/buscar",
    "/buscador-publico/api/contrataciones",
]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=30))
async def scrape_contratos_menores(user_id: int = 0) -> list[dict]:
    """Scrape contratos menores ≤8 UIT. Alto volumen, cierres rápidos."""
    log_id = await log_scraping_start("contratos_menores")
    config = await get_config(user_id)
    keywords = config["keywords"] if config else []
    regiones = config["regiones"] if config else []

    nuevas = []
    encontradas = 0
    errores = 0

    try:
        async with httpx.AsyncClient(
            timeout=30,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/buscador-publico/contrataciones",
            },
            follow_redirects=True,
        ) as client:
            data = None
            
            # Intentar diferentes API endpoints
            for path in API_PATHS:
                try:
                    # Try GET first
                    resp = await client.get(f"{BASE_URL}{path}", params={
                        "page": 1, "size": 50, "sort": "fechaPublicacion,desc"
                    })
                    if resp.status_code == 200:
                        ct = resp.headers.get("content-type", "")
                        if "json" in ct:
                            data = resp.json()
                            break
                except Exception:
                    pass
                
                try:
                    # Try POST
                    resp = await client.post(f"{BASE_URL}{path}", json={
                        "page": 0, "size": 50,
                    })
                    if resp.status_code == 200:
                        ct = resp.headers.get("content-type", "")
                        if "json" in ct:
                            data = resp.json()
                            break
                except Exception:
                    pass

            if not data:
                log.warning("Contratos Menores: API no encontrada. Necesita reverse-engineering del frontend React.")
                await log_scraping_end(log_id, 0, 0, 1, "API endpoints not found")
                return []

            # Parsear resultados (estructura puede variar)
            items = data if isinstance(data, list) else data.get("content", data.get("data", data.get("results", [])))

            for item in items:
                encontradas += 1
                try:
                    objeto = item.get("descripcion", item.get("objeto", item.get("title", "")))
                    entidad = item.get("entidad", item.get("nombreEntidad", item.get("buyer", "")))
                    monto = item.get("monto", item.get("valorReferencial", item.get("amount")))
                    
                    if not objeto or not entidad:
                        continue

                    item_id = item.get("id", item.get("codigo", item.get("nroContratacion", "")))
                    
                    from radar_bot.scrapers.seace import (
                        detectar_departamento,
                        detectar_tipo_entidad,
                        match_keywords,
                    )
                    
                    depto = detectar_departamento(entidad, objeto)
                    
                    if regiones and depto and depto not in regiones:
                        continue
                    if keywords and not match_keywords(f"{objeto} {entidad}", keywords):
                        continue

                    parsed = {
                        "id": f"cm_{item_id}" if item_id else f"cm_{hash(objeto) % 10**10}",
                        "fuente": "contratos_menores",
                        "tipo": "CM",
                        "nomenclatura": str(item_id),
                        "entidad": entidad,
                        "entidad_tipo": detectar_tipo_entidad(entidad),
                        "objeto": objeto,
                        "monto_referencial": float(monto) if monto else None,
                        "departamento": depto,
                        "estado": "convocado",
                        "url": f"{BASE_URL}/buscador-publico/contrataciones/{item_id}" if item_id else None,
                    }

                    is_new = await upsert_licitacion(parsed)
                    if is_new:
                        nuevas.append(parsed)

                except Exception as e:
                    errores += 1
                    log.error(f"Error parsing CM item: {e}")

    except Exception as e:
        errores += 1
        log.error(f"Contratos Menores scraping failed: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    log.info(f"Contratos Menores: {encontradas} encontradas, {len(nuevas)} nuevas")
    return nuevas
