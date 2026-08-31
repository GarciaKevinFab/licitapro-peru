"""Orquestador de scrapers -- ejecuta todos los scrapers de forma modular.

Fuentes implementadas (verificadas):
0. OCDS OECE           -- API oficial contratacionesabiertas.oece.gob.pe (PRINCIPAL,
                          tiempo real, unica con convocatorias vigentes)
1. SEACE 3.0           -- Buscador publico oficial OSCE (JSF AJAX). Bloqueado por
                          reCAPTCHA v3: se mantiene por si cambia, hoy devuelve 0.
2. GORE Portals        -- Portales regionales de cotizacion (MDD, etc.)
3. Peru Compras        -- Catalogo electronico y acuerdos marco
4. Poder Judicial      -- Portal de contrataciones PJ
5. EsSalud             -- Portal de contrataciones hospitalarias
6. SBS                 -- Superintendencia de Banca y Seguros
7. Transparencia       -- Consulta amigable MEF + PAC
8. Municipalidades     -- Portales de gobiernos locales
9. OCDS/CONOSCE        -- XLSX convocatorias de conosce.osce.gob.pe (datos abiertos OSCE)
10. CONOSCE Contratos  -- XLSX contratos + PAC de conosce.osce.gob.pe (Pentaho BI)
11. Datos Abiertos     -- datosabiertos.gob.pe CKAN API + XLSX resources
"""
import re
import html
import logging
import asyncio
import hashlib
from datetime import datetime, date
import httpx
from bs4 import BeautifulSoup
from shared.db import (
    upsert_licitacion, refrescar_licitacion, log_scraping_start,
    log_scraping_end, get_config, connection,
)

log = logging.getLogger("radar.orchestrator")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.5",
}

def _sopa(texto: str) -> BeautifulSoup:
    """BeautifulSoup con lxml si esta, y con el parser de la biblioteca si no.

    POR QUE NO SE EXIGE lxml

      Este modulo lo importa tambien el puente (tools/traer_oece.py), que corre
      en una PC de casa con Python 3.14. lxml no publica binario para toda
      version nueva de Python enseguida, y cuando falta, pip intenta COMPILARLO
      y muere pidiendo Microsoft Visual C++ 14.0. Eso dejaria el puente sin
      instalar por una dependencia que aqui no aporta nada.

      Comprobado sobre el portal de Madre de Dios: los dos parsers sacan las
      mismas 25 filas utiles. Para tablas asi de simples, el de la biblioteca
      estandar da igual, y viene siempre.

      Se prefiere lxml cuando esta porque es el que corre en el servidor, y que
      los dos usen el mismo evita perseguir una diferencia de parseo el dia que
      una pagina venga mal cerrada.
    """
    try:
        return BeautifulSoup(texto, "lxml")
    except Exception:
        return BeautifulSoup(texto, "html.parser")


# ==================== Utilidades compartidas ====================

def _gen_id(fuente: str, *parts) -> str:
    raw = f"{fuente}_{'_'.join(str(p) for p in parts)}".lower().strip()
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _parse_fecha(text: str):
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S",
                "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_monto(text: str):
    # Delega en el parser compartido: el anterior arrancaba todo caracter no
    # numerico y convertia "COT-123-2026" en S/ 1,232,026.
    from shared.config import parse_monto
    return parse_monto(text)


DEPTOS = {
    "MADRE DE DIOS": "Madre de Dios", "CUSCO": "Cusco", "JUNIN": "Junín",
    "JUNÍN": "Junín", "LIMA": "Lima", "AREQUIPA": "Arequipa", "PUNO": "Puno",
    "PIURA": "Piura", "LA LIBERTAD": "La Libertad", "LAMBAYEQUE": "Lambayeque",
    "LORETO": "Loreto", "UCAYALI": "Ucayali", "ANCASH": "Áncash",
    "CAJAMARCA": "Cajamarca", "HUANUCO": "Huánuco", "HUÁNUCO": "Huánuco",
    "ICA": "Ica", "TACNA": "Tacna", "MOQUEGUA": "Moquegua",
    "TUMBES": "Tumbes", "AMAZONAS": "Amazonas", "PASCO": "Pasco",
    "AYACUCHO": "Ayacucho", "APURIMAC": "Apurímac", "APURÍMAC": "Apurímac",
    "HUANCAVELICA": "Huancavelica", "SAN MARTIN": "San Martín",
    "SAN MARTÍN": "San Martín", "CALLAO": "Callao",
    "TAMBOPATA": "Madre de Dios", "PUERTO MALDONADO": "Madre de Dios",
    "HUANCAYO": "Junín", "SATIPO": "Junín",
}


# "INI:28/08/2026FIN:31/08/2026 12:00". La hora solo suele venir en el FIN, y
# a veces no viene: por eso va en un grupo opcional.
_RE_COTIZACION = r"{}:\s*(\d{{2}}/\d{{2}}/\d{{4}}(?:\s+\d{{1,2}}:\d{{2}})?)"


def _fechas_cotizacion(texto: str) -> tuple:
    """(publicacion, cierre) de la celda de fechas de un portal de cotizaciones.

    LA HORA DE CIERRE IMPORTA, Y SE ESTABA TIRANDO

      El portal publica "FIN:31/08/2026 12:00" y la expresion anterior solo
      capturaba la fecha, asi que la fila se guardaba con las 00:00. El panel
      filtra con `fecha_cierre > NOW()`, de modo que la convocatoria
      DESAPARECIA a medianoche del dia en que aun quedaban doce horas para
      presentarse.

      En una compra menor con tres dias de plazo eso es perder un tercio del
      plazo, y justo el tercio que vale: el ultimo dia es cuando quien la ve
      corre a presentarse. Comprobado sobre las 25 de Madre de Dios: tres
      cerraban ese mismo dia a las 18:00 y ya estaban invisibles por la
      manana.

    Devuelve None en la posicion que no venga, en vez de inventar una fecha.
    Una fecha inventada es peor que ninguna: el panel la trataria como buena.
    """
    fechas = []
    for etiqueta in ("INI", "FIN"):
        hallazgo = re.search(_RE_COTIZACION.format(etiqueta), texto or "")
        # Se normalizan los espacios antes de parsear: entre la fecha y la hora
        # puede venir mas de uno, y strptime no perdona eso.
        fechas.append(_parse_fecha(" ".join(hallazgo.group(1).split()))
                      if hallazgo else None)
    return fechas[0], fechas[1]


