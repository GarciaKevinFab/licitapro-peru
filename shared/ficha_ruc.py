"""Lee el "Reporte de Ficha RUC" de SUNAT (PDF) para rellenar el alta de empresa.

POR QUE LA FICHA Y NO LA CONSULTA RUC

  La consulta en linea trae razon social y direccion (shared/sunat.py), pero
  el representante legal, su DNI y su cargo solo estan en la ficha completa,
  que SUNAT entrega en PDF al contribuyente. Es un documento que el usuario ya
  tiene y que sube el mismo: no se consulta nada a terceros.

COMO SE LEE

  pypdf extrae el texto, y el orden NO es el visual: la ficha es una tabla y
  las columnas salen intercaladas. Por eso no se lee "la linea siguiente a la
  etiqueta" sino patrones anclados en lo que no cambia: el RUC de 11 digitos
  tras "Reporte de Ficha RUC", "Departamento <X>" en el domicilio fiscal, o el
  bloque "DOC. NACIONAL DE / IDENTIDAD/LE / <8 digitos> / <nombre>" del
  representante. Lo que no se encuentra se devuelve vacio, nunca inventado.

TAMBIEN LA "CONSULTA RUC"

  Mucha gente no tiene la ficha a mano y sube la pagina de la Consulta RUC
  (e-consultaruc.sunat.gob.pe, "Resultado de la Busqueda") impresa a PDF. Se
  rechazaba con "no parece una Ficha RUC" y habia que cargar todo a mano. Trae
  menos -- ni representante legal, ni telefono, ni correo --, pero si RUC,
  razon social, domicilio, estado, condicion y actividades. Se lee y se dice
  que falta.

NO SE GUARDA SOLO

  Esto rellena el formulario; el usuario lo revisa y pulsa Guardar.
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

from shared.config import DEPARTAMENTOS, normalizar

MAX_BYTES = 5 * 1024 * 1024
MAX_PAGINAS = 8


class FichaInvalida(ValueError):
    """El archivo no es una ficha RUC legible. El mensaje es para el usuario."""


_DEPTOS = {normalizar(d): d for d in DEPARTAMENTOS}

# Etiquetas que cortan una actividad economica que ocupa varias lineas.
_FIN_ACTIVIDAD = re.compile(
    r"^(Actividad Econ|Sistema Emisi|Sistema de Contab|C.digo de Profesi|"
    r"Departamento|Tipo y Nombre|www\.sunat)", re.IGNORECASE)


def _espacios(texto: str) -> str:
    return re.sub(r"\s+", " ", texto or "").strip()


def _campo(texto: str, etiqueta: str) -> str | None:
    """Valor de una linea "Etiqueta valor". '-' es el vacio de SUNAT."""
    m = re.search(rf"^{etiqueta}[ \t]+(.+)$", texto, re.MULTILINE)
    if not m:
        return None
    valor = _espacios(m.group(1))
    return None if valor in ("", "-") else valor


def _actividades(lineas: list[str]) -> list[str]:
    """Descripciones de las actividades principal y secundarias, sin codigo CIIU.

    La descripcion puede empezar en la linea de la etiqueta o en la siguiente,
    y seguir en otras: se junta hasta la proxima etiqueta conocida."""
    salida: list[str] = []
    for i, linea in enumerate(lineas):
        if not re.match(r"^Actividad Econ.mica (Principal|Secundaria \d)", linea):
            continue
        resto = re.sub(r"^Actividad Econ.mica (Principal|Secundaria \d)", "", linea).strip()
        partes = [resto] if resto else []
        for siguiente in lineas[i + 1:i + 4]:
            if _FIN_ACTIVIDAD.match(siguiente):
                break
            partes.append(siguiente.strip())
        texto = _espacios(" ".join(partes))
        m = re.match(r"^\d{4}\s*-\s*(.+)$", texto)
        if m:
            desc = m.group(1).strip().lower()
            if desc not in salida:
                salida.append(desc)
    return salida


def _representante(texto: str) -> dict:
    """Primer representante legal: DNI, nombre y cargo.

    El bloque del representante abre con "DOC. NACIONAL DE" + "IDENTIDAD/LE"
    + el numero. El de "Otras personas vinculadas" se parte distinto
    ("DOC. NACIONAL" + "DE IDENTIDAD/LE" + "- numero"), asi que no se confunde.
    El nombre ocupa una o dos lineas y se corta al llegar al ubigeo (un
    departamento), a un guion o a la cabecera de la tabla."""
    m = re.search(r"DOC\. NACIONAL DE\s*\n\s*IDENTIDAD/LE\s*\n\s*(\d{8})\s*\n", texto)
    if not m:
        return {}
    nombre: list[str] = []
    for linea in texto[m.end():].splitlines()[:4]:
        limpia = linea.strip()
        if (not limpia or limpia.startswith(("-", "Direcci"))
                or normalizar(limpia) in _DEPTOS
                or not re.fullmatch(r"[A-ZÑÁÉÍÓÚÜ' .]+", limpia)):
            break
        nombre.append(limpia)
    cargo = re.search(r"^([A-ZÑÁÉÍÓÚ][A-ZÑÁÉÍÓÚ .]+?)\s+\d{2}/\d{2}/\d{4}\s+\d{2}/\d{2}/\d{4}",
                      texto[m.end():], re.MULTILINE)
    return {
        "dni_representante": m.group(1),
        "representante_legal": _espacios(" ".join(nombre)) or None,
        "cargo_representante": _espacios(cargo.group(1)) if cargo else None,
    }


def _numero_telefono(valor: str) -> str | None:
    """Solo los digitos del numero, sin el codigo de ciudad de SUNAT.

    SUNAT separa el codigo con " - " ("82 - 959249224"). El numero puede traer
    a su vez guiones propios ("959-249-224"): antes se cortaba por el ULTIMO
    guion y quedaba "224", que no llega a seis cifras, y el telefono se perdia.
    Ahora solo se quita el prefijo separado con espacios.
    """
    m = re.match(r"^\s*\d{1,3}\s+-\s+(.+)$", valor)
    numero = re.sub(r"\D", "", m.group(1) if m else valor)
    return numero if len(numero) >= 6 else None


def _telefono(texto: str) -> str | None:
    for etiqueta in ("Tel.fono M.vil 1", "Tel.fono M.vil 2", "Tel.fono Fijo 1", "Tel.fono Fijo 2"):
        valor = _campo(texto, etiqueta)
        if valor:
            numero = _numero_telefono(valor)
            if numero:
                return numero
    return None


def _direccion(texto: str, depto: str | None) -> str | None:
    via = _campo(texto, "Tipo y Nombre V.a")
    nro = _campo(texto, "Nro")
    zona = _campo(texto, "Tipo y Nombre Zona")
    dpto = _campo(texto, "Dpto")
    interior = _campo(texto, "Interior")
    mz, lote = _campo(texto, "Mz"), _campo(texto, "Lote")
    provincia, distrito = _campo(texto, "Provincia"), _campo(texto, "Distrito")

    calle = " ".join(p for p in (via, nro) if p)
    if dpto:
        calle += f" Dpto. {dpto}"
    if interior:
        calle += f" Int. {interior}"
    if mz:
        calle += f" Mz. {mz}"
    if lote:
        calle += f" Lt. {lote}"
    partes = [calle.strip(), zona, distrito, provincia, depto.upper() if depto else None]
    salida = ", ".join(p for p in partes if p)
    return _espacios(salida) or None


# ─── La Consulta RUC ("Resultado de la Busqueda") ───────────
#
# Impresa desde el navegador, cada etiqueta puede salir con su valor en la
# misma linea o en la siguiente (es una tabla). Por eso el valor de una
# etiqueta es todo lo que hay hasta la etiqueta siguiente, y no "la linea".

# {clave: patron de la etiqueta}. El patron usa "." donde va una tilde: el
# texto extraido trae a veces "N?mero" o "NÃºmero" en vez de "Numero".
_ETIQUETAS_CONSULTA = {
    "ruc": r"N.{1,2}mero de RUC",
    "tipo": r"Tipo Contribuyente",
    "documento": r"Tipo de Documento",
    "nombre_comercial": r"Nombre Comercial",
    "inscripcion": r"Fecha de Inscripci.{1,2}n",
    "inicio": r"Fecha de Inicio de Actividades",
    "estado": r"Estado del Contribuyente",
    "condicion": r"Condici.{1,2}n del Contribuyente",
    "domicilio": r"Domicilio Fiscal",
    "emision": r"Sistema Emisi.{1,2}n de Comprobante",
    "comercio_exterior": r"Actividad Comercio Exterior",
    "contabilidad": r"Sistema Contabilidad",
    "actividades": r"Actividad\(es\) Econ.{1,2}mica\(s\)",
    # "Comprobantes de Pago c/aut. de impresion (F. 806 u 816):"
    "comprobantes": r"Comprobantes de Pago[^:\n]{0,60}",
    "emision_electronica": r"Sistema de Emisi.{1,2}n Electr.{1,2}nica",
    "emisor_desde": r"Emisor electr.{1,2}nico desde",
    "electronicos": r"Comprobantes Electr.{1,2}nicos",
    "ple": r"Afiliado al PLE desde",
    "padrones": r"Padrones",
    "consulta": r"Fecha (?:de )?consulta",
}
_RE_ETIQUETA_CONSULTA = re.compile(
    "|".join(f"(?P<{clave}>{patron})\\s*:" for clave, patron in _ETIQUETAS_CONSULTA.items()),
    re.IGNORECASE)


def es_consulta_ruc(texto: str) -> bool:
    return bool(re.search(r"Resultado de la B.squeda|N.mero de RUC\s*:", texto or "",
                          re.IGNORECASE))


def _campos_consulta(texto: str) -> dict[str, str]:
    """{clave de _ETIQUETAS_CONSULTA: valor} de la Consulta RUC."""
    marcas = list(_RE_ETIQUETA_CONSULTA.finditer(texto))
    campos: dict[str, str] = {}
    for i, m in enumerate(marcas):
        fin = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        # La primera aparicion manda: el pie de pagina repite a veces etiquetas.
        campos.setdefault(m.lastgroup, texto[m.end():fin])
    return campos


def _depto_de_domicilio(domicilio: str) -> str | None:
    """"... URB. LOS PINOS CUSCO - CUSCO - WANCHAQ" -> "Cusco".

    El domicilio de la consulta termina en "DEPARTAMENTO - PROVINCIA -
    DISTRITO", con el departamento pegado al final de la direccion. Se busca
    el nombre de departamento mas largo con el que acaba ese tramo, para que
    "MADRE DE DIOS" no se lea como otra cosa."""
    partes = [p.strip() for p in domicilio.split(" - ")]
    if len(partes) < 3:
        return None
    tramo = normalizar(partes[-3])
    candidatos = [d for d in _DEPTOS if tramo == d or tramo.endswith(" " + d)]
    return _DEPTOS[max(candidatos, key=len)] if candidatos else None


def _actividades_consulta(bloque: str) -> list[str]:
    """"Principal - 4741 - VENTA AL ..." y "Secundaria 1 - CIIU 4651 - ...".

    Cada actividad sigue hasta la proxima "Principal"/"Secundaria"; la
    descripcion puede ocupar varias lineas."""
    plano = _espacios(bloque)
    salida: list[str] = []
    patron = re.compile(
        r"(?:Principal|Secundaria\s*\d*)\s*-\s*(?:CIIU\s*)?\d{4,5}\s*-\s*(.+?)"
        r"(?=\s*(?:Principal|Secundaria\s*\d*)\s*-|$)", re.IGNORECASE)
    for m in patron.finditer(plano):
        desc = m.group(1).strip(" -").lower()
        if desc and desc not in salida:
            salida.append(desc)
    return salida


def parsear_consulta(texto: str) -> dict:
    """Campos del formulario a partir de la Consulta RUC de SUNAT.

    No trae representante legal, telefono ni correo: esos quedan vacios y la
    ruta avisa de que hay que completarlos (`origen` = "consulta")."""
    campos = _campos_consulta(texto)
    cabecera = _espacios(campos.get("ruc", ""))
    m = re.match(r"^(\d{11})\s*-\s*(.+)$", cabecera)
    if not m:
        raise FichaInvalida("No se encontró el RUC en la consulta de SUNAT.")

    def valor(clave: str) -> str | None:
        v = _espacios(campos.get(clave, ""))
        return None if v in ("", "-") else v

    domicilio = valor("domicilio")
    estado = valor("estado")
    if estado:
        # "BAJA DE OFICIO Fecha de Baja: 01/01/2020" -> solo el estado.
        estado = re.split(r"\s+Fecha\b", estado, maxsplit=1)[0]
    return {
        "ruc": m.group(1),
        "razon_social": m.group(2).strip() or None,
        "nombre_comercial": valor("nombre_comercial"),
        "departamento": _depto_de_domicilio(domicilio) if domicilio else None,
        "direccion": domicilio,
        "telefono": None,
        "email": None,
        "estado": estado,
        "condicion": valor("condicion"),
        "rubros": _actividades_consulta(campos.get("actividades", "")),
        "representante_legal": None,
        "dni_representante": None,
        "cargo_representante": None,
        "origen": "consulta",
    }


def parsear_texto(texto: str) -> dict:
    """Campos del formulario de empresa a partir del texto de la ficha.

    Acepta tambien la Consulta RUC (ver el docstring del modulo)."""
    if "Reporte de Ficha RUC" not in texto and es_consulta_ruc(texto):
        return parsear_consulta(texto)
    if "Ficha RUC" not in texto:
        raise FichaInvalida(
            "Ese PDF no parece una Ficha RUC ni una Consulta RUC de SUNAT.")

    m = re.search(r"Reporte de Ficha RUC\s*(.*?)\n\s*(\d{11})\s*\n", texto, re.DOTALL)
    if not m:
        raise FichaInvalida("No se encontró el RUC en la ficha.")
    razon = _espacios(m.group(1)) or None
    # SUNAT a veces anade " - <nombre abreviado>" ("K & A SISTEMAS ... CERRADA
    # - K & A SISTEMAS Y TELECOMUNICACIONES S"). Se corta solo si lo de detras
    # repite el comienzo: un guion propio del nombre se queda.
    if razon and " - " in razon:
        antes, despues = razon.rsplit(" - ", 1)
        if despues.split()[:3] == antes.split()[:3]:
            razon = antes

    depto_txt = _campo(texto, "Departamento")
    depto = _DEPTOS.get(normalizar(depto_txt or ""))

    correo = _campo(texto, "Correo Electr.nico 1") or _campo(texto, "Correo Electr.nico 2")
    if correo and "@" not in correo:
        correo = None

    datos = {
        "ruc": m.group(2),
        "razon_social": razon,
        "nombre_comercial": _campo(texto, "Nombre Comercial"),
        "departamento": depto,
        "direccion": _direccion(texto, depto_txt),
        "telefono": _telefono(texto),
        "email": correo.lower() if correo else None,
        "estado": _campo(texto, "Estado del Contribuyente"),
        "condicion": _campo(texto, "Condici.n del Domicilio Fiscal"),
        "rubros": _actividades(texto.splitlines()),
        "representante_legal": None,
        "dni_representante": None,
        "cargo_representante": None,
        "origen": "ficha",
    }
    datos.update(_representante(texto))
    return datos


def leer_ficha_ruc(contenido: bytes) -> dict:
    """Valida el archivo y devuelve los campos. FichaInvalida si no sirve."""
    if not contenido or not contenido.startswith(b"%PDF"):
        raise FichaInvalida("El archivo tiene que ser el PDF de la ficha RUC.")
    if len(contenido) > MAX_BYTES:
        raise FichaInvalida("El PDF pesa demasiado (máximo 5 MB).")
    from pypdf import PdfReader
    try:
        lector = PdfReader(io.BytesIO(contenido))
        paginas = lector.pages[:MAX_PAGINAS]
        texto = "\n".join((p.extract_text() or "") for p in paginas)
    except Exception as e:
        raise FichaInvalida("No se pudo leer el PDF. ¿Está dañado o protegido?") from e
    return parsear_texto(texto)


# ─── El PDF, para adjuntarlo ────────────────────────────────
#
# Las cotizaciones del GOREMAD se mandan con la ficha RUC, firmada y sellada
# en cada hoja. Cuando el usuario la sube para rellenar la empresa se guarda
# tambien el archivo, como la constancia del RNP (shared/constancia_rnp.py).

# Anclado a la raiz del proyecto, no al cwd. FICHA_RUC_DIR sigue mandando.
DIR_PDF = Path(os.getenv("FICHA_RUC_DIR")
               or Path(__file__).resolve().parents[1] / "data" / "ficha_ruc")


def ruta_pdf(empresa_id: int) -> Path:
    return DIR_PDF / f"empresa_{int(empresa_id)}.pdf"


def guardar_pdf(empresa_id: int, contenido: bytes) -> None:
    """Guarda con reemplazo atomico: o la ficha vieja o la nueva entera."""
    import tempfile

    destino = ruta_pdf(empresa_id)
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, temporal = tempfile.mkstemp(dir=destino.parent, prefix=".tmp_", suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(contenido)
        os.replace(temporal, destino)
    except BaseException:
        Path(temporal).unlink(missing_ok=True)
        raise
