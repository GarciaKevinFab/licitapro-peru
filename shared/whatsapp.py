"""Adaptador de WhatsApp Cloud API (Meta) para los avisos de licitaciones.

POR QUE LA API OFICIAL Y NO UNA LIBRERIA DE WHATSAPP WEB

  Las librerias tipo whatsapp-web.js o Baileys automatizan la sesion de un
  telefono real. Funcionan hasta que Meta las detecta, y entonces bloquean el
  numero. Para un producto por el que se cobra una suscripcion, apostar el
  canal de avisos a algo que viola los terminos de servicio no es una opcion:
  el dia del bloqueo se cae el servicio de todos los clientes a la vez.

LO QUE HAY QUE ENTENDER ANTES DE TOCAR ESTO

  WhatsApp NO deja mandar texto libre a quien no te ha escrito. Solo se puede:

    a) Responder texto libre dentro de las 24 h siguientes a un mensaje SUYO
       (la "ventana de servicio").
    b) Iniciar conversacion con una PLANTILLA aprobada previamente por Meta.

  Una alerta de licitacion siempre es (b): la manda el sistema, no responde a
  nada. Por eso todo el envio de avisos va por plantilla. Implementarlo con
  texto libre funciona en las pruebas -- porque el desarrollador acaba de
  escribirle al numero y tiene la ventana abierta -- y falla con cada cliente
  real. Es el error clasico de esta API.

  Consecuencia de diseno: el detalle NO cabe en el mensaje. Una plantilla tiene
  parametros cortos y Meta RECHAZA los que llevan saltos de linea o tabulados.
  Asi que el aviso dice cuantas hay y lleva al panel; el detalle vive en la web.
  Sale mejor producto ademas: nadie quiere veinte notificaciones al dia.

CUANTO CUESTA, QUE AQUI SI IMPORTA

  Desde el 1 de julio de 2025 Meta cobra POR MENSAJE entregado, no por
  conversacion. La tarifa depende del pais y de la categoria de la plantilla.
  Nuestras alertas son categoria UTILITY (avisan de algo que el usuario pidio
  seguir), no MARKETING: es mas barata y se aprueba con menos friccion.

  Por eso los avisos se agrupan en un resumen por usuario y no se manda uno por
  licitacion. Un cliente con veinte coincidencias al dia son 600 mensajes al
  mes sin agrupar, y 30 agrupando. Multiplicado por la cartera, la diferencia
  se come el margen de un plan de S/49.

VERIFICADO CONTRA LA DOCUMENTACION DE META

  - Endpoint: POST https://graph.facebook.com/<version>/<phone_number_id>/messages
  - Autenticacion: cabecera Authorization: Bearer <token>
  - Firma del webhook: cabecera X-Hub-Signature-256, con el prefijo "sha256=",
    HMAC-SHA256 del cuerpo CRUDO usando el App Secret de la aplicacion.
  - Cobro por mensaje entregado desde 2025-07-01; categorias marketing,
    utility y authentication.

  POR_CONFIRMAR (depende de la cuenta, no de la documentacion): el nombre exacto
  de la plantilla una vez aprobada y el codigo de idioma con el que se apruebe
  ("es" o una variante regional). Ambos son configurables por entorno para no
  tener que tocar codigo cuando Meta apruebe la plantilla.
"""
import hashlib
import hmac
import logging
import os
import re

import httpx

log = logging.getLogger("shared.whatsapp")

VERSION_API = os.getenv("WHATSAPP_API_VERSION", "v21.0")
HOST = "https://graph.facebook.com"

# Codigo de pais de Peru. Los moviles peruanos son 9 digitos y empiezan por 9.
PREFIJO_PERU = "51"
LARGO_MOVIL_PERU = 9

TIEMPO_ESPERA = 20

# Palabras con las que alguien pide dejar de recibir avisos. Se comprueban en
# minusculas y sin signos. "STOP" esta incluido porque es lo que Meta y el
# habito internacional han hecho estandar, aunque el resto del producto sea
# en castellano.
PALABRAS_BAJA = frozenset({
    "baja", "stop", "cancelar", "salir", "basta", "desuscribir",
    "dar de baja", "quitar", "unsubscribe",
})


def modo() -> str:
    """'produccion' o 'simulado'.

    Simulado es el valor por defecto a proposito, igual que en la pasarela de
    pago: sin credenciales hay que fallar hacia "no manda nada", nunca hacia
    "intenta mandar y revienta en mitad del reparto de avisos".
    """
    m = (os.getenv("WHATSAPP_MODO") or "").strip().lower()
    if m == "produccion":
        if not (os.getenv("WHATSAPP_TOKEN") and os.getenv("WHATSAPP_PHONE_NUMBER_ID")):
            log.warning(
                "WHATSAPP_MODO=produccion pero faltan WHATSAPP_TOKEN o "
                "WHATSAPP_PHONE_NUMBER_ID: se sigue en modo simulado.")
            return "simulado"
        return "produccion"
    return "simulado"