def _detectar_depto(texto: str) -> str | None:
    upper = texto.upper()
    for key, val in DEPTOS.items():
        if key in upper:
            return val
    return None


def _detectar_tipo_entidad(entidad: str) -> str:
    e = entidad.upper()
    if "GOBIERNO REGIONAL" in e or "GORE" in e:
        return "gore"
    if "MUNICIPALIDAD" in e:
        return "muni"
    if "MINISTERIO" in e:
        return "min"
    if "UNIVERSIDAD" in e:
        return "univ"
    if any(x in e for x in ["HOSPITAL", "ESSALUD", "RED DE SALUD", "SALUD"]):
        return "hosp"
    if any(x in e for x in ["PODER JUDICIAL", "CORTE SUPERIOR", "CORTE SUPREMA"]):
        return "pj"
    if any(x in e for x in ["EJERCITO", "MARINA", "FUERZA AEREA", "PNP", "POLICIA"]):
        return "ffaa"
    if "SBS" in e or "SUPERINTENDENCIA DE BANCA" in e:
        return "sbs"
    return "otro"


def _match_keywords(texto: str, keywords: list[str]) -> bool:
    # Delega en el helper compartido, que ignora tildes: las fuentes de OSCE
    # traen la acentuacion corrupta y el match con tilde nunca ocurria.
    from shared.config import match_keywords
    return match_keywords(texto, keywords)


def _detectar_tipo_proc(nomenclatura: str) -> str:
    n = nomenclatura.upper()
    if "LP-" in n or "LICITACION" in n:
        return "LP"
    if "AS-" in n or "ADJUDICACION SIMPLIFICADA" in n:
        return "AS"
    if "SIE-" in n or "SUBASTA" in n:
        return "SIE"
    if "CP-" in n or "CONCURSO" in n:
        return "CP"
    if "CD-" in n or "CONTRATACION DIRECTA" in n:
        return "CD"
    if "COMPARACION" in n or "CDP" in n:
        return "CdP"
    if "COTIZACION" in n:
        return "cotizacion"
    return "otro"


async def _get_filters(user_id: int) -> dict:
    config = await get_config(user_id)
    return {
        "keywords": config["keywords"] if config else [],
        "keywords_excluir": (config["keywords_excluir"] if config else []) or [],
        "regiones": config["regiones"] if config else [],
        "monto_min": config["monto_min"] if config else 0,
        "monto_max": config["monto_max"] if config else 999999999,
    }


# Noise words that indicate navigation items, not actual procurement content
NOISE_PATTERNS = [
    "portal de transparencia", "tv en vivo", "directorio telefonico",
    "imagen institucional", "servicios en linea", "documentos de transparencia",
    "gerencias y oficinas", "informacion institucional", "mapa del sitio",
    "mesa de partes", "libro de reclamaciones", "inicio", "home",
    "nosotros", "contacto", "facebook", "twitter", "youtube",
    "declaraciones juradas", "ordenanzas regionales", "resoluciones",
    "convocatoria cas", "convocatorias cas", "resultados cas",
    "directorio", "galeria", "noticias", "agenda", "eventos",
    "acceso a la informacion", "plan operativo", "presupuesto institucional",
    "rendicion de cuentas", "audiencia publica", "gestion por procesos",
]


def _apply_filters(entidad, objeto, monto, depto, filters) -> bool:
    """True = passes filters (should be processed)."""
    regiones = filters["regiones"]
    keywords = filters["keywords"]
    monto_min = filters["monto_min"]
    monto_max = filters["monto_max"]

    # Quality filter: reject items that are nav links or garbage
    if len(objeto) < 20:
        return False

    obj_lower = objeto.lower()
    if any(nw in obj_lower for nw in NOISE_PATTERNS):
        return False

    if regiones and depto and depto not in regiones:
        return False
    if monto and (monto < monto_min or monto > monto_max):
        return False
    if keywords and not _match_keywords(f"{objeto} {entidad}", keywords):
        return False
    # Exclusiones del usuario. Van DESPUES del match positivo: sirven para
    # matar falsos positivos por homonimia -- "servidores" matchea tanto un
    # servidor informatico como los servidores publicos de una entidad.
    excluir = filters.get("keywords_excluir") or []
    if excluir and _match_keywords(f"{objeto} {entidad}", excluir):
        return False
    return True


# ==================== Sonda de fuentes ====================

def _corto(url: str) -> str:
    """Host y ultimo tramo de la URL. Los partes se leen en un movil."""
    sin_esquema = re.sub(r"^https?://", "", url).rstrip("/")
    partes = sin_esquema.split("/")
    if len(partes) <= 2:
        return "/".join(partes)
    return f"{partes[0]}/.../{partes[-1]}"


# Por debajo de esto una respuesta no puede llevar una tabla de convocatorias:
# es una pagina de bienvenida de servidor, un error maquillado o un redirector.
_PAGINA_MINIMA = 2000


