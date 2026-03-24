"""Orquestador de scrapers — ejecuta todos los scrapers de forma modular."""
import logging
import asyncio
from datetime import datetime

log = logging.getLogger("radar.orchestrator")


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
        ("ocds_api", _run_ocds),
        ("contratos_menores", _run_contratos_menores),
        ("peru_compras", _run_peru_compras),
        ("datos_abiertos", _run_datos_abiertos),
        ("gore_portals", _run_gore_portals),
        ("poder_judicial", _run_poder_judicial),
        ("transparencia", _run_transparencia),
        ("essalud", _run_essalud),
    ]

    for nombre, func in scrapers:
        try:
            nuevas = await func(user_id)
            count = len(nuevas) if nuevas else 0
            results["por_fuente"][nombre] = count
            results["total_nuevas"] += count
            log.info(f"✅ {nombre}: {count} nuevas")
        except Exception as e:
            results["errores"].append(f"{nombre}: {str(e)[:100]}")
            results["por_fuente"][nombre] = -1
            log.error(f"❌ {nombre} falló: {e}")
        
        # Rate limiting: esperar entre scrapers
        await asyncio.sleep(2)

    log.info(
        f"Scraping completo: {results['total_nuevas']} nuevas, "
        f"{len(results['errores'])} errores"
    )
    return results


async def _run_seace(user_id):
    from radar_bot.scrapers.seace import scrape_seace
    return await scrape_seace(user_id)


async def _run_ocds(user_id):
    from radar_bot.scrapers.ocds_api import scrape_ocds
    return await scrape_ocds(user_id)


async def _run_contratos_menores(user_id):
    from radar_bot.scrapers.contratos_menores import scrape_contratos_menores
    return await scrape_contratos_menores(user_id)


async def _run_peru_compras(user_id):
    """Perú Compras — Catálogos Electrónicos."""
    from shared.db import log_scraping_start, log_scraping_end
    import httpx
    
    log_id = await log_scraping_start("peru_compras")
    nuevas = []
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # CEAM buscador
            resp = await client.get(
                "https://buscadorcatalogos.perucompras.gob.pe/api/catalogs",
                params={"page": 1, "size": 20}
            )
            if resp.status_code == 200:
                data = resp.json()
                # Process catalogs... (structure varies)
                items = data if isinstance(data, list) else data.get("data", [])
                await log_scraping_end(log_id, len(items), 0, 0)
            else:
                await log_scraping_end(log_id, 0, 0, 1, f"HTTP {resp.status_code}")
    except Exception as e:
        await log_scraping_end(log_id, 0, 0, 1, str(e))
        log.warning(f"Perú Compras: {e}")
    
    return nuevas


async def _run_datos_abiertos(user_id):
    """datosabiertos.gob.pe — Datasets del Estado."""
    from shared.db import log_scraping_start, log_scraping_end
    import httpx
    
    log_id = await log_scraping_start("datos_abiertos")
    nuevas = []
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://www.datosabiertos.gob.pe/api/3/action/package_search",
                params={"q": "contrataciones", "rows": 20}
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("result", {}).get("results", [])
                await log_scraping_end(log_id, len(results), 0, 0)
            else:
                await log_scraping_end(log_id, 0, 0, 1, f"HTTP {resp.status_code}")
    except Exception as e:
        await log_scraping_end(log_id, 0, 0, 1, str(e))
    
    return nuevas


async def _run_gore_portals(user_id):
    """Portales regionales de cotización — scraping individual."""
    from shared.db import log_scraping_start, log_scraping_end, get_config
    import httpx
    from bs4 import BeautifulSoup
    
    config = await get_config(user_id)
    regiones = config["regiones"] if config else []
    
    # Map de regiones a URLs de cotización conocidas
    gore_urls = {
        "Madre de Dios": "http://cotizaciones.regionmadrededios.gob.pe/",
        # Agregar más regiones conforme se descubren sus portales
    }
    
    log_id = await log_scraping_start("gore_portals")
    nuevas = []
    encontradas = 0
    
    for region, url in gore_urls.items():
        if regiones and region not in regiones:
            continue
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    # Parsear tabla de cotizaciones
                    rows = soup.find_all("tr")
                    for row in rows[1:]:  # Skip header
                        cells = row.find_all("td")
                        if len(cells) >= 3:
                            encontradas += 1
                            # Parse row data...
        except Exception as e:
            log.warning(f"GORE {region}: {e}")
    
    await log_scraping_end(log_id, encontradas, len(nuevas), 0)
    return nuevas


async def _run_poder_judicial(user_id):
    """Portal de Abastecimiento del Poder Judicial."""
    from shared.db import log_scraping_start, log_scraping_end
    
    log_id = await log_scraping_start("poder_judicial")
    # sap.pj.gob.pe requiere JavaScript rendering
    # TODO: Implementar con Playwright/Puppeteer
    await log_scraping_end(log_id, 0, 0, 0, "Requiere JS rendering - pendiente")
    return []


async def _run_transparencia(user_id):
    """Portal de Transparencia — PAC de entidades."""
    from shared.db import log_scraping_start, log_scraping_end
    
    log_id = await log_scraping_start("transparencia")
    # transparencia.gob.pe — scraping de PAC
    # TODO: Implementar por entidad
    await log_scraping_end(log_id, 0, 0, 0, "Pendiente implementación")
    return []


async def _run_essalud(user_id):
    """EsSalud — contrataciones hospitalarias."""
    from shared.db import log_scraping_start, log_scraping_end
    
    log_id = await log_scraping_start("essalud")
    # EsSalud publica en SEACE, pero también tiene portal propio
    # TODO: Implementar scraping específico
    await log_scraping_end(log_id, 0, 0, 0, "Pendiente implementación")
    return []


def format_scraping_report(results: dict) -> str:
    """Formatea resultados de scraping para Telegram."""
    lines = [f"📊 **Reporte de Scraping** — {results['timestamp'][:16]}\n"]
    
    for fuente, count in results["por_fuente"].items():
        if count == -1:
            emoji = "❌"
            text = "Error"
        elif count == 0:
            emoji = "⚪"
            text = "Sin nuevas"
        else:
            emoji = "✅"
            text = f"{count} nuevas"
        lines.append(f"{emoji} {fuente}: {text}")
    
    lines.append(f"\n📈 Total nuevas: {results['total_nuevas']}")
    
    if results["errores"]:
        lines.append(f"⚠️ Errores: {len(results['errores'])}")
    
    return "\n".join(lines)
