"""Adaptador de Culqi. Todo lo especifico del proveedor vive aqui.

POR QUE CULQI Y NO IZIPAY

  Izipay confirmo por escrito que la afiliacion del comercio esta habilitada
  SOLO para pagos unicos, no recurrentes, y que cada dominio necesita su propia
  afiliacion. Con eso, "suscripcion" era una palabra en la portada: cada
  periodo habia que perseguir al cliente por correo para que volviera a pagar a
  mano.

  Culqi tiene suscripciones nativas en soles: se define un PLAN, se asocia la
  TARJETA del cliente y Culqi cobra sola cada periodo y avisa por webhook. Que
  al cliente le resulte facil -- que el cobro sea automatico -- es justo lo que
  se pedia.

QUE ESTA VERIFICADO CONTRA EL SDK OFICIAL (culqi/culqi-python) Y SUS DOCS

  - Base: https://api.culqi.com/v2
  - Autenticacion: cabecera `Authorization: Bearer <llave>`.
        llave PUBLICA  (pk_test_… / pk_live_…)  -> solo tokens y tokens/confirm
        llave SECRETA  (sk_test_… / sk_live_…)  -> todo lo demas
  - Rutas (constantes reales de culqi/utils/urls.py): tokens, customers, cards,
    charges, refunds, events, orders, recurrent/plans, recurrent/subscriptions.
  - Plan:        POST /v2/recurrent/plans          -> pln_…
  - Cliente:     POST /v2/customers                -> cus_…
  - Tarjeta:     POST /v2/cards                    -> crd_…
  - Suscripcion: POST /v2/recurrent/subscriptions  -> sxn_…
                 DELETE /v2/recurrent/subscriptions/{id}  es IRREVERSIBLE
  - Errores: el cuerpo trae `object: "error"` con `user_message` (para ensenar
    al cliente) y `merchant_message` (para el log del comercio).

QUE **NO** ESTA VERIFICADO -- marcado POR_CONFIRMAR y NO inventado

  1. Que valor de `interval_unit_time` corresponde a mensual y cual a anual.
     La documentacion publica solo ensena `1` en los ejemplos y dice que el
     intervalo "puede ser dia, mes y ano". Ver `intervalo_de()`: no hay valor
     por defecto adivinado, se exige por entorno a proposito.

  2. La forma exacta del cuerpo del webhook y si trae firma. Por eso el
     receptor (web/webhooks_culqi.py) NO se cree el cuerpo: saca el id del
     cargo y lo COMPRUEBA contra esta API con la llave secreta. Eso funciona
     haya firma o no.

  3. La URL vigente del script de Culqi Checkout en el navegador. Va por
     entorno (CULQI_CHECKOUT_JS) y la CSP admite los dos hosts conocidos.

LA TARJETA NUNCA TOCA NUESTRO SERVIDOR

  El token se genera EN EL NAVEGADOR con Culqi Checkout / culqi.js y aqui solo
  llega un `tkn_…`. `POST /v2/tokens` desde el servidor existe, pero es para
  pruebas: usarlo en produccion nos metaria dentro del alcance de PCI-DSS, que
  es exactamente lo que esta integracion evita.
"""
import logging
import os
from decimal import ROUND_HALF_UP, Decimal

import httpx

log = logging.getLogger("shared.culqi")

BASE = "https://api.culqi.com/v2"

# Constantes reales del SDK oficial (culqi/utils/urls.py). Se escriben aqui y
# no sueltas por el codigo para que un cambio de ruta sea una linea.
RUTA_TOKENS = "tokens"
RUTA_CLIENTES = "customers"
RUTA_TARJETAS = "cards"
RUTA_CARGOS = "charges"
RUTA_EVENTOS = "events"
RUTA_PLANES = "recurrent/plans"
RUTA_SUSCRIPCIONES = "recurrent/subscriptions"

MONEDA = "PEN"