class Sonda:
    """Deja constancia de que paso con cada URL de una fuente.

    POR QUE EXISTE

      Los scrapers de HTML compartian esta linea:

          resp = await client.get(url)
          if resp.status_code != 200:
              continue

      Con ella, un 404 y un dia sin convocatorias acaban exactamente igual:
      `0 encontradas, 0 errores`, status 'done'. Comprobado contra produccion:
      cinco fuentes llevaban 21 pasadas seguidas anotando eso mismo mientras
      sus URLs devolvian 404 y 500, y el parte diario las listaba como "Sin
      nuevas" -- que es la frase que se usa para una fuente SANA en domingo.

      La sonda no arregla ninguna fuente. Hace que se note cual esta rota, que
      es lo que faltaba para poder decidir cual merece la pena arreglar.

    LAS DOS AVERIAS QUE SEPARA, Y POR QUE NO SE PUEDEN MEZCLAR

      CAIDA        Ninguna URL respondio 200. La pagina ya no existe, redirige
                   en bucle, o el host no acepta la conexion. Se arregla
                   cambiando la URL.
      SIN EXTRAER  Las paginas responden y no sale ni una fila. El sitio se
                   rediseno y los selectores apuntan a algo que ya no esta. Se
                   arregla mirando el HTML, no la URL.

      Mandan a sitios distintos, asi que se nombran distinto. Un aviso con el
      diagnostico equivocado hace perder la tarde igual que no tener aviso.
    """

    def __init__(self, fuente: str):
        self.fuente = fuente
        self.vivas = 0
        self.paginas: list[tuple[str, int]] = []
        self.fallos: list[str] = []

    @property
    def errores(self) -> int:
        """URLs que no sirvieron. Suma a los errores que ya cuenta el scraper."""
        return len(self.fallos)

    async def get(self, client, url: str):
        """Pide la URL. Devuelve la respuesta, o None si no sirve.

        DEVUELVE None EN VEZ DE LANZAR, A PROPOSITO

          Una fuente con tres URLs de las que dos han muerto tiene que seguir
          leyendo la tercera. Quien llama conserva su `continue`; lo unico que
          cambia es que ahora queda escrito por que.
        """
        try:
            resp = await client.get(url)
        except Exception as e:
            # El fallo se GUARDA (va a `scraping_log` en el diagnostico) y
            # ademas se registra: la tabla sirve para enterarse de que algo se
            # rompio, y el log para ver el mensaje entero al arreglarlo.
            self.fallos.append(f"{_corto(url)}: {type(e).__name__}")
            log.debug("%s: %s fallo con %s", self.fuente, url, e)
            return None
        if resp.status_code != 200:
            self.fallos.append(f"{_corto(url)}: HTTP {resp.status_code}")
            return None
        self.vivas += 1
        # Se guarda el tamano DE CADA URL, no un total ni un maximo: con dos
        # paginas vivas, una caida de 703 bytes y otra sana de 55 KB, cualquier
        # cifra agregada esconde justo la que hay que mirar. Ver diagnostico().
        self.paginas.append((_corto(url), len(resp.content)))
        return resp

    def diagnostico(self, encontradas: int) -> str | None:
        """Que contar en `scraping_log.error_detalle`. None si la pasada fue normal.

        Se escribe en la tabla y no solo en el log del contenedor porque el log
        se pierde con el reinicio y nadie lo abre. `scraping_log` ya es la
        fuente de verdad que consulta el vigilante: anadir un segundo sitio
        donde mirar es la forma de que no se mire ninguno.
        """
        if self.vivas == 0 and self.fallos:
            return "CAIDA -- " + "; ".join(self.fallos[:4])
        if self.vivas and encontradas == 0:
            # DOS COSAS MUY DISTINTAS DAN "200 Y CERO FILAS"
            #
            #   Paso el mismo dia que se escribio esto: el portal de
            #   cotizaciones de Madre de Dios dejo de servir su aplicacion y su
            #   IIS empezo a contestar la pagina de bienvenida por defecto --
            #   703 bytes, titulo "IIS Windows Server", 200 OK.
            #
            #   Decir ahi "revisar los selectores" manda a leer HTML durante
            #   media hora para descubrir que no hay nada que leer: la averia
            #   es de la entidad y lo unico que cabe es esperar o avisarles.
            #
            #   El tamano lo separa sin ambiguedad -- una tabla de
            #   convocatorias pesa decenas de miles de bytes, una pagina por
            #   defecto no llega a dos mil -- pero SOLO si se mira por URL. El
            #   primer intento uso el maximo de las dos, y los 55 KB de Junin
            #   taparon los 703 de Madre de Dios: el aviso volvia a mandar al
            #   sitio equivocado.
            #
            #   Las caidas van primero porque el parte recorta el detalle.
            caidas, redisenos = [], []
            for url, tam in self.paginas:
                if tam < _PAGINA_MINIMA:
                    caidas.append(f"{url}: {tam} bytes, demasiado pequena "
                                  f"(la entidad tiene la aplicacion caida y su "
                                  f"servidor da la pagina por defecto)")
                else:
                    redisenos.append(f"{url}: {tam} bytes, revisar selectores")
            aviso = (f"SIN EXTRAER -- {self.vivas} URL(s) respondieron 200 y no "
                     f"salio ni una fila | " + " ; ".join((caidas + redisenos)[:3]))
            if self.fallos:
                aviso += " | ademas " + "; ".join(self.fallos[:3])
            return aviso
        if self.fallos:
            return "PARCIAL -- " + "; ".join(self.fallos[:4])
        return None


# ==================== ORQUESTADOR PRINCIPAL ====================

# FUENTES APAGADAS, Y POR QUE. El codigo se queda; la linea en `scrapers` no.
#
#   COMPROBADO CONTRA PRODUCCION (30/08/2026, 21 pasadas seguidas)
#
#     essalud, sbs, peru_compras, transparencia_mef y municipalidades anotaron
#     `0 encontradas, 0 errores` en TODAS. No es que no hubiera convocatorias:
#     sus URLs devuelven 404 y 500. El `continue` mudo que tenian las hacia
#     indistinguibles de una fuente sana en domingo, y el parte diario las
#     listaba como "Sin nuevas" durante semanas.
#
#   LO DECISIVO NO ES QUE ESTEN ROTAS, ES QUE NO APORTAN NADA
#
#     Las mismas entidades ya entran por `ocds_oece`, que es la API de SEACE:
#
#       Municipalidades  388 licitaciones (91 abiertas)
#       Gobiernos Reg.   153 (27)
#       EsSalud           13 (4)
#       Poder Judicial     4 (1)
#       SBS                1 (0)
#
#     Arreglarles la URL seria trabajo para volver a traer por HTML, sin monto
#     y sin plazo, lo que ya llega estructurado. Apagarlas ahorra 21 peticiones
#     por hora y deja de ensuciar el parte.
#
#   NO SE BORRA EL CODIGO A PROPOSITO
#
#     Si alguna publica algo que SEACE no recoja -- compras menores, que es
#     justo el caso de gore_portals -- se reengancha anadiendo su linea a
#     `scrapers`. Borrarlas perderia el trabajo hecho y, sobre todo, la razon
#     por la que se apagaron, que es lo que hace que alguien las reescriba
#     dentro de un ano.
FUENTES_APAGADAS = {
    "seace_3.0": "reCAPTCHA v3, y es la web del mismo SEACE que ya se lee por "
                 "API en ocds_oece",
    "peru_compras": "perucompras.gob.pe se mudo a gob.pe; sus procesos ya "
                    "entran por ocds_oece",
    "poder_judicial": "sus dos URLs mueren (redireccion en bucle y conexion "
                      "rechazada); ya entra por ocds_oece",
    "essalud": "sus URLs dan 404 y 500; ya entra por ocds_oece",
    "sbs": "la pagina responde pero no publica ninguna tabla; ya entra por "
           "ocds_oece",
    "transparencia_mef": "la consulta amigable no publica convocatorias; ya "
                         "entran por ocds_oece",
    "municipalidades": "dos de tres portales dan 404 y el tercero redirige a "
                       "gob.pe; ya entran por ocds_oece",
    # Esta NO se apaga por redundante, sino por inservible, y la diferencia
    # importa si alguien la retoma:
    #
    #   Guardaba 17 filas que NINGUN cliente podia ver -- fichas del catalogo
    #   sin fecha de cierre, que el panel descarta -- mientras el parte
    #   anunciaba "17 nuevas". Eso ya se corrigio: ahora no guarda nada y dice
    #   por que. Pero lo que queda es una fuente que descarga "PAC 2015
    #   INICIAL.xlsx" y no encuentra ni columna de entidad.
    #
    #   MERECERIA LA PENA REACTIVARLA si se resuelve el descubrimiento de
    #   datasets: el Plan Anual de Contrataciones dice lo que cada entidad
    #   PIENSA comprar este ano, que es adelantarse a la convocatoria. Hoy el
    #   descubrimiento aterriza en ficheros de hace once anos, asi que es
    #   trabajo de producto, no una URL que cambiar.
    "datos_abiertos": "descarga PAC de 2015 y no extrae ni una convocatoria; "
                      "las 17 filas que guardaba eran fichas de catalogo",
}

