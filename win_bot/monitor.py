"""Monitor — Detecta buena pro en SEACE automáticamente."""
import logging
import httpx
from bs4 import BeautifulSoup
from shared.db import connection

log = logging.getLogger("win.monitor")

SEACE_RESULTADO_URL = "https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "es-PE,es;q=0.9",
}


async def verificar_adjudicaciones() -> list[dict]:
    """Verifica en SEACE si alguna propuesta enviada fue adjudicada.

    Flujo:
    1. Obtener propuestas en estado 'enviado'
    2. Para cada una, buscar en SEACE el estado del proceso
    3. Si detecta 'buena pro' a nuestra empresa → notificar

    Returns: lista de adjudicaciones detectadas
    """
    adjudicaciones = []

    async with connection() as conn:
        props = await conn.fetch(
            """SELECT p.id as propuesta_id, p.empresa_id,
                      l.id as lic_id, l.nomenclatura, l.entidad, l.objeto,
                      l.monto_referencial, l.url,
                      e.razon_social, e.ruc
            FROM propuestas p
            JOIN licitaciones l ON p.licitacion_id = l.id
            JOIN empresas e ON p.empresa_id = e.id
            WHERE p.estado = 'enviado'
            AND NOT EXISTS (SELECT 1 FROM contratos c WHERE c.propuesta_id = p.id)"""
        )

    if not props:
        return []

    async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
        for prop in props:
            try:
                resultado = await _buscar_resultado_seace(
                    client, prop["nomenclatura"], prop["ruc"]
                )
                if resultado and resultado.get("ganador"):
                    adjudicaciones.append({
                        "propuesta_id": prop["propuesta_id"],
                        "empresa_id": prop["empresa_id"],
                        "licitacion_id": prop["lic_id"],
                        "nomenclatura": prop["nomenclatura"],
                        "entidad": prop["entidad"],
                        "objeto": prop["objeto"],
                        "monto_adjudicado": resultado.get("monto"),
                        "fecha_adjudicacion": resultado.get("fecha"),
                        "es_nuestro": resultado.get("ruc_ganador") == prop["ruc"],
                    })
            except Exception as e:
                log.error(f"Error verificando {prop['nomenclatura']}: {e}")

    log.info(f"Verificación completada: {len(adjudicaciones)} adjudicaciones detectadas")
    return adjudicaciones


async def _buscar_resultado_seace(client: httpx.AsyncClient,
                                    nomenclatura: str, ruc: str) -> dict | None:
    """Busca el resultado de un proceso en SEACE."""
    if not nomenclatura:
        return None

    try:
        # Intentar buscar por nomenclatura en SEACE
        resp = await client.get(SEACE_RESULTADO_URL)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        # Buscar en las tablas de resultados
        filas = soup.find_all("tr")
        for fila in filas:
            text = fila.get_text()
            if nomenclatura.lower() in text.lower():
                celdas = fila.find_all("td")
                textos = [c.get_text(strip=True) for c in celdas]

                # Buscar indicios de adjudicación
                text_completo = " ".join(textos).lower()
                if "buena pro" in text_completo or "adjudicado" in text_completo:
                    # Intentar extraer RUC del ganador
                    import re
                    ruc_match = re.search(r"(\d{11})", text_completo)
                    ruc_ganador = ruc_match.group(1) if ruc_match else None

                    return {
                        "ganador": True,
                        "ruc_ganador": ruc_ganador,
                        "monto": _extract_monto(text_completo),
                        "fecha": None,
                    }

        return None

    except Exception as e:
        log.error(f"Error buscando en SEACE: {e}")
        return None


def _extract_monto(text: str) -> float | None:
    """Intenta extraer un monto de un texto."""
    import re
    # Buscar patrones como S/ 123,456.78 o 123456.78
    match = re.search(r"s/\.?\s*([\d,]+\.?\d*)", text.lower())
    if match:
        monto_str = match.group(1).replace(",", "")
        try:
            return float(monto_str)
        except ValueError:
            pass
    return None


async def verificar_estado_proceso(nomenclatura: str) -> dict:
    """Consulta el estado actual de un proceso en SEACE."""
    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
            resp = await client.get(SEACE_RESULTADO_URL)
            if resp.status_code != 200:
                return {"error": f"SEACE returned {resp.status_code}"}

            soup = BeautifulSoup(resp.text, "lxml")
            filas = soup.find_all("tr")

            for fila in filas:
                if nomenclatura.lower() in fila.get_text().lower():
                    celdas = fila.find_all("td")
                    textos = [c.get_text(strip=True) for c in celdas]
                    return {
                        "encontrado": True,
                        "datos": textos,
                        "texto_completo": " | ".join(textos),
                    }

        return {"encontrado": False}

    except Exception as e:
        return {"error": str(e)}