def configurado() -> bool:
    return modo() == "produccion"


# ─── Numeros ─────────────────────────────────────────────

def normalizar_numero(crudo: str) -> str | None:
    """Devuelve el numero en E.164 (+51987654321) o None si no es valido.

    Se acepta lo que la gente escribe de verdad: con espacios, guiones,
    parentesis, con o sin +51, con el 0 de larga distancia delante. Rechazar
    esas variantes obligaria al usuario a adivinar el formato, y el formato es
    problema nuestro, no suyo.
    """
    if not crudo:
        return None
    digitos = re.sub(r"\D", "", crudo)
    if not digitos:
        return None

    # 00 51 9XX... o 0 9XX...: el prefijo de salida internacional o nacional.
    if digitos.startswith("00"):
        digitos = digitos[2:]
    if digitos.startswith(PREFIJO_PERU) and len(digitos) == LARGO_MOVIL_PERU + 2:
        movil = digitos[2:]
    elif len(digitos) == LARGO_MOVIL_PERU:
        movil = digitos
    elif len(digitos) == LARGO_MOVIL_PERU + 1 and digitos.startswith("0"):
        movil = digitos[1:]
    elif digitos.startswith(PREFIJO_PERU):
        # Empieza por 51 pero no tiene el largo de un movil peruano. El 51 es
        # codigo de Peru en exclusiva, asi que esto es un numero mal escrito
        # (tipico: el prefijo tecleado dos veces), no el de otro pais. Colarlo
        # como extranjero producia un destino inexistente que solo se descubria
        # al fallar el envio.
        return None
    else:
        # Numero de otro pais: se acepta tal cual si tiene largo plausible.
        # No se asume Peru para no mandarle el aviso a un desconocido.
        return f"+{digitos}" if 8 <= len(digitos) <= 15 else None

    if not movil.startswith("9"):
        # Fijo peruano. WhatsApp es un servicio de moviles: avisar aqui evita
        # que el cliente crea que configuro algo que nunca le va a llegar.
        return None
    return f"+{PREFIJO_PERU}{movil}"


def _para_api(numero_e164: str) -> str:
    """La API quiere los digitos sin el '+'."""
    return numero_e164.lstrip("+")


# ─── Envio ───────────────────────────────────────────────

def _limpiar_parametro(texto: str, maximo: int = 120) -> str:
    """Deja un texto apto para ser parametro de plantilla.

    Meta RECHAZA el mensaje entero si un parametro contiene saltos de linea,
    tabulados o cuatro espacios seguidos. Un resumen de varias lineas metido en
    un parametro no falla al escribirlo: falla al enviarlo, en produccion.
    """
    plano = re.sub(r"[\r\n\t]+", " ", texto or "")
    plano = re.sub(r"\s{2,}", " ", plano).strip()
    if len(plano) > maximo:
        plano = plano[: maximo - 1].rstrip() + "…"
    return plano or "-"


