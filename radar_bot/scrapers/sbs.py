"""Scraper para SBS (Superintendencia de Banca y Seguros) — Contrataciones."""
import logging

from bs4 import BeautifulSoup

from radar_bot.scrapers.base_scraper import BaseScraper, generar_id, parse_monto

log = logging.getLogger("radar.sbs")

SBS_URL = "https://www.sbs.gob.pe/principal/categoria/contrataciones/3128/c-3128"


class SBSScraper(BaseScraper):
    FUENTE = "sbs"
    URL = SBS_URL

    async def _fetch_items(self, client):
        resp = await client.get(self.URL)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select("table tbody tr, .content-list li, .list-group-item, .card-body")
        return [i for i in items if i.get_text(strip=True)]

    def _parse_item(self, item) -> dict | None:
        celdas = item.find_all("td")
        if celdas and len(celdas) >= 2:
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

        link = item.find("a", href=True)
        url = link["href"] if link else ""
        if url and not url.startswith("http"):
            url = f"https://www.sbs.gob.pe{url}"

        return {
            "id": generar_id("sbs", nomenclatura or objeto[:30]),
            "fuente": self.FUENTE,
            "nomenclatura": nomenclatura,
            "entidad": "Superintendencia de Banca, Seguros y AFP",
            "entidad_tipo": "otro",
            "objeto": objeto,
            "monto_referencial": parse_monto(monto_text),
            "departamento": "Lima",
            "url": url,
            "estado": "convocado",
        }


async def scrape_sbs(user_id: int = 0) -> list[dict]:
    scraper = SBSScraper()
    return await scraper.scrape(user_id)