# Culqi cobra 3.99% + S/0.90 por transaccion, mas IGV. Sirve para ensenar el
# neto en el panel, no para cobrar.
COMISION_PORCENTAJE = 0.0399 * 1.18
COMISION_FIJA = 0.90 * 1.18

# Culqi separa los dos entornos EN LA PROPIA LLAVE: pk_test_/sk_test_ frente a
# pk_live_/sk_live_. No hace falta adivinar nada mirando el host.
_PREFIJOS_PRUEBA = ("pk_test_", "sk_test_")
_PREFIJOS_PRODUCCION = ("pk_live_", "sk_live_")

_CENTIMO = Decimal("0.01")


class ErrorCulqi(Exception):
    """Un rechazo de Culqi con el texto que SI se le puede ensenar al cliente.

    Culqi devuelve dos mensajes distintos y no son intercambiables:

        user_message      "Tu tarjeta no tiene saldo suficiente."
        merchant_message  "The card was declined for insufficient funds."

    El primero esta escrito para el titular de la tarjeta; el segundo, para
    quien opera el comercio. Ensenar el segundo en la pantalla de pago es la
    forma habitual de que un cliente vea ingles tecnico y abandone la compra, y
    ademas filtra detalles del rechazo que no le corresponden.
    """

    def __init__(self, user_message: str, merchant_message: str = "",
                 tipo: str = "", codigo: str = "", http: int = 0,
                 crudo: dict | None = None):
        super().__init__(merchant_message or user_message)
        self.user_message = user_message
        self.merchant_message = merchant_message
        self.tipo = tipo
        self.codigo = codigo
        self.http = http
        self.crudo = crudo or {}


class ConfiguracionCulqi(Exception):
    """Falta algo por configurar. No es culpa del cliente y no se le ensena."""


# ─── Modo de funcionamiento ──────────────────────────────

def _llave_publica() -> str:
    return (os.getenv("CULQI_LLAVE_PUBLICA") or "").strip()


def _llave_secreta() -> str:
    return (os.getenv("CULQI_LLAVE_SECRETA") or "").strip()


def llaves_de_prueba() -> bool:
    """Si las llaves configuradas son las del entorno de pruebas de Culqi."""
    valores = (_llave_publica() + " " + _llave_secreta()).lower()
    return any(p in valores for p in _PREFIJOS_PRUEBA)


def llaves_de_produccion() -> bool:
    valores = (_llave_publica() + " " + _llave_secreta()).lower()
    return any(p in valores for p in _PREFIJOS_PRODUCCION)


def modo() -> str:
    """'produccion', 'prueba' o 'simulado'.

    SIMULADO ES EL VALOR POR DEFECTO A PROPOSITO

      Sin llaves hay que fallar hacia "no cobra nada", nunca hacia "intenta
      cobrar de verdad". Y mientras el modo sea simulado, los textos del sitio
      siguen diciendo que el pago es unico -- ver `cobro_recurrente()` --,
      porque es lo que de verdad ocurriria.

    POR QUE SE NIEGA A CORRER EN PRODUCCION CON LLAVES DE PRUEBA

      Es el mismo fallo caro que ya documenta `shared/izipay.py`, y aqui es
      todavia mas silencioso: con `sk_test_` en modo produccion, Culqi acepta
      las peticiones -- son validas, solo que contra el entorno de pruebas --
      y crea suscripciones que NO cobran dinero de verdad. O sea: el dueno cree
      que factura, el cliente cree que pago, y no se ha movido un sol.

      Por eso se levanta el error aqui y no se cae de vuelta a 'prueba'. Caer
      de vuelta dejaria la pasarela en pruebas mientras el dueno cree que
      cobra, que es la misma confusion con otra cara.
    """
    m = (os.getenv("CULQI_MODO") or "").strip().lower()
    if m not in ("produccion", "prueba"):
        return "simulado"
    if not (_llave_publica() and _llave_secreta()):
        log.warning("CULQI_MODO=%s pero faltan CULQI_LLAVE_PUBLICA o "
                    "CULQI_LLAVE_SECRETA: se sigue en modo simulado.", m)
        return "simulado"
    if m == "produccion" and llaves_de_prueba():
        raise RuntimeError(
            "CULQI_MODO=produccion pero CULQI_LLAVE_PUBLICA/CULQI_LLAVE_SECRETA "
            "son las de PRUEBAS de Culqi (empiezan por 'pk_test_' o "
            "'sk_test_'). Culqi las aceptaria sin protestar y crearia "
            "suscripciones que no cobran dinero real: el sitio pareceria estar "
            "facturando mientras no entra un sol. Cambia las DOS llaves por "
            "las pk_live_/sk_live_ del panel antes de mover CULQI_MODO."
        )
    return m


