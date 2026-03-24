"""Scraper para Portal de Datos Abiertos del Estado Peruano."""
import logging
from radar_bot.scrapers.base_scraper import BaseScraper, generar_id, parse_fecha, parse_monto

log = logging.getLogger("radar.datos_abiertos")

# API de datos abiertos del gobierno peruano
DATOS_URL = "https://www.datosabiertos.gob.pe/api/3/action/package_search"


class DatosAbiertosScraper(BaseScraper):
    FUENTE = "datos_abiertos"
    URL = DATOS_URL

    async def _fetch_items(self, client):
        params = {
            "q": "licitacion OR contratacion OR convocatoria",
            "rows": 30,
            "sort": "metadata_modified desc",
        }
        resp = await client.get(self.URL, params=params)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("result", {}).get("results", [])

    def _parse_item(self, item) -> dict | None:
        titulo = item.get("title", "")
        org = item.get("organization", {}).get("title", "Estado Peruano")
        notas = item.get("notes", "")

        if not titulo:
            return None

        from radar_bot.scrapers.seace import detectar_departamento, detectar_tipo_entidad

        return {
            "id": generar_id("datosgob", item.get("id", titulo[:30])),
            "fuente": self.FUENTE,
            "nomenclatura": item.get("name", ""),
            "entidad": org,
            "entidad_tipo": detectar_tipo_entidad(org),
            "objeto": f"{titulo}. {notas[:200]}" if notas else titulo,
            "monto_referencial": None,
            "fecha_publicacion": parse_fecha(item.get("metadata_created", "")),
            "departamento": detectar_departamento(org, titulo),
            "url": f"https://www.datosabiertos.gob.pe/dataset/{item.get('name', '')}",
            "estado": "convocado",
        }


async def scrape_datos_abiertos(user_id: int = 0) -> list[dict]:
    scraper = DatosAbiertosScraper()
    return await scraper.scrape(user_id)
