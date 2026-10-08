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

NO SE GUARDA SOLO

  Esto rellena el formulario; el usuario lo revisa y pulsa Guardar.
"""
from __future__ import annotations

import io
import re

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


def _telefono(texto: str) -> str | None:
    for etiqueta in ("Tel.fono M.vil 1", "Tel.fono M.vil 2", "Tel.fono Fijo 1", "Tel.fono Fijo 2"):
        valor = _campo(texto, etiqueta)
        if valor:
            # "82 - 959249224": el prefijo es el codigo de ciudad de SUNAT.
            numero = re.sub(r"\D", "", valor.split("-")[-1])
            if len(numero) >= 6:
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


def parsear_texto(texto: str) -> dict:
    """Campos del formulario de empresa a partir del texto de la ficha."""
    if "Ficha RUC" not in texto:
        raise FichaInvalida("Ese PDF no parece una Ficha RUC de SUNAT.")

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
