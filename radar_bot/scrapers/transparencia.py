"""Scraper para Portal de Transparencia del Estado Peruano."""
import logging
from bs4 import BeautifulSoup
from radar_bot.scrapers.base_scraper import BaseScraper, generar_id, parse_fecha, parse_monto

log = logging.getLogger("radar.transparencia")

TRANSPARENCIA_URL = "https://www.transparencia.gob.pe/contrataciones/ct_contratos.aspx"


class TransparenciaScraper(BaseScraper):
    FUENTE = "transparencia"
    URL = TRANSPARENCIA_URL

    async def _fetch_items(self, client):
        resp = await client.get(self.URL)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        filas = soup.select("table tbody tr, .row-contratacion, #ContentPlaceHolder1_grdContrato tr")
        return [f for f in filas if f.find_all("td")]

    def _parse_item(self, item) -> dict | None:
        celdas = item.find_all("td")
        if len(celdas) < 3:
            return None

        textos = [c.get_text(strip=True) for c in celdas]
        entidad = textos[0] if textos[0] else ""
        objeto = textos[1] if len(textos) > 1 else ""

        if not entidad or not objeto:
            return None

        from radar_bot.scrapers.seace import detectar_departamento, detectar_tipo_entidad

        link = item.find("a", href=True)
        url = link["href"] if link else ""
        if url and not url.startswith("http"):
            url = f"https://www.transparencia.gob.pe{url}"

        return {
            "id": generar_id("transp", objeto[:30], entidad),
            "fuente": self.FUENTE,
            "nomenclatura": textos[0] if textos else "",
            "entidad": entidad,
            "entidad_tipo": detectar_tipo_entidad(entidad),
            "objeto": objeto,
            "monto_referencial": parse_monto(textos[2]) if len(textos) > 2 else None,
            "fecha_cierre": parse_fecha(textos[3]) if len(textos) > 3 else None,
            "departamento": detectar_departamento(entidad, objeto),
            "url": url,
            "estado": "convocado",
        }


async def scrape_transparencia(user_id: int = 0) -> list[dict]:
    scraper = TransparenciaScraper()
    return await scraper.scrape(user_id)
