"""Scraper para portales de Gobiernos Regionales (GORE)."""
import logging

from bs4 import BeautifulSoup

from radar_bot.scrapers.base_scraper import BaseScraper, generar_id, parse_monto

log = logging.getLogger("radar.gore")

# Portales de GOREs prioritarios
GORE_PORTALS = {
    "Madre de Dios": "https://www.regionmadrededios.gob.pe",
    "Junín": "https://www.regionjunin.gob.pe",
    "Cusco": "https://www.regioncusco.gob.pe",
}

GORE_PATHS = [
    "/convocatorias",
    "/licitaciones",
    "/contrataciones",
    "/logistica/procesos-de-seleccion",
    "/abastecimiento",
]


class GOREPortalsScraper(BaseScraper):
    FUENTE = "gore_portals"

    async def scrape(self, user_id: int = 0) -> list[dict]:
        import httpx

        from radar_bot.scrapers.base_scraper import HEADERS, match_config_filters
        from shared.db import get_config, log_scraping_end, log_scraping_start, upsert_licitacion

        log_id = await log_scraping_start(self.FUENTE)
        config = await get_config(user_id)
        nuevas = []
        encontradas = 0
        errores = 0

        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            for depto, base_url in GORE_PORTALS.items():
                for path in GORE_PATHS:
                    try:
                        url = f"{base_url}{path}"
                        resp = await client.get(url)
                        if resp.status_code != 200:
                            continue

                        soup = BeautifulSoup(resp.text, "lxml")
                        items = soup.select("table tbody tr, .entry-content li, article, .post-item")

                        for item in items:
                            try:
                                data = self._parse_gore_item(item, depto, base_url)
                                if not data:
                                    continue
                                encontradas += 1
                                if match_config_filters(data, config):
                                    is_new = await upsert_licitacion(data)
                                    if is_new:
                                        nuevas.append(data)
                            except Exception as e:  # noqa: BLE001
                                # Una fila con el HTML cambiado no puede tumbar
                                # las demas del mismo portal.
                                errores += 1
                                log.debug("%s: fila descartada (%s)", depto, e)
                    except Exception as e:  # noqa: BLE001
                        # Y un portal caido no puede tumbar a los otros. Se
                        # registra: sin esto, una region que deja de responder
                        # se lee en el parte igual que una region sin
                        # convocatorias, que es justo como cinco fuentes
                        # estuvieron invisibles durante 21 pasadas.
                        log.debug("%s%s no respondio (%s)", base_url, path, e)
                        continue

        await log_scraping_end(log_id, encontradas, len(nuevas), errores)
        self.log.info(f"GORE portals: {encontradas} encontradas, {len(nuevas)} nuevas")
        return nuevas

    def _parse_gore_item(self, item, depto: str, base_url: str) -> dict | None:
        text = item.get_text(strip=True)
        if len(text) < 20:
            return None

        celdas = item.find_all("td")
        if celdas and len(celdas) >= 2:
            textos = [c.get_text(strip=True) for c in celdas]
            objeto = textos[1] if len(textos) > 1 else textos[0]
            nomenclatura = textos[0]
            monto_text = textos[2] if len(textos) > 2 else ""
        else:
            objeto = text[:300]
            nomenclatura = ""
            monto_text = ""

        link = item.find("a", href=True)
        url = link["href"] if link else ""
        if url and not url.startswith("http"):
            url = f"{base_url}{url}"

        return {
            "id": generar_id("gore", depto, objeto[:30]),
            "fuente": self.FUENTE,
            "tipo": "otro",
            "nomenclatura": nomenclatura,
            "entidad": f"Gobierno Regional de {depto}",
            "entidad_tipo": "gore",
            "objeto": objeto,
            "monto_referencial": parse_monto(monto_text),
            "departamento": depto,
            "url": url,
            "estado": "convocado",
        }

    def _parse_item(self, item) -> dict | None:
        return None  # Not used, overrides scrape()


async def scrape_gore_portals(user_id: int = 0) -> list[dict]:
    scraper = GOREPortalsScraper()
    return await scraper.scrape(user_id)
