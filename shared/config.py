"""Shared configuration and utility functions."""
import os
import logging
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
    "AS": "Adjudicación Simplificada",
    "SIE": "Subasta Inversa Electrónica",
    "CD": "Contratación Directa",
    "CdP": "Comparación de Precios",
    "CM": "Contrato Menor (≤8 UIT)",
}


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
