"""Scraper para EsSalud — Convocatorias y contrataciones."""
import logging
from bs4 import BeautifulSoup
from radar_bot.scrapers.base_scraper import BaseScraper, generar_id, parse_monto

log = logging.getLogger("radar.essalud")

ESSALUD_URL = "http://www.essalud.gob.pe/contrataciones-del-estado/"


class EsSaludScraper(BaseScraper):
    FUENTE = "essalud"
    URL = ESSALUD_URL

    async def _fetch_items(self, client):
        resp = await client.get(self.URL)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select("table tbody tr, .entry-content li a, .post-content a")
        return [i for i in items if i.get_text(strip=True)]

    def _parse_item(self, item) -> dict | None:
        celdas = item.find_all("td")
        if celdas and len(celdas) >= 3:
            textos = [c.get_text(strip=True) for c in celdas]
            nomenclatura = textos[0]
            objeto = textos[1] if len(textos) > 1 else ""
            monto_text = textos[2] if len(textos) > 2 else ""
        else:
            text = item.get_text(strip=True)
            if len(text) < 15:
                return None
            nomenclatura = ""
            objeto = text[:300]
            monto_text = ""

        if not objeto:
            return None

        from radar_bot.scrapers.seace import detectar_departamento

        link = item.find("a", href=True) if item.name != "a" else item
        url = ""
        if link:
            url = link.get("href", "")
            if url and not url.startswith("http"):
                url = f"http://www.essalud.gob.pe{url}"

        return {
            "id": generar_id("essalud", nomenclatura or objeto[:30]),
            "fuente": self.FUENTE,
            "nomenclatura": nomenclatura,
            "entidad": "EsSalud",
            "entidad_tipo": "hosp",
            "objeto": objeto,
            "monto_referencial": parse_monto(monto_text),
            "departamento": detectar_departamento("EsSalud", objeto),
            "url": url,
            "estado": "convocado",
        }


async def scrape_essalud(user_id: int = 0) -> list[dict]:
    scraper = EsSaludScraper()
    return await scraper.scrape(user_id)