def activo() -> bool:
    """Si la ruta de Culqi puede cobrar de verdad."""
    return modo() != "simulado"


def cobro_recurrente() -> bool:
    """Si HOY el cobro se renueva solo.

    Los textos de /comprar, /precios y /terminos cuelgan de esto y no de una
    frase escrita a mano. La semana pasada hubo que corregirlos porque
    prometian renovacion automatica con una pasarela que solo hacia pagos
    unicos; atarlo a un hecho comprobable evita repetir exactamente eso.
    """
    try:
        return activo()
    except RuntimeError:
        # modo() se niega a arrancar mal configurado. Un sitio a medio
        # configurar no puede prometer renovacion automatica.
        return False


def comision_estimada(monto: float) -> float:
    """Lo que se lleva Culqi de un cobro, con IGV incluido."""
    return round(monto * COMISION_PORCENTAJE + COMISION_FIJA, 2)


# ─── Importes ────────────────────────────────────────────

def a_centimos(monto) -> int:
    """S/ 99.00 -> 9900. Culqi trabaja en la unidad minima de la moneda.

    En Decimal y no en float: int(99.00 * 100) da 9899 en cuanto el precio
    llega como 98.99999999 desde un NUMERIC de Postgres, y ese error no
    revienta -- solo cobra un centimo de menos y descuadra la contabilidad.

    POR_CONFIRMAR (2 minutos con la llave de prueba): que `amount` sea de
    verdad en centimos. Se crea un plan de 9900 y se mira el importe que Culqi
    devuelve y muestra en el panel: si dijera S/ 9,900.00 en vez de S/ 99.00,
    esta funcion sobra. Toda la documentacion y el SDK apuntan a centimos.
    """
    return int((Decimal(str(monto)).quantize(_CENTIMO, rounding=ROUND_HALF_UP)
                * 100).to_integral_value())


def a_soles(centimos: int) -> Decimal:
    return (Decimal(int(centimos)) / 100).quantize(_CENTIMO)


# ─── Intervalo del plan: POR_CONFIRMAR, y no se adivina ──

_INTERVALO_ENV = {"mensual": "CULQI_INTERVALO_MENSUAL",
                  "anual": "CULQI_INTERVALO_ANUAL"}


