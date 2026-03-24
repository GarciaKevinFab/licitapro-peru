"""Scraper para CONOSCE — Portal de consultas del OSCE."""
import logging
from bs4 import BeautifulSoup
from radar_bot.scrapers.base_scraper import BaseScraper, generar_id, parse_fecha, parse_monto

log = logging.getLogger("radar.conosce")

CONOSCE_URL = "https://portal.osce.gob.pe/osce/conosce/convocatorias"


class CONOSCEScraper(BaseScraper):
    FUENTE = "conosce"
    URL = CONOSCE_URL

    async def _fetch_items(self, client):
        resp = await client.get(self.URL)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        filas = soup.select("table tbody tr, .views-row")
        return filas

    def _parse_item(self, item) -> dict | None:
        celdas = item.find_all("td") if item.name == "tr" else []

        if not celdas:
            # Modo vista (views-row)
            titulo = item.find(class_="views-field-title")
            entidad_el = item.find(class_="views-field-field-entidad")
            if not titulo:
                return None
            objeto = titulo.get_text(strip=True)
            entidad = entidad_el.get_text(strip=True) if entidad_el else "OSCE"
        elif len(celdas) < 3:
            return None
        else:
            textos = [c.get_text(strip=True) for c in celdas]
            objeto = textos[1] if len(textos) > 1 else textos[0]
            entidad = textos[2] if len(textos) > 2 else ""

        if not objeto:
            return None

        from radar_bot.scrapers.seace import detectar_departamento, detectar_tipo_entidad

        link = item.find("a", href=True)
        url = link["href"] if link else ""
        if url and not url.startswith("http"):
            url = f"https://portal.osce.gob.pe{url}"

        return {
            "id": generar_id("conosce", objeto[:40], entidad),
            "fuente": self.FUENTE,
            "nomenclatura": "",
            "entidad": entidad,
            "entidad_tipo": detectar_tipo_entidad(entidad),
            "objeto": objeto,
            "monto_referencial": None,
            "departamento": detectar_departamento(entidad, objeto),
            "url": url,
            "estado": "convocado",
        }


async def scrape_conosce(user_id: int = 0) -> list[dict]:
    scraper = CONOSCEScraper()
    return await scraper.scrape(user_id)
