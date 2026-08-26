"""Shared configuration and utility functions."""
import os
import re
import logging
import unicodedata
from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def env_int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


ADMIN_ID = env_int("TELEGRAM_ADMIN_ID")
ANTHROPIC_KEY = env("ANTHROPIC_API_KEY")

DEPARTAMENTOS = [
    "Amazonas", "Áncash", "Apurímac", "Arequipa", "Ayacucho", "Cajamarca",
    "Callao", "Cusco", "Huancavelica", "Huánuco", "Ica", "Junín",
    "La Libertad", "Lambayeque", "Lima", "Loreto", "Madre de Dios",
    "Moquegua", "Pasco", "Piura", "Puno", "San Martín", "Tacna",
    "Tumbes", "Ucayali",
]

TIPOS_PROCEDIMIENTO = {
    "LP": "Licitación Pública",
    "LPA": "Licitación Pública Abreviada",
    "CP": "Concurso Público",
    "CPA": "Concurso Público Abreviado",
    "AS": "Adjudicación Simplificada",
    "SIE": "Subasta Inversa Electrónica",
    "CD": "Contratación Directa",
    "CdP": "Comparación de Precios",
    "CM": "Contrato Menor (≤8 UIT)",
}


def normalizar(texto: str) -> str:
    """Minusculas y sin tildes, para comparar sin que la acentuacion estorbe.

    Las fuentes de OSCE devuelven el texto con la acentuacion corrupta
    ("CONTRATACI?N"), asi que una keyword con tilde como "tecnologia" nunca
    llegaba a matchear. Comparando ambos lados sin tildes el match si ocurre.
    """
    if not texto:
        return ""
    descompuesto = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return sin_tildes.lower()


@lru_cache(maxsize=512)
def _patron(clave: str) -> re.Pattern:
    """Patron con limites de palabra, cacheado por keyword ya normalizada."""
    # El sufijo opcional cubre el plural castellano ("prenda" -> "prendas",
    # "servidor" -> "servidores"). Sin el, exigir limites de palabra dejaba
    # pasar todos los plurales, que en estos textos son la forma habitual.
    return re.compile(r"(?<!\w)" + re.escape(clave) + r"(?:es|s)?(?!\w)")


def match_keywords(texto: str, keywords: list[str]) -> bool:
    """True si alguna keyword aparece en el texto, ignorando tildes.

    Exige limites de palabra. Sin esto el match por substring produce falsos
    positivos absurdos: la keyword "ERP" entraba dentro de "cuERPo", asi que
    "CUERPO GENERAL DE BOMBEROS" se colaba como si fuera un ERP.
    """
    if not keywords:
        return True
    texto_norm = normalizar(texto)
    for kw in keywords:
        clave = normalizar(kw).strip()
        if clave and _patron(clave).search(texto_norm):
            return True
    return False


# Un monto real trae marca de moneda ("S/ 5000") o estructura numerica
# ("1,234.56"). Un entero pelado en una celda casi nunca es plata: es el ano,
# un correlativo o un conteo de items.
_RE_FECHA_HORA = re.compile(r"\d{1,4}\s*[/-]\s*\d{1,2}\s*[/-]\s*\d{1,4}|\d{1,2}:\d{2}")
_RE_MONEDA = re.compile(r"s/\.?|us\$|u\$s|\$|pen|usd|soles", re.IGNORECASE)
_RE_NUMERO = re.compile(r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+[.,]\d{1,2}|\d+")


def _a_float(crudo: str) -> float | None:
    """Normaliza separadores de miles y decimales a float."""
    tiene_coma, tiene_punto = "," in crudo, "." in crudo
    if tiene_coma and tiene_punto:
        # El separador decimal es el que aparece mas a la derecha.
        decimal = "," if crudo.rfind(",") > crudo.rfind(".") else "."
        miles = "." if decimal == "," else ","
        crudo = crudo.replace(miles, "").replace(decimal, ".")
    elif tiene_coma or tiene_punto:
        sep = "," if tiene_coma else "."
        cola = crudo.rsplit(sep, 1)[1]
        # 3 digitos despues del separador = miles; 1 o 2 = decimales.
        crudo = crudo.replace(sep, "") if len(cola) == 3 else crudo.replace(sep, ".")
    try:
        return float(crudo)
    except ValueError:
        return None


def parse_monto(texto, minimo: float = 100.0, maximo: float = 1e10) -> float | None:
    """Extrae un monto de texto libre. None si no hay uno creible.

    Prefiere NULL antes que inventar una cifra: un monto falso envenena el
    scoring, el filtro de rango y el estimador de precios. La version anterior
    arrancaba todo caracter no numerico, asi que "COT-123-2026" se convertia en
    S/ 1,232,026 y la fecha "21/08/2026" en S/ 21,082,026.
    """
    if texto is None:
        return None
    if isinstance(texto, (int, float)):
        valor = float(texto)
        return valor if minimo <= valor <= maximo else None

    s = str(texto).strip()
    if not s or _RE_FECHA_HORA.search(s):
        return None

    con_moneda = bool(_RE_MONEDA.search(s))
    for m in _RE_NUMERO.finditer(s):
        crudo = m.group(0)
        # Sin marca de moneda exigimos estructura (miles o decimales): un
        # entero suelto es casi siempre un ano o un correlativo.
        if not con_moneda and not ("," in crudo or "." in crudo):
            continue
        valor = _a_float(crudo)
        if valor is not None and minimo <= valor <= maximo:
            return valor
    return None


def format_monto(monto: float, moneda: str = "PEN") -> str:
    if moneda == "PEN":
        return f"S/ {monto:,.2f}"
    return f"US$ {monto:,.2f}"


def format_fecha(fecha) -> str:
    if fecha is None:
        return "—"
    if hasattr(fecha, "strftime"):
        return fecha.strftime("%d/%m/%Y")
    return str(fecha)


def dias_restantes(fecha_cierre) -> int | None:
    if fecha_cierre is None:
        return None
    from datetime import datetime, date
    if hasattr(fecha_cierre, "date"):
        fecha_cierre = fecha_cierre.date()
    return (fecha_cierre - date.today()).days


def prioridad_emoji(score: float | None, dias: int | None) -> str:
    if score and score >= 80 and dias and dias <= 7:
        return "🔴"
    if score and score >= 60:
        return "🟡"
    if dias and dias <= 3:
        return "⚡"
    return "🟢"