def intervalo_de(periodo: str) -> int:
    """`interval_unit_time` para 'mensual' o 'anual'. **POR_CONFIRMAR.**

    QUE NO SE SABE

      La documentacion publica de Culqi dice que el intervalo "puede ser dia,
      mes y ano" y en TODOS sus ejemplos escribe `interval_unit_time: 1`, sin
      decir a cual de los tres corresponde el 1. Con eso no se puede deducir si
      mensual es 1, 2 o 3.

    POR QUE NO HAY VALOR POR DEFECTO

      Adivinar aqui no da error: da un plan que cobra cada DIA lo que debia
      cobrar cada MES, o cada ano lo que debia cobrar cada mes. Lo primero son
      treinta cargos al cliente antes de que nadie lo note; lo segundo es no
      facturar durante once meses. Ninguno de los dos avisa.

      Asi que se exige por entorno y, sin ello, `crear_plan` se niega a crear
      nada. Es incomodo a proposito: la incomodidad dura los cinco minutos que
      cuesta comprobarlo, y la alternativa cuesta un mes de cobros mal hechos.

    COMO SE COMPRUEBA EN CINCO MINUTOS (con la llave sk_test_)

        for v in 1 2 3; do
          curl -s https://api.culqi.com/v2/recurrent/plans \\
            -H "Authorization: Bearer $CULQI_LLAVE_SECRETA" \\
            -H "Content-Type: application/json" \\
            -d "{\\"name\\":\\"sonda $v\\",\\"short_name\\":\\"sonda-$v\\",
                 \\"description\\":\\"sonda de intervalo\\",\\"amount\\":100,
                 \\"currency\\":\\"PEN\\",\\"interval_unit_time\\":$v,
                 \\"interval_count\\":1,\\"initial_cycles\\":{\\"count\\":0,
                 \\"has_initial_charge\\":false,\\"amount\\":0,
                 \\"interval_unit_time\\":$v}}"
        done

      La respuesta de cada plan trae el intervalo resuelto (y el panel de Culqi
      lo ensena en letra: "Diario", "Mensual", "Anual"). Se apunta cual es cual
      en CULQI_INTERVALO_MENSUAL y CULQI_INTERVALO_ANUAL, se borran los planes
      sonda del panel y ya esta.
    """
    if periodo not in _INTERVALO_ENV:
        raise ConfiguracionCulqi(f"Periodo desconocido: {periodo!r}")
    valor = (os.getenv(_INTERVALO_ENV[periodo]) or "").strip()
    if not valor.isdigit():
        raise ConfiguracionCulqi(
            f"Falta {_INTERVALO_ENV[periodo]}. Culqi no documenta que valor de "
            f"interval_unit_time significa '{periodo}', y adivinarlo crearia un "
            f"plan que cobra con otra frecuencia sin dar ningun error. "
            f"Compruebalo con la llave de prueba (ver shared/culqi.intervalo_de) "
            f"y ponlo en el entorno.")
    return int(valor)


# ─── Cliente HTTP ────────────────────────────────────────

# Tiempos: conectar rapido, leer con paciencia. Crear una suscripcion dispara
# el primer cobro contra el banco emisor y eso no siempre es instantaneo;
# cortar a los 10 s dejaria cobros hechos que nosotros damos por fallidos.
_TIMEOUT = httpx.Timeout(connect=8.0, read=35.0, write=15.0, pool=8.0)

# Reintentos: SOLO donde repetir es inofensivo. Ver `_peticion`.
_REINTENTOS = 2


def _cabeceras(llave: str) -> dict:
    return {"Authorization": f"Bearer {llave}",
            "Content-Type": "application/json",
            "Accept": "application/json"}


def _error_de(datos: dict, http: int) -> ErrorCulqi:
    """Traduce el cuerpo de error de Culqi a nuestra excepcion.

    Culqi devuelve {"object": "error", "type": ..., "code": ...,
    "merchant_message": ..., "user_message": ...}. Si faltara `user_message`
    -- un 500 suyo, un proxy por medio -- se pone un texto nuestro: lo que NO
    puede pasar es que el cliente vea una traza o un JSON crudo en la pantalla
    de pago.
    """
    user = (datos.get("user_message") or "").strip()
    merchant = (datos.get("merchant_message") or "").strip()
    return ErrorCulqi(
        user_message=user or "No se pudo procesar el pago. Intenta de nuevo o "
                             "prueba con otra tarjeta.",
        merchant_message=merchant or f"HTTP {http}: {datos}",
        tipo=str(datos.get("type") or ""),
        codigo=str(datos.get("code") or ""),
        http=http, crudo=datos)


