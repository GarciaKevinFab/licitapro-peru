"""Datos publicos de una empresa a partir de su RUC, para rellenar el alta.

POR QUE NO SE PREGUNTA A SUNAT DIRECTAMENTE

  La consulta RUC de SUNAT (e-consultaruc.sunat.gob.pe) exige resolver un
  captcha. No se evade: saltarse un control anti-bot de un sistema del Estado
  no es una opcion tecnica a valorar. Lo que si hay son servicios que
  republican el padron de SUNAT, que es informacion publica, en JSON y sin
  captcha. Se usan dos, en orden:

    1. OpenRUC (openruc.com/api/ruc/{ruc}): sin clave ni login, responde
       desde un CDN. Trae razon social, estado, condicion, direccion y ubigeo.
    2. apis.net.pe (api.apis.net.pe/v1/ruc?numero=): sin clave trae ademas
       departamento/provincia/distrito escritos, pero limita por IP: en la
       prueba del 2026-10-07 devolvio 429 a la segunda llamada seguida. Por
       eso es el respaldo y no el primero.

  La region NO se toma del texto que devuelvan sino del ubigeo, que es un
  codigo (shared.ubigeo.por_ubigeo): "LIMA" escrito puede ser el departamento
  o la provincia; 150130 es San Borja y no hay forma de leerlo mal.

LO QUE NO SE PUEDE RELLENAR

  Representante legal y DNI: SUNAT los publica en la ficha completa, que esta
  detras del captcha y que los republicadores cobran aparte. RNP (numero,
  capitulo, vigencia): es del OECE, no de SUNAT, y su consulta publica tambien
  lleva captcha. Esos campos los escribe el usuario; el formulario lo dice.

NO ES VERDAD ABSOLUTA

  Es un dato de terceros sobre un padron que cambia. Se rellena el formulario
  para ahorrar tipeo, el usuario lo ve antes de guardar, y el estado/condicion
  (ACTIVO/HABIDO) se le ensena para que decida. Nunca se guarda sin que pase
  por el formulario.
"""
from __future__ import annotations

import logging
import re
import time

import httpx

from shared.ubigeo import por_ubigeo

log = logging.getLogger("shared.sunat")

OPENRUC = "https://openruc.com/api/ruc/{ruc}"
APISNET = "https://api.apis.net.pe/v1/ruc"
TIEMPO_MAXIMO = 8.0
HEADERS = {"User-Agent": "LicitaPro/1.0 (+https://licitapro.sisac.pe)",
           "Accept": "application/json"}

# Cache en memoria de un dia: el mismo RUC se teclea varias veces (el usuario
# corrige, recarga, vuelve), y apis.net.pe cuenta cada llamada. Un dia es mas
# que suficiente para un dato que cambia pocas veces al ano.
_TTL = 24 * 3600
_cache: dict[str, tuple[float, dict | None]] = {}

_RE_RUC = re.compile(r"^(10|15|16|17|20)\d{9}$")


def ruc_valido(ruc: str | None) -> bool:
    """11 digitos con prefijo de SUNAT y digito verificador correcto."""
    if not ruc or not _RE_RUC.match(ruc):
        return False
    pesos = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    suma = sum(int(d) * p for d, p in zip(ruc[:10], pesos))
    verificador = (11 - suma % 11) % 10
    return verificador == int(ruc[10])


def _limpiar(valor) -> str | None:
    if valor is None:
        return None
    texto = re.sub(r"\s+", " ", str(valor)).strip(" -")
    return texto or None


def _normalizar(ruc: str, crudo: dict, fuente: str) -> dict:
    """Las dos fuentes nombran distinto; aqui queda una sola forma."""
    razon = crudo.get("razon_social") or crudo.get("razonSocial") or crudo.get("nombre")
    ubigeo = _limpiar(crudo.get("ubigeo"))
    lugar = por_ubigeo(ubigeo) or {}
    return {
        "ruc": ruc,
        "razon_social": _limpiar(razon),
        "estado": _limpiar(crudo.get("estado")),
        "condicion": _limpiar(crudo.get("condicion")),
        "direccion": _limpiar(crudo.get("direccion")),
        "ubigeo": ubigeo,
        "departamento": lugar.get("departamento"),
        "provincia": lugar.get("provincia"),
        "distrito": lugar.get("distrito"),
        "fuente": fuente,
    }


async def _openruc(cliente: httpx.AsyncClient, ruc: str) -> dict | None:
    r = await cliente.get(OPENRUC.format(ruc=ruc))
    if r.status_code == 404:
        return None
    r.raise_for_status()
    datos = r.json()
    return _normalizar(ruc, datos, "openruc") if datos.get("razon_social") else None


async def _apisnet(cliente: httpx.AsyncClient, ruc: str) -> dict | None:
    r = await cliente.get(APISNET, params={"numero": ruc})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    datos = r.json()
    return _normalizar(ruc, datos, "apis.net.pe") if datos.get("nombre") else None


async def consultar_ruc(ruc: str) -> dict | None:
    """Datos del RUC, o None si no existe o ninguna fuente respondio.

    Devuelve None tambien cuando las dos fuentes fallan: el formulario sigue
    sirviendo a mano, que es como funcionaba antes. Un fallo aqui no puede
    impedir dar de alta una empresa.
    """
    ruc = (ruc or "").strip()
    if not ruc_valido(ruc):
        return None
    ahora = time.monotonic()
    guardado = _cache.get(ruc)
    if guardado and ahora - guardado[0] < _TTL:
        return guardado[1]

    resultado = None
    async with httpx.AsyncClient(timeout=TIEMPO_MAXIMO, headers=HEADERS) as cliente:
        for fuente in (_openruc, _apisnet):
            try:
                resultado = await fuente(cliente, ruc)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("Consulta RUC %s por %s fallo: %s", ruc, fuente.__name__, e)
    # Se cachea tambien el None de "no existe", pero no el de "todo fallo":
    # ese merece reintentarse en la siguiente tecleada.
    if resultado is not None or guardado is None:
        _cache[ruc] = (ahora, resultado)
    return resultado