# FUENTES QUE SOLO FUNCIONAN DESDE UNA CONEXION PERUANA.
#
#   `gore_portals` NO esta rota y NO es redundante: el portal de cotizaciones
#   de Madre de Dios publica compras menores a 8 UIT, que no llegan a SEACE y
#   por tanto no las trae `ocds_oece`. Comprobado desde una linea peruana:
#   responde 200 y el scraper extrae 25 convocatorias vigentes.
#
#   Desde el VPS no se alcanza. Es el mismo bloqueo por origen que ya obligo a
#   montar el puente para OECE, y por eso la cosecha se hace alli
#   (tools/traer_oece.py) en vez de aqui.
#
#   NO SE DEJA TAMBIEN EN LA PASADA DEL SERVIDOR, aunque no costaria nada:
#   anotaria "CAIDA" cada hora sobre una fuente que SI se esta cosechando. Un
#   aviso que salta cuando todo va bien deja de leerse, y entonces tampoco se
#   lee el dia que la averia es de verdad.
FUENTES_DEL_PUENTE = {
    "gore_portals": "solo responde a conexiones peruanas",
    # ocds_oece SI se sigue intentando desde el servidor, por si algun dia
    # cambia el enrutado. Esta aqui por lo otro que implica la lista: su fallo
    # desde el VPS es ESPERADO y no se reporta como novedad. Ver _diagnosticos.
    "ocds_oece": "OECE devuelve 403 al VPS; cosecha el puente peruano",
}


async def _diagnosticos(fuentes) -> dict:
    """El ultimo `error_detalle` de cada fuente de esta pasada, si lo hay.

    POR QUE SE LEE DE LA BASE EN VEZ DE DEVOLVERLO CADA SCRAPER

      Los `_run_*` devuelven una lista de licitaciones nuevas y nada mas.
      Cambiar esa firma obligaria a tocar las once. La pasada ACABA de escribir
      su fila en `scraping_log`, que ya es la fuente de verdad que consulta el
      vigilante: leer de alli no inventa un segundo sitio donde mirar.

    POR QUE NO SE FILTRA POR HORA

      `fin` se escribe con `NOW()` de PostgreSQL, que en esta base va en UTC.
      Compararlo contra un `datetime.now()` de Python es el desfase de cinco
      horas que ya mordio a este proyecto una vez. La ultima fila de cada
      fuente es la de esta pasada porque se acaba de escribir, y eso no
      necesita reloj.

    POR QUE SE CALLAN LAS FUENTES DEL PUENTE

      `ocds_oece` deja un fallo en CADA pasada del servidor: OECE le devuelve
      403 por ir por una IP de fuera de Peru. Es una condicion conocida,
      permanente y ya resuelta -- la cosecha la hace el puente --, asi que
      reportarla cada hora seria un aviso que salta siempre. Y un aviso que
      salta siempre no se lee el dia que dice algo nuevo, que es exactamente
      la averia que esta funcion existe para evitar.

      Que esa fuente este viva lo vigila `shared/vigilancia.py`, y lo vigila
      mejor: mide horas desde la ultima cosecha que DE VERDAD leyo algo, en
      vez de quejarse de cada intento fallido del servidor.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT DISTINCT ON (fuente) fuente, error_detalle
                 FROM scraping_log
                WHERE fin IS NOT NULL
                ORDER BY fuente, id DESC""")
    return {f["fuente"]: f["error_detalle"] for f in filas
            if f["error_detalle"] and f["fuente"] in fuentes
            and f["fuente"] not in FUENTES_DEL_PUENTE}



