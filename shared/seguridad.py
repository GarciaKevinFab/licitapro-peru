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
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from shared import fechas

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


# Entornos en los que se tolera arrancar sin LICITAPRO_SECRET_KEY. Cualquier
# otro valor -- 'produccion', o una errata como 'prod' -- exige la clave.
ENTORNOS_SIN_CLAVE = ("dev", "test")


def _entorno() -> str:
    """LICITAPRO_ENTORNO normalizado. Sin definir (o vacia) cuenta como 'dev',
    igual que en _clave_maestra y en web/app.py."""
    return (os.getenv("LICITAPRO_ENTORNO") or "dev").strip().lower()


def clave_sesion() -> str:
    """Secreto para firmar la cookie de sesion.

    POR QUE YA NO CAE A LA CLAVE DE DESARROLLO EN PRODUCCION

      Antes, sin LICITAPRO_SECRET_KEY, devolvia la cadena literal que esta en
      este mismo archivo, que es publico. Con ella cualquiera puede firmar una
      cookie con el usuario_id que quiera: entrar como el admin sin contrasena.
      Y no avisaba de nada, porque la app arrancaba y funcionaba igual. Ahora
      fuera de dev/test la app se niega a arrancar, que es un fallo ruidoso y
      se arregla en un minuto.

    POR QUE SE DERIVA Y NO SE USA LA VARIABLE TAL CUAL

      La misma variable alimenta la clave Fernet de las credenciales. Usarla
      en crudo para firmar cookies mezcla dos usos de un mismo secreto; con
      HKDF y un `info` propio salen dos claves independientes. La de Fernet NO
      se toca (cambiarla volveria ilegibles las credenciales ya cifradas); la
      de sesion si, y el unico efecto es que las sesiones abiertas se cierran
      una vez al desplegar esto.
    """
    secreto = os.getenv("LICITAPRO_SECRET_KEY")
    if not secreto:
        if _entorno() not in ENTORNOS_SIN_CLAVE:
            raise RuntimeError(
                "LICITAPRO_SECRET_KEY es obligatoria fuera de desarrollo: sin "
                "ella la cookie de sesion se firmaria con una clave publica.")
        log.warning(
            "LICITAPRO_SECRET_KEY sin definir: firmando sesiones con la clave "
            "de desarrollo. NO usar asi en produccion.")
        secreto = "clave-solo-para-desarrollo-no-usar-en-produccion"
    derivada = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                    info=b"licitapro/cookie-de-sesion").derive(secreto.encode("utf-8"))
    return derivada.hex()


# ─── Contrasenas ─────────────────────────────────────────

# bcrypt solo mira los primeros 72 bytes. Hasta la 4.x los demas se tiraban en
# silencio; la 5.0 (la que fija requirements.txt) lanza ValueError, y eso era
# un 500 al registrarse con una frase larga.
#
#   POR QUE SE RECHAZA Y NO SE PREHASHEA (sha256 antes de bcrypt): el prehash
#   cambia lo que entra a bcrypt para TODAS las contrasenas, asi que ningun
#   hash ya guardado volveria a casar y todos los usuarios actuales se
#   quedarian fuera. Rechazar lo que pasa de 72 bytes no rompe a nadie: con
#   bcrypt 5 nadie pudo guardar una contrasena asi.
#
#   Son BYTES, no caracteres: una letra con tilde ocupa dos en UTF-8.
MAX_BYTES_PASSWORD = 72
MOTIVO_PASSWORD_LARGA = ("Usa una contrasena mas corta: como maximo 72 "
                         "caracteres (las letras con tilde cuentan doble).")


def _demasiado_larga(password: str) -> bool:
    return len(password.encode("utf-8")) > MAX_BYTES_PASSWORD


def hashear_password(password: str) -> str:
    """bcrypt con salt propio por contrasena.

    Lanza ValueError con un mensaje mostrable si pasa de 72 bytes. Los
    llamadores ya filtran antes con password_debil; esto es el respaldo para
    que un camino nuevo que se lo salte no acabe en un 500 sin explicacion.
    """
    if _demasiado_larga(password):
        raise ValueError(MOTIVO_PASSWORD_LARGA)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verificar_password(password: str, hash_guardado: str | None) -> bool:
    """Comparacion en tiempo constante. Devuelve False si no hay hash.

    Se hace el trabajo de bcrypt igual cuando el usuario no existe, para no
    filtrar por tiempo de respuesta que correos estan registrados. Lo mismo
    con una contrasena de mas de 72 bytes: no puede casar con ningun hash
    guardado (ver MAX_BYTES_PASSWORD) y se responde False igual en bcrypt 4 y 5.
    """
    if not hash_guardado or _demasiado_larga(password):
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
    if _demasiado_larga(password):
        return MOTIVO_PASSWORD_LARGA
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
    return secrets.token_urlsafe(24), fechas.ahora() + timedelta(minutes=MINUTOS_TOKEN_TELEGRAM)


# OJO CON EL VENCIMIENTO DE LOS DOS TOKENS
#
#   `expira` es hora de pared de Lima (fechas.ahora()), igual que fecha_cierre.
#   Quien lo compare en SQL tiene que hacerlo contra
#   `NOW() AT TIME ZONE 'America/Lima'`, NUNCA contra NOW() a secas: la sesion
#   del VPS corre en UTC (POSTGRES_ZONA_SESION) y NOW() leido en una columna
#   sin zona da la hora UTC, cinco horas por delante. Asi estuvo: todo enlace
#   de recuperacion y todo codigo de Telegram nacian ya vencidos en produccion,
#   y los tests no lo veian porque corrian con la sesion en Lima.


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


# ─── Recuperacion de contrasena ──────────────────────────

# Una hora. Corto porque el token viaja por correo, que es un canal que no
# controlamos: puede quedar en un buzon compartido, reenviado o en un archivo.
HORAS_TOKEN_RECUPERACION = 1

# Cuantos enlaces se pueden pedir por hora para una misma cuenta. Sin limite,
# el formulario sirve para bombardear la bandeja de cualquiera.
MAX_PETICIONES_POR_HORA = 3


def nuevo_token_recuperacion() -> tuple[str, str, datetime]:
    """(token_en_claro, hash_para_guardar, vencimiento).

    El token en claro solo existe en memoria el tiempo de armar el correo. Lo
    que se guarda es el hash: si alguien se lleva un volcado de la base, los
    enlaces vigentes no le sirven de nada, igual que con las contrasenas.

    SHA-256 basta aqui y bcrypt seria un error: son 32 bytes aleatorios, no una
    contrasena humana adivinable, y el hash se consulta en cada apertura del
    enlace.
    """
    token = secrets.token_urlsafe(32)
    return token, hash_token(token), fechas.ahora() + timedelta(hours=HORAS_TOKEN_RECUPERACION)


def hash_token(token: str) -> str:
    """SHA-256 en hexadecimal del token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
