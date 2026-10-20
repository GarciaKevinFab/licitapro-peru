"""Base scraper — Clase base para todos los scrapers."""
import hashlib
import logging
from datetime import datetime

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from shared import fechas
from shared.db import get_config, log_scraping_end, log_scraping_start, upsert_licitacion

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9",
}


def generar_id(prefix: str, *args) -> str:
    raw = "_".join(str(a) for a in args).lower().strip()
    return f"{prefix}_{hashlib.md5(raw.encode()).hexdigest()[:12]}"


def parse_fecha(text: str) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y"):
        try:
            return fechas.desde_texto(text, fmt)
        except ValueError:
            continue
    return None


def parse_monto(text: str) -> float | None:
    # Delega en el parser compartido. Este ni siquiera descartaba 0 o negativos.
    from shared.config import parse_monto as _pm
    return _pm(text)


def match_config_filters(data: dict, config) -> bool:
    """Filtra según config del usuario (regiones, keywords, monto)."""
    if not config:
        return True

    regiones = config.get("regiones") or []
    keywords = config.get("keywords") or []
    monto_min = config.get("monto_min", 0) or 0
    monto_max = config.get("monto_max", 999999999) or 999999999

    depto = data.get("departamento", "")
    if regiones and depto and depto not in regiones:
        return False

    monto = data.get("monto_referencial") or 0
    if monto and (monto < monto_min or monto > monto_max):
        return False

    if keywords:
        texto = f"{data.get('objeto', '')} {data.get('entidad', '')}".lower()
        if not any(kw.lower() in texto for kw in keywords):
            return False

    return True


class BaseScraper:
    """Clase base para scrapers de licitaciones."""

    FUENTE = "base"
    URL = ""
    # REINTENTOS. Antes el @retry decoraba `scrape`, cuyo try interno se
    # tragaba todo: nunca veia una excepcion y nunca reintentaba (y si lo
    # hubiera hecho, habria abierto otra fila en scraping_log por intento).
    # Ahora se reintenta solo la descarga y solo ante errores de red; un
    # 404 no mejora por insistir.
    REINTENTOS = 3
    ESPERA_MIN = 5

    def __init__(self):
        self.log = logging.getLogger(f"radar.{self.FUENTE}")
        self._fallos: list[str] = []

    async def _pedir(self, client: httpx.AsyncClient, url: str, **kwargs):
        """GET que deja constancia. La respuesta si es 200; None si no.

        Los scrapers heredados hacian `if resp.status_code != 200: return []`:
        un 404 y un dia sin convocatorias acababan igual, "0 encontradas, 0
        errores". El codigo queda en el log (warning) y en
        `scraping_log.error_detalle`. Los errores de red NO se capturan aqui:
        suben para que `scrape` reintente.
        """
        resp = await client.get(url, **kwargs)
        if resp.status_code != 200:
            self._fallos.append(f"{url}: HTTP {resp.status_code}")
            self.log.warning("%s: %s respondio HTTP %s", self.FUENTE, url,
                             resp.status_code)
            return None
        return resp

    async def _fetch_con_reintento(self, client: httpx.AsyncClient) -> list:
        async for intento in AsyncRetrying(
                stop=stop_after_attempt(self.REINTENTOS),
                wait=wait_exponential(min=self.ESPERA_MIN, max=30),
                retry=retry_if_exception_type(httpx.TransportError),
                reraise=True):
            with intento:
                self._fallos = []
                return await self._fetch_items(client)
        return []

    async def scrape(self, user_id: int = 0) -> list[dict]:
        log_id = await log_scraping_start(self.FUENTE)
        config = await get_config(user_id)
        nuevas = []
        encontradas = 0
        errores = 0
        self._fallos = []
        causa = None

        try:
            async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
                items = await self._fetch_con_reintento(client)
                encontradas = len(items)

                for item in items:
                    try:
                        data = self._parse_item(item)
                        if not data or not data.get("entidad") or not data.get("objeto"):
                            continue
                        if not match_config_filters(data, config):
                            continue

                        is_new = await upsert_licitacion(data)
                        if is_new:
                            nuevas.append(data)
                    except Exception as e:  # noqa: BLE001
                        errores += 1
                        self.log.error(f"Error parsing item: {e}")

        except Exception as e:  # noqa: BLE001
            errores += 1
            causa = f"{type(e).__name__}: {str(e)[:200]}"
            self.log.error(f"{self.FUENTE} scraping failed: {e}")

        errores += len(self._fallos)
        detalle = " | ".join(x for x in [causa, *self._fallos] if x) or None
        if detalle and encontradas == 0:
            detalle = f"CAIDA -- {detalle}"
        await log_scraping_end(log_id, encontradas, len(nuevas), errores, detalle)
        self.log.info(f"{self.FUENTE}: {encontradas} encontradas, {len(nuevas)} nuevas, {errores} errores")
        return nuevas

    async def _fetch_items(self, client: httpx.AsyncClient) -> list:
        """Override: fetch raw items from source."""
        raise NotImplementedError

    def _parse_item(self, item) -> dict | None:
        """Override: parse raw item into licitacion dict."""
        raise NotImplementedError