async def run_all_scrapers(user_id: int = 0) -> dict:
    """Ejecuta todos los scrapers disponibles. Si uno falla, los otros siguen."""
    results = {
        "timestamp": datetime.now().isoformat(),
        "total_nuevas": 0,
        "por_fuente": {},
        "errores": [],
    }

    # POR QUE ESTA LISTA TIENE UNA FUENTE Y NO ONCE
    #
    #   Ver FUENTES_APAGADAS y FUENTES_DEL_PUENTE, arriba. Resumido: siete
    #   estaban muertas Y eran redundantes, una guardaba fichas de catalogo
    #   que nadie podia ver, y la que aporta el dato que SEACE no publica
    #   (gore_portals) no responde a este servidor y viaja por el puente.
    #
    #   Queda ocds_oece, que es la API de SEACE y trae el 98% de todo. Que la
    #   lista sea corta no es un empobrecimiento: antes eran once nombres de
    #   los que diez aportaban cero, y el parte los listaba a todos como si
    #   trabajaran.
    #
    #   La pasada NO sobra aunque la lista sea corta: despues del scrapeo
    #   puntua lo recien capturado, y sin eso las licitaciones del puente se
    #   quedarian con `score_viabilidad` en NULL y el panel las mandaria al
    #   final de la lista.
    scrapers = [
        # Fuente principal: unica que entrega convocatorias vigentes.
        ("ocds_oece", _run_ocds_oece),
        # ocds_conosce y conosce_contratos quedaron FUERA de las alertas: son
        # volcados XLSX con retraso y producian 0 convocatorias vigentes (291
        # filas, ninguna postulable). Sus datos con monto se migraron a
        # historico_precios, que es donde si valen: alimentan el estimador de
        # precios de prep_bot. Las funciones siguen definidas para reengancharlas
        # a esa tabla cuando toque.
    ]

    for nombre, func in scrapers:
        try:
            nuevas = await func(user_id)
            count = len(nuevas) if nuevas else 0
            results["por_fuente"][nombre] = count
            results["total_nuevas"] += count
            log.info(f"[OK] {nombre}: {count} nuevas")
        except Exception as e:
            results["errores"].append(f"{nombre}: {str(e)[:120]}")
            results["por_fuente"][nombre] = -1
            log.error(f"[FAIL] {nombre}: {e}")

        await asyncio.sleep(2)

    # Puntuar lo recien capturado. Sin esto las licitaciones quedan con
    # score_viabilidad en NULL y el bot las lista por fecha, no por relevancia.
    try:
        from radar_bot.scorer import recalcular_scores_pendientes
        results["scoreadas"] = await recalcular_scores_pendientes()
    except Exception as e:
        results["errores"].append(f"scoring: {str(e)[:120]}")
        results["scoreadas"] = 0
        log.error(f"[FAIL] scoring: {e}")

    # Que fuente se quejo, en cristiano. Sin esto el diagnostico se queda
    # escrito en una tabla que solo se mira cuando ya hay un cliente enfadado.
    try:
        results["diagnosticos"] = await _diagnosticos(results["por_fuente"])
    except Exception as e:
        results["diagnosticos"] = {}
        log.error(f"No se pudieron leer los diagnosticos: {e}")

    log.info(
        f"Scraping completo: {results['total_nuevas']} nuevas, "
        f"{results.get('scoreadas', 0)} scoreadas, "
        f"{len(results['errores'])} errores"
    )
    return results


# ==================== 1. SEACE 3.0 ====================

async def _run_seace(user_id):
    from radar_bot.scrapers.seace import scrape_seace
    return await scrape_seace(user_id)


# ==================== 2. GORE PORTALS ====================

# Registry of known GORE cotizaciones portals (verified working)
GORE_COTIZACIONES_PORTALS = {
    "Madre de Dios": {
        "url": "http://cotizaciones.regionmadrededios.gob.pe/",
        "type": "cotizaciones_app",
        "entidad": "Gobierno Regional de Madre de Dios",
    },
}

# Generic GORE portals (HTML scraping, lower reliability)
#
#   Cusco salio de esta lista: https://www.regioncusco.gob.pe/contrataciones/
#   devuelve 404 desde hace meses. Con la sonda ya no era un fallo invisible,
#   pero seguia siendo un aviso cada cuatro horas sobre algo que no se va a
#   arreglar solo. Un aviso que sale siempre no se lee nunca.
#
#   Junin responde 200 y no publica tabla: no aporta, pero tampoco miente. Se
#   deja por si vuelven a publicar ahi.
GORE_GENERIC_PORTALS = {
    "Junín": [
        "https://www.regionjunin.gob.pe/pagina/id/contrataciones_y_adquisiciones/",
    ],
}


async def _scrape_gore_cotizaciones_app(
    client: httpx.AsyncClient, region: str, portal_info: dict, filters: dict,
    sonda: "Sonda",
) -> tuple[int, int, list[dict]]:
    """Scrape a GORE cotizaciones web app (like regionmadrededios.gob.pe/cotizaciones).

    These portals have a structured table with columns:
    TIPO | ANO | NUM | RUBRO | CONCEPTO | FECHAS | ACCIONES

    Returns (encontradas, errores, nuevas).
    """
    url = portal_info["url"]
    entidad = portal_info["entidad"]
    encontradas = 0
    errores = 0
    nuevas = []

    try:
        resp = await sonda.get(client, url)
        if resp is None:
            return 0, 0, []

        soup = _sopa(resp.text)

        # Parse the main table
        table = soup.find("table")
        if not table:
            return 0, 0, []

        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 6:
                continue

            textos = [c.get_text(strip=True) for c in cells]

            # Expected format: TIPO | ANO | NUM | RUBRO | CONCEPTO | FECHAS | DETALLES
            tipo_bien = textos[0]  # BIENES or SERVICIOS
            anio = textos[1]
            numero = textos[2]
            rubro = textos[3]
            concepto = textos[4]
            fecha_raw = textos[5]

            # Validate: anio should be a 4-digit year
            if not (anio.isdigit() and len(anio) == 4):
                continue

            # Skip empty or too-short concepts
            if not concepto or len(concepto) < 10:
                continue

            encontradas += 1
            objeto = f"[{tipo_bien}] {concepto}"

            fecha_pub, fecha_cierre = _fechas_cotizacion(fecha_raw)

            # Get the detail link
            link = row.find("a", href=True)
            href = link["href"] if link else ""
            if href and not href.startswith("http"):
                base = url.rstrip("/")
                href = f"{base}/{href.lstrip('/')}"

            # Apply filters
            monto = None  # MDD cotizaciones don't show monto in listing
            if not _apply_filters(entidad, objeto, monto, region, filters):
                continue

            lid = _gen_id("gore", f"{anio}-{numero}", region)
            licit = {
                "id": f"gore_{lid}",
                "fuente": "gore_portals",
                "tipo": "cotizacion",
                "nomenclatura": f"COT-{numero}-{anio}-GOREMAD",
                "entidad": entidad,
                "entidad_tipo": "gore",
                "objeto": objeto[:500],
                "monto_referencial": monto,
                "departamento": region,
                "fecha_publicacion": fecha_pub,
                "fecha_cierre": fecha_cierre,
                "url": href or url,
                "estado": "convocado",
            }
            # SE REFRESCA, NO SOLO SE INSERTA
            #
            #   `upsert_licitacion` al reencontrar una fila solo toca `estado`:
            #   la fecha de cierre guardada la primera vez se queda para
            #   siempre. En un portal de cotizaciones los plazos se prorrogan,
            #   y ademas cualquier correccion nuestra del parseo -- como la
            #   hora de arriba -- no llegaria nunca a las filas ya guardadas.
            is_new = await refrescar_licitacion(licit)
            if is_new:
                nuevas.append(licit)

    except Exception as e:
        errores += 1
        log.warning(f"GORE {region} cotizaciones app: {e}")

    return encontradas, errores, nuevas


