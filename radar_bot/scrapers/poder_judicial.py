"""Scraper para Portal del Poder Judicial — Convocatorias PJ."""
import logging

from bs4 import BeautifulSoup

from radar_bot.scrapers.base_scraper import BaseScraper, generar_id, parse_monto

log = logging.getLogger("radar.poder_judicial")

PJ_URL = "https://www.pj.gob.pe/wps/wcm/connect/CorteSuprema/s_corte_suprema_home/as_convocatorias/"


class PoderJudicialScraper(BaseScraper):
    FUENTE = "poder_judicial"
    URL = PJ_URL

    async def _fetch_items(self, client):
        resp = await client.get(self.URL)
        if resp.status_code != 200:
            # Intentar endpoint alternativo
            alt = "https://www.pj.gob.pe/wps/wcm/connect/Logistica/s_corte_suprema_logistica/as_procesos_seleccion/"
            resp = await client.get(alt)
            if resp.status_code != 200:
                return []

        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select("table tbody tr, .wpthemeContent .lotusui30 tr, .list-group-item")
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

        from radar_bot.scrapers.seace import detectar_departamento

        link = item.find("a", href=True)
        url = link["href"] if link else ""
        if url and not url.startswith("http"):
            url = f"https://www.pj.gob.pe{url}"

        return {
            "id": generar_id("pj", nomenclatura or objeto[:30]),
            "fuente": self.FUENTE,
            "tipo": "otro",
            "nomenclatura": nomenclatura,
            "entidad": "Poder Judicial",
            "entidad_tipo": "pj",
            "objeto": objeto,
            "monto_referencial": parse_monto(monto_text),
            "departamento": detectar_departamento("Poder Judicial", objeto),
            "url": url,
            "estado": "convocado",
        }


async def scrape_poder_judicial(user_id: int = 0) -> list[dict]:
    scraper = PoderJudicialScraper()
    return await scraper.scrape(user_id)
