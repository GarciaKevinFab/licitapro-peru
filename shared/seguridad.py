"""Primitivas de seguridad: contrasenas, tokens de vinculacion y cifrado.

Todo lo sensible pasa por aqui para que haya un solo sitio que auditar.

La clave maestra vive en LICITAPRO_SECRET_KEY, FUERA de la base de datos. Si
alguien se lleva un volcado de Postgres no se lleva las credenciales: sin la
clave, valor_cifrado es ruido.
"""
import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("shared.seguridad")

# Vigencia del token de vinculacion de Telegram. Corta a proposito: el token
# viaja en una URL y solo tiene que sobrevivir el tiempo de abrir la app.
MINUTOS_TOKEN_TELEGRAM = 10


def _clave_maestra() -> bytes:
    """Clave Fernet derivada de LICITAPRO_SECRET_KEY.

    En desarrollo cae a una clave fija y avisa por log. En produccion la
    variable es obligatoria: sin ella, cambiar de servidor volveria ilegibles
    todas las credenciales guardadas.
    """
    secreto = os.getenv("LICITAPRO_SECRET_KEY")
    if not secreto:
        if os.getenv("LICITAPRO_ENTORNO", "dev") != "dev":
            raise RuntimeError(
                "LICITAPRO_SECRET_KEY es obligatoria fuera de desarrollo: sin "
                "ella las credenciales cifradas quedan irrecuperables."
            )
        log.warning(
            "LICITAPRO_SECRET_KEY sin definir: usando clave de desarrollo. "
            "NO usar asi en produccion."
        )
        secreto = "clave-solo-para-desarrollo-no-usar-en-produccion"
    # Fernet exige 32 bytes en base64 urlsafe; el secreto puede ser cualquier texto.
    return base64.urlsafe_b64encode(hashlib.sha256(secreto.encode()).digest())


def clave_sesion() -> str:
    """Secreto para firmar la cookie de sesion."""
    return os.getenv("LICITAPRO_SECRET_KEY") or "clave-solo-para-desarrollo-no-usar-en-produccion"


# ─── Contrasenas ─────────────────────────────────────────

def hashear_password(password: str) -> str:
    """bcrypt con salt propio por contrasena."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verificar_password(password: str, hash_guardado: str | None) -> bool:
    """Comparacion en tiempo constante. Devuelve False si no hay hash.

    Se hace el trabajo de bcrypt igual cuando el usuario no existe, para no
    filtrar por tiempo de respuesta que correos estan registrados.
    """
    if not hash_guardado:
        bcrypt.checkpw(b"x", bcrypt.hashpw(b"x", bcrypt.gensalt()))
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hash_guardado.encode("utf-8"))
    except ValueError:
        return False


def password_debil(password: str) -> str | None:
    """Devuelve el motivo si la contrasena no sirve, o None si esta bien."""
    if len(password) < 10:
        return "Usa al menos 10 caracteres."
    if password.isdigit():
        return "Usa algo mas que numeros."
    if password.lower() in ("contrasena123", "password123", "licitapro123"):
        return "Esa contrasena es demasiado comun."
    return None


# ─── Token de vinculacion de Telegram ────────────────────

def nuevo_token_telegram() -> tuple[str, datetime]:
    """Token de un solo uso y su vencimiento.

    Se usa asi en vez de pedirle al usuario su ID numerico: si escribiera un ID
    ajeno, sus alertas de licitaciones se enviarian al chat de un desconocido y
    el sistema no tendria forma de detectarlo. Con enlace profundo el chat_id lo
    entrega Telegram, no el usuario.
    """
    return secrets.token_urlsafe(24), datetime.now() + timedelta(minutes=MINUTOS_TOKEN_TELEGRAM)


# ─── Credenciales cifradas ───────────────────────────────

def cifrar(valor: str) -> bytes:
    return Fernet(_clave_maestra()).encrypt(valor.encode("utf-8"))


def descifrar(valor_cifrado: bytes) -> str | None:
    """None si la clave no corresponde, en vez de reventar.

    Pasa al rotar LICITAPRO_SECRET_KEY sin re-cifrar: la credencial se marca
    como ilegible y el usuario la vuelve a cargar.
    """
    try:
        return Fernet(_clave_maestra()).decrypt(valor_cifrado).decode("utf-8")
    except (InvalidToken, TypeError):
        log.warning("Credencial ilegible con la clave actual (rotacion sin re-cifrar?)")
        return None