async def _scrape_gore_generic(
    client: httpx.AsyncClient, region: str, url: str, filters: dict,
    sonda: "Sonda",
) -> tuple[int, int, list[dict]]:
    """Scrape a generic GORE contrataciones page (WordPress/static HTML).

    Looks for actual procurement links (not navigation/menu items).
    Uses strict filtering to avoid false positives.
    """
    encontradas = 0
    errores = 0
    nuevas = []
    entidad = f"Gobierno Regional de {region}"

    try:
        resp = await sonda.get(client, url)
        if resp is None:
            return 0, 0, []

        soup = _sopa(resp.text)

        # Strategy: look for tables with actual procurement data
        for table in soup.find_all("table"):
            rows = table.find_all("tr")[1:]  # Skip header
            for row in rows:
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue

                textos = [c.get_text(strip=True) for c in cells]

                # Build object text from cells, but validate it's procurement content
                objeto = " | ".join(t for t in textos if 5 < len(t) < 500)
                if len(objeto) < 25:
                    continue

                # Must contain at least one procurement keyword
                procurement_keywords = [
                    "adquisicion", "servicio", "contratacion", "suministro",
                    "consultoria", "obra", "bienes", "equipos", "sistema",
                    "mantenimiento", "alquiler", "arrendamiento", "instalacion",
                    "construccion", "mejoramiento", "implementacion",
                ]
                obj_lower = objeto.lower()
                if not any(kw in obj_lower for kw in procurement_keywords):
                    continue

                encontradas += 1
                monto = None
                fecha_cierre = None
                for t in textos:
                    if not monto:
                        monto = _parse_monto(t)
                    if not fecha_cierre:
                        fecha_cierre = _parse_fecha(t)

                if not _apply_filters(entidad, objeto, monto, region, filters):
                    continue

                link = row.find("a", href=True)
                href = link["href"] if link else ""
                if href and not href.startswith("http"):
                    href = f"{url.rstrip('/')}/{href.lstrip('/')}"

                lid = _gen_id("gore", objeto[:40], region)
                licit = {
                    "id": f"gore_{lid}",
                    "fuente": "gore_portals",
                    "tipo": "cotizacion",
                    "entidad": entidad,
                    "entidad_tipo": "gore",
                    "objeto": objeto[:500],
                    "monto_referencial": monto,
                    "departamento": region,
                    "fecha_cierre": fecha_cierre,
                    "url": href or url,
                    "estado": "convocado",
                }
                is_new = await upsert_licitacion(licit)
                if is_new:
                    nuevas.append(licit)

        # Also look for structured list items (cards, articles) that are actual cotizaciones
        items = soup.find_all(["article", "div", "li"], class_=re.compile(
            r"cotizacion|convocatoria|proceso|licitacion", re.IGNORECASE
        ))
        for item in items:
            titulo_el = item.find(["a", "h3", "h4", "h5", "strong"])
            if not titulo_el:
                continue
            objeto = titulo_el.get_text(strip=True)
            if len(objeto) < 20:
                continue

            # Must contain procurement keywords
            obj_lower = objeto.lower()
            procurement_keywords = [
                "adquisicion", "servicio", "contratacion", "cotizacion",
                "licitacion", "adjudicacion", "concurso",
            ]
            if not any(kw in obj_lower for kw in procurement_keywords):
                continue

            encontradas += 1
            link = item.find("a", href=True)
            href = link["href"] if link else ""
            if href and not href.startswith("http"):
                href = f"{url.rstrip('/')}/{href.lstrip('/')}"

            if not _apply_filters(entidad, objeto, None, region, filters):
                continue

            lid = _gen_id("gore", objeto[:40], region)
            licit = {
                "id": f"gore_{lid}",
                "fuente": "gore_portals",
                "tipo": "cotizacion",
                "entidad": entidad,
                "entidad_tipo": "gore",
                "objeto": objeto[:500],
                "departamento": region,
                "url": href or url,
                "estado": "convocado",
            }
            is_new = await upsert_licitacion(licit)
            if is_new:
                nuevas.append(licit)

    except Exception as e:
        errores += 1
        log.debug(f"GORE {region} generic ({url}): {e}")

    return encontradas, errores, nuevas


async def _run_gore_portals(user_id):
    """Portales de Gobiernos Regionales -- cotizaciones y contrataciones."""
    log_id = await log_scraping_start("gore_portals")
    sonda = Sonda("gore_portals")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            # 1. Scrape dedicated cotizaciones apps (high quality)
            for region, portal_info in GORE_COTIZACIONES_PORTALS.items():
                if filters["regiones"] and region not in filters["regiones"]:
                    continue

                enc, err, new = await _scrape_gore_cotizaciones_app(
                    client, region, portal_info, filters, sonda
                )
                encontradas += enc
                errores += err
                nuevas.extend(new)
                await asyncio.sleep(1)

            # 2. Scrape generic GORE portals (lower quality, strict filtering)
            for region, urls in GORE_GENERIC_PORTALS.items():
                if filters["regiones"] and region not in filters["regiones"]:
                    continue

                for url in urls:
                    try:
                        enc, err, new = await _scrape_gore_generic(
                            client, region, url, filters, sonda
                        )
                        encontradas += enc
                        errores += err
                        nuevas.extend(new)
                    except Exception as e:
                        errores += 1
                        # Contar el fallo sin registrar la causa fue justo lo que
                        # mantuvo invisibles los bugs de esquema.
                        log.warning(f"fila descartada: {e}")
                    await asyncio.sleep(1)

    except Exception as e:
        errores += 1
        log.warning(f"GORE portals: {e}")

    detalle = sonda.diagnostico(encontradas)
    if detalle:
        log.warning("gore_portals: %s", detalle)
    await log_scraping_end(log_id, encontradas, len(nuevas),
                           errores + sonda.errores, detalle)
    return nuevas


# ==================== 3. PERU COMPRAS ====================