async def _peticion(metodo: str, ruta: str, cuerpo: dict | None = None,
                    publica: bool = False) -> dict:
    """Una llamada a Culqi, ya traducida a dict o a ErrorCulqi.

    QUE SE REINTENTA Y QUE NO, Y POR QUE IMPORTA TANTO AQUI

      Un reintento a ciegas sobre un POST es la forma clasica de cobrar dos
      veces: si la peticion llego a Culqi y lo que se perdio fue la RESPUESTA,
      repetirla crea una segunda suscripcion o un segundo cargo.

      Por eso solo se repite lo que es demostrablemente seguro:

        - GET (leer un plan, un cargo o un evento): idempotente por
          definicion.
        - httpx.ConnectError / ConnectTimeout en cualquier metodo: la conexion
          NO llego a establecerse, asi que Culqi no vio la peticion.

      Un ReadTimeout en un POST NO se repite, aunque sea tentador: ahi la
      peticion si viajo y puede haberse procesado. Se propaga y quien llama
      decide (el checkout ensena un error y no activa nada; el cliente ve que
      no se activo y lo intenta otra vez, esta vez con un token nuevo).
    """
    llave = _llave_publica() if publica else _llave_secreta()
    if not llave:
        raise ConfiguracionCulqi(
            "Falta CULQI_LLAVE_PUBLICA" if publica else
            "Falta CULQI_LLAVE_SECRETA")

    url = f"{BASE}/{ruta.lstrip('/')}"
    seguro_repetir = metodo.upper() == "GET"
    ultimo: Exception | None = None

    for intento in range(_REINTENTOS + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as cliente:
                r = await cliente.request(metodo, url, json=cuerpo,
                                          headers=_cabeceras(llave))
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            # No se establecio la conexion: Culqi no vio nada. Repetir es seguro
            # aunque sea un POST.
            ultimo = e
            log.warning("Culqi %s %s: sin conexion (intento %d)", metodo, ruta,
                        intento + 1)
            continue
        except httpx.HTTPError as e:
            if not seguro_repetir:
                log.error("Culqi %s %s fallo y NO se repite: %s", metodo, ruta, e)
                raise ErrorCulqi(
                    user_message="No pudimos contactar con la pasarela. "
                                 "Revisa tu correo antes de volver a intentarlo: "
                                 "si el cobro se hizo, te llegara el comprobante.",
                    merchant_message=f"{type(e).__name__}: {e}") from e
            ultimo = e
            log.warning("Culqi %s %s fallo (intento %d): %s", metodo, ruta,
                        intento + 1, e)
            continue

        try:
            datos = r.json()
        except ValueError:
            datos = {}

        if r.status_code >= 500 and seguro_repetir and intento < _REINTENTOS:
            log.warning("Culqi %s %s devolvio %s: se repite", metodo, ruta,
                        r.status_code)
            continue

        if r.status_code >= 400 or datos.get("object") == "error":
            raise _error_de(datos if isinstance(datos, dict) else {},
                            r.status_code)
        if not isinstance(datos, dict):
            raise ErrorCulqi(
                user_message="La pasarela respondio algo inesperado.",
                merchant_message=f"cuerpo no es un objeto JSON: {datos!r}",
                http=r.status_code)
        return datos

    raise ErrorCulqi(
        user_message="No pudimos contactar con la pasarela. Intenta de nuevo "
                     "en unos minutos.",
        merchant_message=f"{type(ultimo).__name__}: {ultimo}")


# ─── Respuestas simuladas ────────────────────────────────
#
# El modo simulado NO es un adorno de desarrollo: es lo que permite probar el
# checkout, el webhook y la cancelacion de punta a punta sin llaves y sin
# mover dinero. Los identificadores llevan "sim" bien visible para que, si uno
# se colara en produccion, se vea en el log en lugar de pasar por bueno.

def _sim(prefijo: str, semilla: str) -> str:
    import hashlib
    # sha256 y no sha1: esto solo genera un identificador estable de pruebas,
    # pero un sha1 en el arbol dispara los analizadores de seguridad cada vez
    # que alguien pasa uno, y una alarma que siempre suena deja de mirarse.
    return f"{prefijo}_sim_{hashlib.sha256(semilla.encode()).hexdigest()[:12]}"


# ─── Operaciones ─────────────────────────────────────────

async def crear_cliente(email: str, nombre: str = "", apellido: str = "",
                        telefono: str = "", direccion: str = "",
                        ciudad: str = "", pais: str = "PE") -> dict:
    """POST /v2/customers -> cus_…

    Culqi exige los siete campos aunque nosotros solo tengamos el correo: el
    checkout de LicitaPro pide correo y contrasena, no una direccion. Se
    rellenan con valores neutros y honestos ("Lima", "-") en vez de inventar
    datos del cliente: son obligatorios para la API, no un perfil que despues
    alguien lea como si fuera cierto.
    """
    if modo() == "simulado":
        return {"id": _sim("cus", email), "object": "customer", "email": email,
                "simulado": True}
    cuerpo = {
        "first_name": (nombre or email.split("@")[0])[:50],
        "last_name": (apellido or "-")[:50],
        "email": email,
        "address": (direccion or "-")[:100],
        "address_city": (ciudad or "Lima")[:30],
        "country_code": pais,
        "phone_number": (telefono or "000000000")[:15],
    }
    return await _peticion("POST", RUTA_CLIENTES, cuerpo)


async def crear_tarjeta(customer_id: str, token_id: str) -> dict:
    """POST /v2/cards -> crd_…

    `token_id` viene del NAVEGADOR (Culqi Checkout). Aqui nunca llega un numero
    de tarjeta: es lo que nos mantiene fuera del alcance de PCI-DSS.
    """
    if modo() == "simulado":
        return {"id": _sim("crd", token_id), "object": "card",
                "customer_id": customer_id, "simulado": True,
                "source": {"iin": {"card_brand": "Visa"}, "last_four": "4444"}}
    return await _peticion("POST", RUTA_TARJETAS,
                           {"customer_id": customer_id, "token_id": token_id})


async def crear_plan(nombre: str, short_name: str, descripcion: str,
                     monto, periodo: str, metadata: dict | None = None) -> dict:
    """POST /v2/recurrent/plans -> pln_…

    `monto` llega en SOLES (lo que hay en la tabla `planes`) y sale en
    centimos. Que la conversion viva aqui y no en quien llama es deliberado:
    es el unico sitio donde puede equivocarse por cien.

    `initial_cycles` va con `has_initial_charge: false` y `count: 0`: no hay
    ciclo inicial distinto ni cobro de alta. El primer cobro es el de la
    suscripcion, del importe del plan, y eso es lo que el checkout promete.
    """
    intervalo = intervalo_de(periodo)   # POR_CONFIRMAR: ver intervalo_de()
    centimos = a_centimos(monto)
    if modo() == "simulado":
        return {"id": _sim("pln", short_name), "object": "plan",
                "short_name": short_name, "amount": centimos,
                "interval_unit_time": intervalo, "simulado": True}
    cuerpo = {
        "name": nombre,
        "short_name": short_name,
        "description": descripcion,
        "amount": centimos,
        "currency": MONEDA,
        "interval_unit_time": intervalo,
        "interval_count": 1,
        "initial_cycles": {"count": 0, "has_initial_charge": False,
                           "amount": 0, "interval_unit_time": intervalo},
        "metadata": metadata or {},
    }
    return await _peticion("POST", RUTA_PLANES, cuerpo)


async def leer_plan(plan_id: str) -> dict:
    """GET /v2/recurrent/plans/{id}. Para comprobar que un id guardado sigue vivo."""
    if modo() == "simulado":
        return {"id": plan_id, "object": "plan", "simulado": True}
    return await _peticion("GET", f"{RUTA_PLANES}/{plan_id}")


async def crear_suscripcion(card_id: str, plan_id: str,
                            metadata: dict | None = None) -> dict:
    """POST /v2/recurrent/subscriptions -> sxn_…

    `tyc: true` declara que el cliente acepto los terminos. No es un adorno:
    es la constancia de que hubo consentimiento para un cobro recurrente, y en
    el checkout se recoge de verdad (el boton de pagar lo dice y enlaza a
    /terminos), no se pone a mano aqui.

    ESTA LLAMADA COBRA. No se reintenta sola -- ver `_peticion` --: repetirla
    tras un timeout de lectura crearia una segunda suscripcion cobrando dos
    veces al mismo cliente.
    """
    if modo() == "simulado":
        return {"id": _sim("sxn", f"{card_id}:{plan_id}"), "object": "subscription",
                "plan_id": plan_id, "card_id": card_id, "simulado": True}
    return await _peticion("POST", RUTA_SUSCRIPCIONES, {
        "card_id": card_id, "plan_id": plan_id, "tyc": True,
        "metadata": metadata or {},
    })


async def cancelar_suscripcion(subscription_id: str) -> dict:
    """DELETE /v2/recurrent/subscriptions/{id}. **IRREVERSIBLE.**

    Culqi no reactiva una suscripcion cancelada: para volver hay que crear otra,
    y eso exige la tarjeta otra vez. Quien llama tiene que habérselo dicho al
    cliente ANTES (web/suscripcion.py lo hace con una confirmacion explicita).
    """
    if modo() == "simulado":
        return {"id": subscription_id, "deleted": True, "simulado": True}
    return await _peticion("DELETE", f"{RUTA_SUSCRIPCIONES}/{subscription_id}")


async def leer_cargo(charge_id: str) -> dict:
    """GET /v2/charges/{id} -> chr_…

    ES LA PIEZA CENTRAL DE LA SEGURIDAD DEL WEBHOOK

      El aviso que llega por HTTP no se cree: se le saca el id del cargo y se
      pregunta AQUI, con la llave secreta, si ese cargo existe y como fue. Un
      aviso falsificado no sobrevive a esta llamada, haya firma o no -- y no
      sabemos si la hay (POR_CONFIRMAR).
    """
    if modo() == "simulado":
        return {"id": charge_id, "object": "charge", "simulado": True,
                "amount": 0, "currency_code": MONEDA,
                "outcome": {"type": "venta_exitosa", "code": "AUT0000"}}
    return await _peticion("GET", f"{RUTA_CARGOS}/{charge_id}")


async def leer_evento(event_id: str) -> dict:
    """GET /v2/events/{id} -> evt_…

    Segunda via de comprobacion cuando el aviso no trae un chr_ reconocible.
    """
    if modo() == "simulado":
        return {"id": event_id, "object": "event", "simulado": True, "data": {}}
    return await _peticion("GET", f"{RUTA_EVENTOS}/{event_id}")


# ─── Lectura de objetos de Culqi ─────────────────────────

def cargo_exitoso(cargo: dict) -> bool:
    """Si un objeto charge representa dinero cobrado.

    Culqi describe el resultado en `outcome.type`, y el unico valor que
    significa cobrado es "venta_exitosa". Se comprueba por lista blanca y no
    descartando los rechazos conocidos: un `type` nuevo o desconocido tiene que
    contar como NO cobrado. Equivocarse hacia "no cobro" retrasa una activacion
    y se arregla solo con el siguiente aviso; equivocarse hacia "si cobro"
    regala un mes de servicio a quien no pago.
    """
    outcome = cargo.get("outcome")
    if not isinstance(outcome, dict):
        return False
    return outcome.get("type") == "venta_exitosa"


def mensaje_para_el_cliente(cargo: dict) -> str:
    outcome = cargo.get("outcome") or {}
    return (outcome.get("user_message") or "").strip()


def marca_y_ultimos(tarjeta: dict) -> tuple[str | None, str | None]:
    """(marca, ultimos 4) de un objeto card, para ensenarlos en el panel.

    Se navega con `.get` encadenado y no con indices porque esta estructura es
    de Culqi y puede cambiar; que falte un nivel tiene que dejar la tarjeta sin
    etiqueta bonita, no tumbar un pago que ya se hizo.
    """
    fuente = tarjeta.get("source") or {}
    iin = fuente.get("iin") or {}
    marca = iin.get("card_brand") or fuente.get("card_brand")
    ultimos = fuente.get("last_four") or fuente.get("last_four_digits")
    return (str(marca) if marca else None,
            str(ultimos) if ultimos else None)
