"""Scraper para Open Contracting Partnership — Datos internacionales."""
import logging

from radar_bot.scrapers.base_scraper import BaseScraper, generar_id, parse_fecha

log = logging.getLogger("radar.open_contracting")

OC_API_URL = "https://api.open-contracting.org/v1/releases"


class OpenContractingScraper(BaseScraper):
    FUENTE = "open_contracting"
    URL = OC_API_URL

    async def _fetch_items(self, client):
        params = {"country": "PE", "limit": 30}
        try:
            resp = await client.get(self.URL, params=params)
            if resp.status_code != 200:
                # Fallback: intentar endpoint alternativo
                alt_url = "https://standard.open-contracting.org/latest/es/api/"
                resp = await client.get(alt_url)
                if resp.status_code != 200:
                    return []
            data = resp.json()
            return data.get("releases", data.get("results", []))
        except Exception as e:  # noqa: BLE001
            self.log.warning(f"Open Contracting API error: {e}")
            return []

    def _parse_item(self, item) -> dict | None:
        tender = item.get("tender", {})
        buyer = item.get("buyer", {})

        entidad = buyer.get("name", "")
        objeto = tender.get("title", "") or tender.get("description", "")
        if not entidad or not objeto:
            return None

        value = tender.get("value", {})
        period = tender.get("tenderPeriod", {})

        from radar_bot.scrapers.seace import detectar_departamento, detectar_tipo_entidad

        return {
            "id": generar_id("oc", item.get("ocid", objeto[:30])),
            "fuente": self.FUENTE,
            "nomenclatura": item.get("ocid", ""),
            "entidad": entidad,
            "entidad_tipo": detectar_tipo_entidad(entidad),
            "objeto": objeto,
            "monto_referencial": value.get("amount"),
            "moneda": value.get("currency", "PEN"),
            "fecha_publicacion": parse_fecha(period.get("startDate", "")),
            "fecha_cierre": parse_fecha(period.get("endDate", "")),
            "departamento": detectar_departamento(entidad, objeto),
            "url": item.get("url", ""),
            "estado": tender.get("status", "active"),
        }


async def scrape_open_contracting(user_id: int = 0) -> list[dict]:
    scraper = OpenContractingScraper()
    return await scraper.scrape(user_id)
