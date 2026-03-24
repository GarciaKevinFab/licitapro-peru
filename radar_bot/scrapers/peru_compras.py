"""Scraper para Perú Compras — Catálogos Electrónicos y Acuerdos Marco."""
import logging
from bs4 import BeautifulSoup
from radar_bot.scrapers.base_scraper import BaseScraper, generar_id, parse_fecha, parse_monto

log = logging.getLogger("radar.peru_compras")

PERU_COMPRAS_URL = "https://www.perucompras.gob.pe/convocatorias/listado-convocatorias.aspx"


class PeruComprasScraper(BaseScraper):
    FUENTE = "peru_compras"
    URL = PERU_COMPRAS_URL

    async def _fetch_items(self, client):
        resp = await client.get(self.URL)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")
        filas = soup.select("table.table tbody tr, .list-group-item, .card")
        return filas

    def _parse_item(self, item) -> dict | None:
        celdas = item.find_all("td") if item.name == "tr" else []
        if len(celdas) < 4:
            text = item.get_text(strip=True)
            if len(text) < 20:
                return None
            return {
                "id": generar_id("pcompras", text[:50]),
                "fuente": self.FUENTE,
                "nomenclatura": "",
                "entidad": "Perú Compras",
                "objeto": text[:300],
                "monto_referencial": None,
                "departamento": None,
                "url": self.URL,
                "estado": "convocado",
            }

        textos = [c.get_text(strip=True) for c in celdas]
        from radar_bot.scrapers.seace import detectar_departamento, detectar_tipo_entidad

        entidad = textos[1] if len(textos) > 1 else "Perú Compras"
        objeto = textos[2] if len(textos) > 2 else textos[0]

        link = item.find("a", href=True)
        url = link["href"] if link else ""
        if url and not url.startswith("http"):
            url = f"https://www.perucompras.gob.pe{url}"

        return {
            "id": generar_id("pcompras", textos[0], entidad),
            "fuente": self.FUENTE,
            "nomenclatura": textos[0],
            "entidad": entidad,
            "entidad_tipo": detectar_tipo_entidad(entidad),
            "objeto": objeto,
            "monto_referencial": parse_monto(textos[3]) if len(textos) > 3 else None,
            "fecha_cierre": parse_fecha(textos[4]) if len(textos) > 4 else None,
            "departamento": detectar_departamento(entidad, objeto),
            "url": url,
            "estado": "convocado",
        }


async def scrape_peru_compras(user_id: int = 0) -> list[dict]:
    scraper = PeruComprasScraper()
    return await scraper.scrape(user_id)
