"""Pedir a un portal lo que su propio HTML dice, sin fiarse del HTML.

POR QUE EXISTE

  Los documentos de una cotizacion (la SC, la DJ, las EETT) se bajan de la URL
  que trae el `href` de la ficha, o el campo `expediente` de una API publica.
  Ese texto lo escribe un servidor ajeno: si manana el portal del GOREMAD
  aparece con un enlace a http://169.254.169.254/ o a http://localhost:5432/,
  el radar lo pediria desde el VPS -- que SI alcanza la red interna, la base y
  los metadatos del proveedor -- y guardaria la respuesta como si fuera un PDF.
  Es un SSRF de manual, y lo dispara cualquiera que logre publicar un enlace
  en el portal de una entidad.

  Ademas los clientes se abrian con `verify=False` y `follow_redirects=True`:
  un intermediario podia servir lo que quisiera, y un 302 a cualquier host se
  seguia sin mirar.

LO QUE HACE `bajar`

  1. Solo http/https, solo puertos 80/443, y solo hosts del dominio del portal
     de donde salio el enlace (cada scraper pasa su lista).
  2. Resuelve el host y rechaza si ALGUNA de sus IP no es publica (privada,
     loopback, link-local, reservada, multicast). Se resuelve en un hilo:
     `getaddrinfo` bloquea y el radar es un solo bucle asyncio.
  3. No sigue redirecciones a ciegas: cada Location se revalida con 1 y 2, y
     como mucho MAX_REDIRECCIONES saltos.
  4. Lee en streaming y corta en cuanto pasa del tope: un "PDF" de 2 GB no
     llega a la memoria.

  Queda una ventana entre la resolucion de 2 y la conexion (DNS rebinding).
  Cerrarla exige fijar la IP en la conexion, que rompe el SNI de TLS; con el
  filtro de dominio delante, para colarse hay que controlar el DNS del propio
  portal del Estado. Se acepta.

TLS: `cliente`

  `verify=False` se aplicaba a clientes enteros. Medido el 2026-10-20 con un
  handshake contra cada host (certifi): todos presentan una cadena valida
  salvo cotizaciones.regionmadrededios.gob.pe, que sirve un certificado
  AUTOFIRMADO. No hay CA intermedia que agregar: no hay CA. Ese host se pide
  por http:// (GORE_COTIZACIONES_PORTALS), asi que la excepcion solo aplica si
  algun dia redirige a https. `cliente` monta un transporte sin verificar SOLO
  para los hosts de HOSTS_TLS_AUTOFIRMADO; cualquier otro host, incluido un
  redirect a otro sitio, va con la verificacion normal.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

import httpx

log = logging.getLogger("radar.red_segura")

MAX_REDIRECCIONES = 3
_ESQUEMAS = {"http", "https"}
_PUERTOS = {None, 80, 443}
_REDIRECCIONES = {301, 302, 303, 307, 308}

# host -> POR QUE se le acepta un certificado que no verifica. Agregar uno
# aqui exige haberlo medido (handshake fallido con certifi) y anotarlo.
HOSTS_TLS_AUTOFIRMADO: dict[str, str] = {
    "cotizaciones.regionmadrededios.gob.pe": (
        "certificado autofirmado (medido 2026-10-20); el portal se pide por "
        "http y solo importa si redirige a https"),
}

_avisados: set[str] = set()


class DescargaRechazada(Exception):
    """La URL no se pide, o lo que devolvio no se acepta. El mensaje dice por que."""


def _host(url: httpx.URL) -> str:
    return (url.host or "").lower().rstrip(".")


def _del_dominio(host: str, dominios) -> bool:
    for d in dominios:
        d = d.lower().strip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def validar_url(url: str | httpx.URL, dominios) -> httpx.URL:
    """La URL como httpx.URL si se puede pedir; si no, DescargaRechazada.

    Solo mira el texto: esquema, puerto y dominio. La resolucion DNS va
    aparte (`exigir_ip_publica`) porque es de red y bloquea.
    """
    try:
        u = url if isinstance(url, httpx.URL) else httpx.URL(str(url).strip())
    except Exception as e:
        raise DescargaRechazada(f"URL invalida: {e}") from e
    if u.scheme not in _ESQUEMAS:
        raise DescargaRechazada(f"esquema {u.scheme!r} no permitido")
    if u.port not in _PUERTOS:
        raise DescargaRechazada(f"puerto {u.port} no permitido")
    host = _host(u)
    if not host:
        raise DescargaRechazada("URL sin host")
    if not _del_dominio(host, dominios):
        raise DescargaRechazada(f"host {host} fuera de {', '.join(dominios)}")
    return u


def _ip_publica(ip: str) -> bool:
    try:
        direccion = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(direccion, ipaddress.IPv6Address) and direccion.ipv4_mapped:
        direccion = direccion.ipv4_mapped
    return direccion.is_global and not direccion.is_multicast


async def _resolver(host: str, puerto: int) -> list[str]:
    """Las IP del host. Aparte para que las pruebas no toquen la red."""
    infos = await asyncio.to_thread(socket.getaddrinfo, host, puerto,
                                    type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def exigir_ip_publica(u: httpx.URL) -> None:
    host = _host(u)
    puerto = u.port or (443 if u.scheme == "https" else 80)
    try:
        ips = await _resolver(host, puerto)
    except OSError as e:
        raise DescargaRechazada(f"{host} no resuelve: {e}") from e
    if not ips:
        raise DescargaRechazada(f"{host} no resuelve")
    malas = [ip for ip in ips if not _ip_publica(ip)]
    if malas:
        # TODAS tienen que ser publicas: con una privada entre varias, la
        # conexion puede caer justo en esa.
        raise DescargaRechazada(f"{host} resuelve a una IP no publica ({malas[0]})")


async def bajar(client: httpx.AsyncClient, url: str, dominios, maximo: int) -> bytes:
    """El cuerpo de `url` si cumple todo lo de arriba; si no, DescargaRechazada.

    Un 4xx/5xx tambien es DescargaRechazada (con el codigo): quien llama
    registra la causa igual, sea la red, el portal o el filtro.
    """
    actual = validar_url(url, dominios)
    for _ in range(MAX_REDIRECCIONES + 1):
        await exigir_ip_publica(actual)
        async with client.stream("GET", actual, follow_redirects=False) as resp:
            if resp.status_code in _REDIRECCIONES:
                destino = resp.headers.get("location")
                if not destino:
                    raise DescargaRechazada(f"HTTP {resp.status_code} sin Location")
                actual = validar_url(actual.join(destino), dominios)
                continue
            if resp.status_code != 200:
                raise DescargaRechazada(f"HTTP {resp.status_code}")
            declarado = resp.headers.get("content-length")
            if declarado and declarado.isdigit() and int(declarado) > maximo:
                raise DescargaRechazada(f"{declarado} bytes, tope {maximo}")
            partes, total = [], 0
            async for trozo in resp.aiter_bytes():
                total += len(trozo)
                if total > maximo:
                    raise DescargaRechazada(f"mas de {maximo} bytes")
                partes.append(trozo)
            return b"".join(partes)
    raise DescargaRechazada(f"mas de {MAX_REDIRECCIONES} redirecciones")


def cliente(**kwargs) -> httpx.AsyncClient:
    """AsyncClient con TLS verificado salvo en los hosts de HOSTS_TLS_AUTOFIRMADO.

    Los `mounts` de httpx eligen el transporte por esquema y host: el que no
    verifica solo se usa para https://<ese host>, nunca para otro.
    """
    mounts = {}
    for host, motivo in HOSTS_TLS_AUTOFIRMADO.items():
        mounts[f"https://{host}"] = httpx.AsyncHTTPTransport(verify=False)
        if host not in _avisados:
            _avisados.add(host)
            log.warning("TLS sin verificar SOLO para %s: %s", host, motivo)
    return httpx.AsyncClient(mounts=mounts, **kwargs)