async def _run_peru_compras(user_id):
    """Peru Compras -- Catalogos Electronicos y Acuerdos Marco."""
    log_id = await log_scraping_start("peru_compras")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    try:
        async with httpx.AsyncClient(
            timeout=30, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            urls_to_try = [
                "https://www.perucompras.gob.pe/convocatorias.htm",
                "https://www.perucompras.gob.pe/subasta/listado.htm",
                "https://www.perucompras.gob.pe/acuerdomarco/listado.htm",
            ]

            for url in urls_to_try:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = _sopa(resp.text)

                    # Buscar tablas con datos de convocatorias
                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]  # Skip header
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            objeto = " ".join(textos[:3])
                            if len(objeto) < 15:
                                continue

                            encontradas += 1
                            entidad = "Peru Compras - Central de Compras Publicas"
                            depto = _detectar_depto(objeto)
                            monto = None
                            for t in textos:
                                m = _parse_monto(t)
                                if m and m > 100:
                                    monto = m
                                    break

                            if not _apply_filters(entidad, objeto, monto, depto, filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://www.perucompras.gob.pe{href}"

                            lid = _gen_id("pc", objeto[:40], str(encontradas))
                            licit = {
                                "id": f"pc_{lid}",
                                "fuente": "peru_compras",
                                "tipo": "acuerdo_marco",
                                "entidad": entidad,
                                "entidad_tipo": "otro",
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": depto,
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"Peru Compras: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 4. PODER JUDICIAL ====================

async def _run_poder_judicial(user_id):
    """Portal del Poder Judicial -- contrataciones y adquisiciones."""
    log_id = await log_scraping_start("poder_judicial")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://cea.pj.gob.pe/convocatorias",
        "https://cea.pj.gob.pe/procesos",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = _sopa(resp.text)

                    # Buscar tablas con procesos
                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            objeto = " | ".join(t for t in textos if len(t) > 5)
                            if len(objeto) < 20:
                                continue

                            encontradas += 1
                            entidad = "Poder Judicial del Peru"
                            depto = _detectar_depto(objeto)
                            monto = None
                            for t in textos:
                                m = _parse_monto(t)
                                if m and m > 100:
                                    monto = m
                                    break

                            if not _apply_filters(entidad, objeto, monto, depto, filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://cea.pj.gob.pe{href}"

                            lid = _gen_id("pj", objeto[:40])
                            licit = {
                                "id": f"pj_{lid}",
                                "fuente": "poder_judicial",
                                "tipo": _detectar_tipo_proc(objeto),
                                "entidad": entidad,
                                "entidad_tipo": "pj",
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": depto,
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"Poder Judicial: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 5. ESSALUD ====================

async def _run_essalud(user_id):
    """EsSalud -- Portal de contrataciones hospitalarias."""
    log_id = await log_scraping_start("essalud")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://www.essalud.gob.pe/contrataciones-y-adquisiciones/",
        "https://www.essalud.gob.pe/convocatorias/",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = _sopa(resp.text)

                    # Tablas de procesos
                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 2:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            objeto = " | ".join(t for t in textos if len(t) > 5)
                            if len(objeto) < 20:
                                continue

                            encontradas += 1
                            monto = None
                            fecha_cierre = None
                            for t in textos:
                                if not monto:
                                    monto = _parse_monto(t)
                                if not fecha_cierre:
                                    fecha_cierre = _parse_fecha(t)

                            depto = _detectar_depto(objeto)
                            if not _apply_filters("EsSalud", objeto, monto, depto, filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://www.essalud.gob.pe{href}"

                            lid = _gen_id("essalud", objeto[:40])
                            licit = {
                                "id": f"essalud_{lid}",
                                "fuente": "essalud",
                                "tipo": _detectar_tipo_proc(objeto),
                                "entidad": "EsSalud",
                                "entidad_tipo": "hosp",
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": depto,
                                "fecha_cierre": fecha_cierre,
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"EsSalud: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 6. SBS ====================

async def _run_sbs(user_id):
    """SBS -- Superintendencia de Banca, Seguros y AFP."""
    log_id = await log_scraping_start("sbs")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://www.sbs.gob.pe/app/pp/AdquisicionesYContrataciones/Paginas/LicitacionConvocatoria.aspx",
        "https://www.sbs.gob.pe/app/pp/AdquisicionesYContrataciones/",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = _sopa(resp.text)

                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 2:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            objeto = " | ".join(t for t in textos if len(t) > 5)
                            if len(objeto) < 20:
                                continue

                            encontradas += 1
                            monto = None
                            for t in textos:
                                m = _parse_monto(t)
                                if m and m > 100:
                                    monto = m
                                    break

                            if not _apply_filters("SBS", objeto, monto, "Lima", filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://www.sbs.gob.pe{href}"

                            lid = _gen_id("sbs", objeto[:40])
                            licit = {
                                "id": f"sbs_{lid}",
                                "fuente": "sbs",
                                "tipo": _detectar_tipo_proc(objeto),
                                "entidad": "Superintendencia de Banca, Seguros y AFP",
                                "entidad_tipo": "sbs",
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": "Lima",
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"SBS: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 7. TRANSPARENCIA MEF ====================

async def _run_transparencia_mef(user_id):
    """Consulta Amigable MEF y Portal de Transparencia."""
    log_id = await log_scraping_start("transparencia_mef")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    urls = [
        "https://www.transparencia.gob.pe/contrataciones/pte_transparencia_contrataciones.aspx",
    ]

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for url in urls:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    soup = _sopa(resp.text)

                    for table in soup.find_all("table"):
                        rows = table.find_all("tr")[1:]
                        for row in rows:
                            cells = row.find_all("td")
                            if len(cells) < 3:
                                continue

                            textos = [c.get_text(strip=True) for c in cells]
                            entidad = textos[0] if textos[0] else ""
                            objeto = textos[1] if len(textos) > 1 else ""
                            if not entidad or not objeto or len(objeto) < 20:
                                continue

                            encontradas += 1
                            monto = None
                            for t in textos[2:]:
                                m = _parse_monto(t)
                                if m:
                                    monto = m
                                    break

                            depto = _detectar_depto(f"{entidad} {objeto}")

                            if not _apply_filters(entidad, objeto, monto, depto, filters):
                                continue

                            link = row.find("a", href=True)
                            href = link["href"] if link else ""
                            if href and not href.startswith("http"):
                                href = f"https://www.transparencia.gob.pe{href}"

                            lid = _gen_id("transp", objeto[:40], entidad[:20])
                            licit = {
                                "id": f"transp_{lid}",
                                "fuente": "transparencia_mef",
                                "tipo": _detectar_tipo_proc(objeto),
                                "entidad": entidad,
                                "entidad_tipo": _detectar_tipo_entidad(entidad),
                                "objeto": objeto[:500],
                                "monto_referencial": monto,
                                "departamento": depto,
                                "url": href or url,
                                "estado": "convocado",
                            }
                            is_new = await upsert_licitacion(licit)
                            if is_new:
                                nuevas.append(licit)

                except Exception as e:
                    errores += 1
                    # Contar el fallo sin registrar la causa fue justo lo que
                    # mantuvo invisibles los bugs de esquema.
                    log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"Transparencia MEF: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 8. MUNICIPALIDADES ====================

async def _run_municipalidades(user_id):
    """Portales de municipalidades -- contrataciones locales."""
    log_id = await log_scraping_start("municipalidades")
    filters = await _get_filters(user_id)
    nuevas = []
    encontradas = 0
    errores = 0

    munis = {
        "Madre de Dios": [
            ("Municipalidad Provincial de Tambopata", "https://www.munitambopata.gob.pe/contrataciones"),
        ],
        "Junín": [
            ("Municipalidad Provincial de Huancayo", "https://www.munihuancayo.gob.pe/contrataciones"),
        ],
        "Cusco": [
            ("Municipalidad Provincial de Cusco", "https://www.cusco.gob.pe/contrataciones/"),
        ],
    }

    try:
        async with httpx.AsyncClient(
            timeout=15, headers=HEADERS, follow_redirects=True, verify=False
        ) as client:
            for region, portales in munis.items():
                if filters["regiones"] and region not in filters["regiones"]:
                    continue

                for entidad_name, url in portales:
                    try:
                        resp = await client.get(url)
                        if resp.status_code != 200:
                            continue

                        soup = _sopa(resp.text)

                        for table in soup.find_all("table"):
                            rows = table.find_all("tr")[1:]
                            for row in rows:
                                cells = row.find_all("td")
                                if len(cells) < 2:
                                    continue

                                textos = [c.get_text(strip=True) for c in cells]
                                objeto = " | ".join(t for t in textos if len(t) > 5)
                                if len(objeto) < 20:
                                    continue

                                # Must contain procurement keywords
                                obj_lower = objeto.lower()
                                prc_kw = [
                                    "adquisicion", "servicio", "contratacion",
                                    "suministro", "consultoria", "bienes", "obra",
                                ]
                                if not any(kw in obj_lower for kw in prc_kw):
                                    continue

                                encontradas += 1
                                monto = None
                                for t in textos:
                                    m = _parse_monto(t)
                                    if m and m > 50:
                                        monto = m
                                        break

                                if not _apply_filters(entidad_name, objeto, monto, region, filters):
                                    continue

                                lid = _gen_id("muni", objeto[:40], entidad_name[:20])
                                licit = {
                                    "id": f"muni_{lid}",
                                    "fuente": "municipalidades",
                                    "tipo": "cotizacion",
                                    "entidad": entidad_name,
                                    "entidad_tipo": "muni",
                                    "objeto": objeto[:500],
                                    "monto_referencial": monto,
                                    "departamento": region,
                                    "url": url,
                                    "estado": "convocado",
                                }
                                is_new = await upsert_licitacion(licit)
                                if is_new:
                                    nuevas.append(licit)

                    except Exception as e:
                        errores += 1
                        # Contar el fallo sin registrar la causa fue justo lo que
                        # mantuvo invisibles los bugs de esquema.
                        log.warning(f"fila descartada: {e}")

    except Exception as e:
        errores += 1
        log.warning(f"Municipalidades: {e}")

    await log_scraping_end(log_id, encontradas, len(nuevas), errores)
    return nuevas


# ==================== 0. OCDS OECE (API oficial, tiempo real) ====================

async def _run_ocds_oece(user_id):
    from radar_bot.scrapers.ocds_oece import scrape_ocds_oece
    # Sin user_id: el pozo es compartido y se filtra al leer.
    return await scrape_ocds_oece()


# ==================== 9. OCDS CONOSCE (Convocatorias XLSX) ====================

async def _run_ocds_conosce(user_id):
    from radar_bot.scrapers.ocds_api import scrape_ocds
    return await scrape_ocds(user_id)


# ==================== 10. CONOSCE Contratos + PAC ====================

async def _run_conosce_contratos(user_id):
    from radar_bot.scrapers.conosce import scrape_conosce
    return await scrape_conosce(user_id)


# ==================== 11. Datos Abiertos ====================

async def _run_datos_abiertos(user_id):
    from radar_bot.scrapers.datos_abiertos import scrape_datos_abiertos
    return await scrape_datos_abiertos(user_id)


# ==================== FORMAT REPORT ====================

def format_scraping_report(results: dict) -> str:
    """Formatea resultados de scraping para Telegram."""
    lines = [f"<b>Reporte de Scraping</b> -- {results['timestamp'][:16]}\n"]

    fuente_labels = {
        "ocds_oece": "OCDS OECE (principal)",
        "seace_3.0": "SEACE 3.0",
        "gore_portals": "GOREs Regionales",
        "peru_compras": "Peru Compras",
        "poder_judicial": "Poder Judicial",
        "essalud": "EsSalud",
        "sbs": "SBS",
        "transparencia_mef": "Transparencia MEF",
        "municipalidades": "Municipalidades",
        "ocds_conosce": "OCDS/CONOSCE",
        "conosce_contratos": "CONOSCE Contratos+PAC",
        "datos_abiertos": "Datos Abiertos",
    }

    # "Sin nuevas" SE RESERVA PARA LAS FUENTES SANAS
    #
    #   Antes lo decia tambien una fuente cuya URL llevaba semanas dando 404,
    #   porque las dos cosas acababan en `count == 0`. Esa frase es justo la
    #   que hace que nadie mire: suena a domingo tranquilo. Cuando la sonda
    #   dejo dicho que la fuente esta caida, se dice eso y se pega el motivo.
    diagnosticos = results.get("diagnosticos") or {}

    for fuente, count in results["por_fuente"].items():
        label = fuente_labels.get(fuente, fuente)
        detalle = diagnosticos.get(fuente)
        if count == -1:
            status = "Error"
        elif detalle and detalle.startswith("CAIDA"):
            status = "CAIDA"
        elif detalle and detalle.startswith("SIN EXTRAER"):
            status = "responde pero no extrae nada"
        elif count == 0:
            status = "Sin nuevas"
        else:
            status = f"{count} nuevas"
        lines.append(f"  {label}: {status}")
        if detalle:
            # El detalle trae URLs, y una URL con & rompe el parse_mode HTML
            # de Telegram: el mensaje no llega y el fallo se ve como silencio,
            # que es la averia que este parte existe para evitar.
            lines.append(f"      <i>{html.escape(detalle[:160])}</i>")

    lines.append(f"\n<b>Total nuevas: {results['total_nuevas']}</b>")

    if results["errores"]:
        lines.append(f"Errores: {len(results['errores'])}")

    return "\n".join(lines)