async def enviar_plantilla(numero_e164: str, plantilla: str,
                           parametros: list[str],
                           idioma: str | None = None) -> tuple[bool, str]:
    """Manda una plantilla aprobada. Devuelve (enviado, detalle).

    Es la unica forma valida de iniciar conversacion. `detalle` trae el motivo
    cuando falla, para poder guardarlo y explicarselo al usuario en vez de
    dejar el aviso perdido en un log.
    """
    if modo() == "simulado":
        log.info("[simulado] WhatsApp a %s | plantilla=%s | params=%s",
                 numero_e164, plantilla, parametros)
        return True, "simulado"

    token = os.getenv("WHATSAPP_TOKEN")
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    idioma = idioma or os.getenv("WHATSAPP_PLANTILLA_IDIOMA", "es")

    cuerpo = {
        "messaging_product": "whatsapp",
        "to": _para_api(numero_e164),
        "type": "template",
        "template": {
            "name": plantilla,
            "language": {"code": idioma},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": _limpiar_parametro(p)}
                    for p in parametros
                ],
            }],
        },
    }

    url = f"{HOST}/{VERSION_API}/{phone_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=TIEMPO_ESPERA) as cliente:
            r = await cliente.post(
                url, json=cuerpo,
                headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        log.error("WhatsApp: fallo de red al enviar a %s: %s", numero_e164, e)
        return False, f"red: {e}"

    if r.status_code == 200:
        return True, "enviado"

    # El error de Meta viene con un mensaje util; conviene conservarlo entero
    # porque distingue "plantilla no aprobada" de "numero invalido" de "sin
    # saldo", y cada uno se arregla de forma distinta.
    try:
        detalle = str((r.json().get("error") or {}).get("message") or "")[:300]
    except Exception:
        detalle = r.text[:300]
    log.error("WhatsApp rechazo el envio a %s (%s): %s",
              numero_e164, r.status_code, detalle)
    return False, detalle or f"HTTP {r.status_code}"


async def enviar_texto(numero_e164: str, texto: str) -> tuple[bool, str]:
    """Texto libre. SOLO vale dentro de la ventana de 24 h.

    Sirve para contestar a quien acaba de escribirnos (confirmar el alta, o la
    baja). Para avisos que iniciamos nosotros hay que usar enviar_plantilla:
    esto fallaria con cada cliente real aunque funcione al probarlo.
    """
    if modo() == "simulado":
        log.info("[simulado] WhatsApp texto a %s: %s", numero_e164, texto[:80])
        return True, "simulado"

    url = f"{HOST}/{VERSION_API}/{os.getenv('WHATSAPP_PHONE_NUMBER_ID')}/messages"
    try:
        async with httpx.AsyncClient(timeout=TIEMPO_ESPERA) as cliente:
            r = await cliente.post(url, json={
                "messaging_product": "whatsapp",
                "to": _para_api(numero_e164),
                "type": "text",
                "text": {"body": texto[:4000], "preview_url": False},
            }, headers={"Authorization": f"Bearer {os.getenv('WHATSAPP_TOKEN')}"})
    except Exception as e:
        return False, f"red: {e}"
    return (r.status_code == 200), (
        "enviado" if r.status_code == 200 else r.text[:300])


# ─── Webhook ─────────────────────────────────────────────

def verificar_firma(cuerpo_crudo: bytes, firma_recibida: str) -> bool:
    """Valida X-Hub-Signature-256 contra el App Secret.

    Sin secreto configurado devuelve False, NO True. Un webhook de WhatsApp sin
    verificar deja que cualquiera nos diga "este usuario se dio de baja" o
    "este numero quedo verificado". Aqui hay que fallar cerrado.
    """
    secreto = os.getenv("WHATSAPP_APP_SECRET")
    if not secreto or not firma_recibida:
        log.warning("Webhook de WhatsApp sin firma verificable: se rechaza.")
        return False
    # Meta lo manda como "sha256=<hex>".
    recibida = firma_recibida.strip()
    if recibida.startswith("sha256="):
        recibida = recibida[7:]
    esperada = hmac.new(secreto.encode(), cuerpo_crudo, hashlib.sha256).hexdigest()
    # compare_digest evita filtrar por tiempo cuanto coincide la firma.
    return hmac.compare_digest(esperada, recibida.lower())


def verificar_suscripcion(modo_hub: str, token_hub: str, reto: str) -> str | None:
    """Responde al GET con el que Meta da de alta el webhook.

    Devuelve el reto a repetir, o None si el token no coincide. Sin
    WHATSAPP_VERIFY_TOKEN configurado siempre falla: dar de alta un webhook sin
    comprobar quien lo pide es abrir la puerta.
    """
    esperado = os.getenv("WHATSAPP_VERIFY_TOKEN")
    if not esperado:
        log.warning("Falta WHATSAPP_VERIFY_TOKEN: no se puede dar de alta el webhook.")
        return None
    if modo_hub == "subscribe" and hmac.compare_digest(token_hub or "", esperado):
        return reto
    log.warning("Alta de webhook de WhatsApp con token incorrecto.")
    return None


def es_baja(texto: str) -> bool:
    """True si el mensaje entrante pide dejar de recibir avisos.

    Se compara la frase entera y no "contiene": alguien que escribe "no me
    llego la licitacion" no se esta dando de baja, y darle de baja por eso lo
    deja sin servicio sin que se entere.
    """
    limpio = re.sub(r"[^\w\s]", "", (texto or "").strip().lower())
    limpio = re.sub(r"\s+", " ", limpio)
    return limpio in PALABRAS_BAJA


def mensajes_entrantes(cuerpo: dict) -> list[dict]:
    """Extrae [{numero, texto}] de la estructura anidada del webhook.

    Meta envuelve todo en entry[].changes[].value.messages[]. Se recorre con
    tolerancia porque el mismo webhook trae tambien avisos de estado (enviado,
    entregado, leido) que no llevan 'messages' y no son un error.
    """
    salida = []
    for entrada in (cuerpo.get("entry") or []):
        for cambio in (entrada.get("changes") or []):
            valor = cambio.get("value") or {}
            for m in (valor.get("messages") or []):
                numero = m.get("from")
                texto = ((m.get("text") or {}).get("body") or "").strip()
                if numero:
                    salida.append({"numero": f"+{numero}", "texto": texto})
    return salida
