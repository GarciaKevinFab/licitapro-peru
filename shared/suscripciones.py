"""Maquina de estados de la suscripcion y limites por plan.

Estados y como se pasa de uno a otro:

    prueba     -> activa      pago confirmado
    prueba     -> vencida     se acabaron los dias sin pagar
    activa     -> vencida     llego la renovacion sin cobro exitoso
    vencida    -> activa      pago confirmado (reintento o manual)
    vencida    -> suspendida  pasaron los dias de gracia
    cualquiera -> cancelada   el usuario cancela

Por que gracia y no corte inmediato: una tarjeta rebota por mil motivos
(limite, vencimiento, el banco), y cortarle el servicio a alguien que si quiere
pagar es la forma mas cara de perder un cliente. `vencida` conserva el acceso;
`suspendida` lo corta.

Los limites se comprueban al ACTUAR, no al mostrar: ocultar un boton no impide
que alguien mande el formulario igual.
"""
import logging
from datetime import datetime, timedelta

from shared import fechas
from shared.db import connection

log = logging.getLogger("shared.suscripciones")

DIAS_PRUEBA = 14
DIAS_GRACIA = 7           # cuanto sigue funcionando una suscripcion vencida
MAX_INTENTOS = 4          # reintentos de cobro antes de suspender

ACCESO_PERMITIDO = ("prueba", "activa", "vencida")

# Plan al que se cae cuando se agota la gracia, en vez de perder el acceso.
PLAN_GRATUITO = "gratis"   # 'vencida' sigue en gracia


def estado_efectivo_de(estado: str, vence, ahora: datetime | None = None) -> str:
    """El estado que de verdad aplica hoy, a partir del guardado y la fecha.

    Es la regla de `estado_suscripcion` sacada a una funcion sin base para que
    el panel del dueno la aplique a cien cuentas de una consulta y para que se
    pueda probar con fechas inventadas. Cambiarla aqui cambia las dos cosas, que
    es lo que se quiere: una sola definicion de "vencida" y de "suspendida".
    """
    ahora = ahora or fechas.ahora()
    if estado in ("prueba", "activa") and vence and vence < ahora:
        estado = "vencida"
    if estado == "vencida" and vence and vence + timedelta(days=DIAS_GRACIA) < ahora:
        estado = "suspendida"
    return estado


async def estado_suscripcion(usuario_id: int) -> dict:
    """Suscripcion + plan + si tiene acceso, resuelto en un solo lugar."""
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT s.*, p.nombre AS plan_nombre, p.precio_mensual,
                      p.precio_anual, p.max_empresas, p.max_regiones, p.analisis_ia,
                      p.alertas
                 FROM suscripciones s
                 JOIN planes p ON p.codigo = s.plan_codigo
                WHERE s.usuario_id = $1""",
            usuario_id)

    if not fila:
        # Sin suscripcion NO se bloquea: seria dejar fuera a alguien por un
        # fallo nuestro. Se registra para poder detectarlo.
        log.warning("Usuario %s sin fila de suscripcion", usuario_id)
        return {"existe": False, "acceso": True, "estado_efectivo": "sin_suscripcion",
                "dias_restantes": None, "en_gracia": False}

    d = dict(fila)
    ahora = fechas.ahora()
    vence = d.get("vence")
    dias = (vence - ahora).days if vence else None

    # El vencimiento se evalua al leer: asi el estado es correcto aunque el
    # proceso de renovacion no haya corrido todavia.
    estado = estado_efectivo_de(d["estado"], vence, ahora)

    d["estado_efectivo"] = estado
    d["existe"] = True
    d["dias_restantes"] = dias
    d["en_gracia"] = estado == "vencida"
    d["degradado"] = False

    if estado == "suspendida":
        # Agotada la gracia NO se expulsa: se cae al plan gratuito. Bloquear del
        # todo empuja al proveedor a la web del competidor, que si le deja
        # mirar; y el que aun no ha visto pasar una licitacion suya no tiene con
        # que decidir si pagar. Dejarle el panel no nos cuesta nada -- el pozo
        # ya esta scrapeado -- y lo que se corta es lo que da valor y cuesta
        # dinero: los avisos, la IA y las empresas de mas.
        limites = await _limites_plan(PLAN_GRATUITO)
        if limites:
            d.update(limites)
            d["plan_codigo"] = PLAN_GRATUITO
            d["degradado"] = True
            d["acceso"] = True
            return d
        # Sin plan gratuito en la tabla se mantiene el corte: es preferible
        # bloquear a conceder por accidente un acceso sin limites conocidos.

    d["acceso"] = estado in ACCESO_PERMITIDO
    return d


async def _limites_plan(codigo: str) -> dict | None:
    """Capacidades de un plan, con los mismos nombres que trae estado_suscripcion."""
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT nombre AS plan_nombre, precio_mensual, precio_anual,
                      max_empresas, max_regiones, analisis_ia, alertas
                 FROM planes WHERE codigo = $1""",
            codigo)
    return dict(fila) if fila else None


async def crear_suscripcion_prueba(usuario_id: int, plan: str = "pro") -> None:
    """Da de alta la prueba al registrarse. Sin esto la cuenta nace sin plan."""
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO suscripciones (usuario_id, plan_codigo, estado, periodo, vence)
               VALUES ($1, $2, 'prueba', 'mensual', NOW() + ($3 || ' days')::interval)
               ON CONFLICT (usuario_id) DO NOTHING""",
            usuario_id, plan, str(DIAS_PRUEBA))


# ─── Limites por plan ────────────────────────────────────

async def puede_agregar_empresa(usuario_id: int) -> tuple[bool, str]:
    """(permitido, motivo). Se comprueba al guardar, no al pintar el boton."""
    susc = await estado_suscripcion(usuario_id)
    if not susc.get("acceso"):
        return False, "Tu suscripción está suspendida. Actualiza tu pago para continuar."

    tope = susc.get("max_empresas")
    if tope is None:
        return True, ""
    async with connection() as conn:
        actuales = await conn.fetchval(
            "SELECT COUNT(*) FROM empresas WHERE usuario_id=$1 AND activa=TRUE",
            usuario_id)
    if actuales >= tope:
        plan = susc.get("plan_nombre") or susc.get("plan_codigo")
        return False, (f"Tu plan {plan} permite {tope} empresa(s) activa(s). "
                       f"Cambia de plan o desactiva una para agregar otra.")
    return True, ""


async def limite_regiones(usuario_id: int) -> int | None:
    """None = sin limite."""
    susc = await estado_suscripcion(usuario_id)
    return susc.get("max_regiones")


# ─── Cobros ──────────────────────────────────────────────

async def registrar_intento(usuario_id: int, monto, numero_orden: str) -> int | None:
    """Crea el pago en 'pendiente'. None si ese numero de orden ya existia.

    La unicidad de izipay_order_number es la idempotencia: si el usuario pulsa
    dos veces o el webhook llega repetido, no se duplica el cobro.
    """
    async with connection() as conn:
        susc_id = await conn.fetchval(
            "SELECT id FROM suscripciones WHERE usuario_id=$1", usuario_id)
        if not susc_id:
            return None
        return await conn.fetchval(
            """INSERT INTO pagos_suscripcion
                   (suscripcion_id, monto, estado, izipay_order_number)
               VALUES ($1, $2, 'pendiente', $3)
               ON CONFLICT (izipay_order_number) DO NOTHING
               RETURNING id""",
            susc_id, monto, numero_orden)


async def confirmar_pago(numero_orden: str, transaction_id: str | None,
                         respuesta: dict | None = None) -> bool:
    """Marca el pago como pagado y extiende la suscripcion. Idempotente.

    Devuelve True solo la PRIMERA vez que confirma ese numero de orden: si el
    webhook llega dos veces, la segunda no vuelve a extender el periodo.
    """
    import json
    async with connection() as conn:
        pago = await conn.fetchrow(
            """UPDATE pagos_suscripcion
                  SET estado='pagado', confirmado_en=NOW(),
                      izipay_transaction_id=$2, respuesta=$3
                WHERE izipay_order_number=$1 AND estado <> 'pagado'
             RETURNING id, suscripcion_id""",
            numero_orden, transaction_id,
            json.dumps(respuesta) if respuesta else None)
        if not pago:
            log.info("Pago %s ya confirmado o inexistente: no se repite", numero_orden)
            return False

        susc = await conn.fetchrow(
            "SELECT periodo FROM suscripciones WHERE id=$1", pago["suscripcion_id"])
        dias = 365 if (susc and susc["periodo"] == "anual") else 30
        # Se extiende desde el vencimiento si aun no paso; si ya paso, desde hoy.
        # Asi renovar antes de tiempo no regala dias ni los quita.
        await conn.execute(
            """UPDATE suscripciones
                  SET estado='activa',
                      intentos_fallidos=0,
                      vence = GREATEST(COALESCE(vence, NOW()), NOW())
                              + ($2 || ' days')::interval
                WHERE id=$1""",
            pago["suscripcion_id"], str(dias))
    log.info("Pago %s confirmado; suscripcion %s extendida %s dias",
             numero_orden, pago["suscripcion_id"], dias)
    return True


async def registrar_intento_fallido(usuario_id: int) -> None:
    """Suma un intento fallido y suspende al pasarse del maximo."""
    async with connection() as conn:
        await conn.execute(
            """UPDATE suscripciones
                  SET intentos_fallidos = intentos_fallidos + 1,
                      ultimo_intento = NOW(),
                      estado = CASE WHEN intentos_fallidos + 1 >= $2
                                    THEN 'suspendida' ELSE estado END
                WHERE usuario_id = $1""",
            usuario_id, MAX_INTENTOS)


async def guardar_token_tarjeta(usuario_id: int, token: str,
                                marca: str | None = None, ultimos: str | None = None) -> None:
    """Guarda el token cifrado. Nunca se guarda el numero de tarjeta."""
    from shared.seguridad import cifrar
    async with connection() as conn:
        await conn.execute(
            """UPDATE suscripciones
                  SET token_tarjeta=$2, tarjeta_marca=$3, tarjeta_ultimos=$4
                WHERE usuario_id=$1""",
            usuario_id, cifrar(token), marca, ultimos)


async def cambiar_plan(usuario_id: int, plan_codigo: str, periodo: str) -> bool:
    async with connection() as conn:
        existe = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM planes WHERE codigo=$1 AND activo=TRUE)",
            plan_codigo)
        if not existe or periodo not in ("mensual", "anual"):
            return False
        await conn.execute(
            "UPDATE suscripciones SET plan_codigo=$2, periodo=$3 WHERE usuario_id=$1",
            usuario_id, plan_codigo, periodo)
    return True


async def activar_manual(usuario_id: int, plan_codigo: str, estado: str, periodo: str,
                         vence, nota: str | None, monto=None, por: str | None = None) -> bool:
    """Plan y estado puestos A MANO por el dueno del producto.

    Existe porque no todo el mundo paga por Izipay: hay clientes que pagan en
    efectivo, por Yape o por transferencia, y otros a los que se les regala un
    periodo. Sin esto, el unico camino para activar un plan era la pasarela.

    Si no hay fila de suscripcion se crea; si la hay, se pisa. Cuando se indica
    un monto, queda un pago con metodo 'manual' en pagos_suscripcion para que
    el historial de la cuenta y los informes cuadren con lo cobrado.
    """
    import json
    if estado not in ("prueba", "activa", "vencida", "suspendida", "cancelada"):
        return False
    if periodo not in ("mensual", "anual"):
        return False
    async with connection() as conn:
        if not await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM planes WHERE codigo=$1 AND activo=TRUE)", plan_codigo):
            return False
        susc_id = await conn.fetchval(
            "SELECT id FROM suscripciones WHERE usuario_id=$1", usuario_id)
        if susc_id:
            await conn.execute(
                """UPDATE suscripciones
                      SET plan_codigo=$2, estado=$3, periodo=$4, vence=$5,
                          intentos_fallidos=0,
                          cancelada_en=CASE WHEN $3='cancelada' THEN NOW() ELSE NULL END
                    WHERE id=$1""",
                susc_id, plan_codigo, estado, periodo, vence)
        else:
            susc_id = await conn.fetchval(
                """INSERT INTO suscripciones (usuario_id, plan_codigo, estado, periodo, inicia, vence)
                   VALUES ($1, $2, $3, $4, NOW(), $5) RETURNING id""",
                usuario_id, plan_codigo, estado, periodo, vence)
        if monto is not None and float(monto) > 0:
            await conn.execute(
                """INSERT INTO pagos_suscripcion
                       (suscripcion_id, monto, moneda, estado, metodo, respuesta, confirmado_en)
                   VALUES ($1, $2, 'PEN', 'pagado', 'manual', $3, NOW())""",
                susc_id, monto, json.dumps({"nota": nota, "por": por}))
    log.info("Suscripcion de %s puesta a mano: %s/%s hasta %s por %s",
             usuario_id, plan_codigo, estado, vence, por)
    return True


async def cancelar(usuario_id: int) -> None:
    """Cancela sin cortar el acceso: se conserva hasta el fin del periodo pagado."""
    async with connection() as conn:
        await conn.execute(
            """UPDATE suscripciones
                  SET estado='cancelada', cancelada_en=NOW(), token_tarjeta=NULL
                WHERE usuario_id=$1""",
            usuario_id)


async def renovaciones_pendientes() -> list[dict]:
    """Suscripciones con tarjeta guardada que toca renovar.

    Solo las que tienen token: sin tarjeta guardada no hay nada que reintentar
    automaticamente. El filtro de ultimo_intento evita machacar la pasarela.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT s.id, s.usuario_id, s.periodo, s.token_tarjeta,
                      s.intentos_fallidos, u.email,
                      CASE WHEN s.periodo='anual' THEN p.precio_anual
                           ELSE p.precio_mensual END AS monto
                 FROM suscripciones s
                 JOIN usuarios u ON u.id = s.usuario_id
                 JOIN planes p ON p.codigo = s.plan_codigo
                WHERE s.token_tarjeta IS NOT NULL
                  AND s.estado IN ('activa','vencida')
                  -- Nunca se cobra un plan de precio cero. Sin esto, alguien
                  -- que cayo al plan gratuito conservando su tarjeta generaria
                  -- una orden de S/0.00 contra la pasarela cada dia: la
                  -- rechazaria, sumaria un intento fallido y acabaria
                  -- suspendiendo a un usuario que no debe nada.
                  AND COALESCE(CASE WHEN s.periodo='anual' THEN p.precio_anual
                                    ELSE p.precio_mensual END, 0) > 0
                  AND s.vence < NOW()
                  AND s.intentos_fallidos < $1
                  AND (s.ultimo_intento IS NULL
                       OR s.ultimo_intento < NOW() - INTERVAL '1 day')
                ORDER BY s.vence""",
            MAX_INTENTOS)
    return [dict(f) for f in filas]


# ─── Suscripciones recurrentes de Culqi ──────────────────
#
# QUE CAMBIA RESPECTO A IZIPAY
#
#   Con Izipay el cobro lo disparabamos nosotros: `renovaciones_pendientes` +
#   un cron que cobra la tarjeta guardada. Con Culqi el cobro lo dispara
#   Culqi, cada periodo, y nos avisa. Nuestro trabajo pasa de "cobrar" a
#   "enterarse y no equivocarse al contar los periodos".
#
#   Por eso lo de aqui gira todo alrededor de la misma pregunta: este aviso,
#   ¿ya lo apliqué? Culqi reintenta lo que no recibe 200, asi que el mismo
#   cobro puede llegar tres veces, y cada aplicacion de mas es un mes de
#   servicio regalado que nadie reclama.

DIAS_POR_PERIODO = {"mensual": 30, "anual": 365}


def _dias_de(periodo: str) -> int:
    return DIAS_POR_PERIODO.get(periodo, 30)


async def suscripcion_por_culqi(subscription_id: str | None = None,
                                customer_id: str | None = None,
                                email: str | None = None) -> dict | None:
    """De un identificador de Culqi a nuestra fila. None si no es nuestra.

    SE BUSCA POR TRES VIAS Y EN ESTE ORDEN

      El webhook trae lo que trae -- su forma exacta es POR_CONFIRMAR -- y no
      podemos exigir un campo concreto. Se intenta de lo mas especifico a lo
      menos:

        sxn_  identifica exactamente una suscripcion nuestra.
        cus_  identifica al cliente; si tuviera dos suscripciones habria
              ambiguedad, pero la tabla tiene usuario_id UNICO, asi que no.
        email ultimo recurso, y solo sirve si el correo de Culqi es el de la
              cuenta.

      Devolver None NO es un error: puede ser un aviso de otro comercio o de
      otro producto. Quien llama lo registra y contesta 200.
    """
    condiciones, valores = [], []
    if subscription_id:
        valores.append(subscription_id)
        condiciones.append(f"s.culqi_subscription_id = ${len(valores)}")
    if customer_id:
        valores.append(customer_id)
        condiciones.append(f"s.culqi_customer_id = ${len(valores)}")
    if email:
        valores.append(email.strip().lower())
        condiciones.append(f"LOWER(u.email) = ${len(valores)}")
    if not condiciones:
        return None

    async with connection() as conn:
        fila = await conn.fetchrow(
            f"""SELECT s.id, s.usuario_id, s.periodo, s.plan_codigo, s.estado,
                       s.vence, s.culqi_subscription_id, s.culqi_card_id,
                       s.culqi_customer_id, u.email
                  FROM suscripciones s
                  JOIN usuarios u ON u.id = s.usuario_id
                 WHERE {' OR '.join(condiciones)}
                 LIMIT 1""",
            *valores)
    return dict(fila) if fila else None


async def datos_culqi(usuario_id: int) -> dict | None:
    """Los identificadores de Culqi de una cuenta, para cancelar o cambiar plan."""
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT id, periodo, plan_codigo, estado, vence,
                      culqi_customer_id, culqi_card_id, culqi_subscription_id,
                      tarjeta_marca, tarjeta_ultimos
                 FROM suscripciones WHERE usuario_id = $1""",
            usuario_id)
    return dict(fila) if fila else None


async def activar_por_culqi(usuario_id: int, plan_codigo: str, periodo: str,
                            monto, customer_id: str, card_id: str,
                            subscription_id: str, marca: str | None = None,
                            ultimos: str | None = None,
                            charge_id: str | None = None,
                            respuesta: dict | None = None) -> bool:
    """Deja la cuenta activa tras contratar en Culqi, y registra el pago inicial.

    TODO EN UNA TRANSACCION, Y NO POR PULCRITUD

      Al llegar aqui la suscripcion en Culqi YA ESTA CREADA y el primer cobro
      YA SE HIZO. Si la activacion local se quedara a medias -- suscripcion
      activa sin pago registrado, o pago registrado sin activar --, el cliente
      tendria un cargo en su tarjeta y una cuenta que no le deja entrar, o un
      historial que no cuadra con su banco. O entra entero o no entra nada, y
      quien llama cancela en Culqi lo que no pudo asentar aqui.

    EL PERIODO SE CONCEDE AQUI, NO EN EL WEBHOOK

      `vence` se pone a un periodo vista desde HOY. El aviso de ese mismo
      primer cobro llegara despues por webhook, y ahi NO puede volver a
      extender: ver `aplicar_cargo_culqi`, que reconoce este pago y lo adopta
      en vez de contarlo dos veces.
    """
    import json
    dias = _dias_de(periodo)
    async with connection() as conn, conn.transaction():
        susc_id = await conn.fetchval(
            """UPDATE suscripciones
                  SET plan_codigo=$2, periodo=$3, estado='activa',
                      inicia=NOW(), vence=NOW() + ($4 || ' days')::interval,
                      cancelada_en=NULL, intentos_fallidos=0,
                      culqi_customer_id=$5, culqi_card_id=$6,
                      culqi_subscription_id=$7,
                      tarjeta_marca=COALESCE($8, tarjeta_marca),
                      tarjeta_ultimos=COALESCE($9, tarjeta_ultimos)
                WHERE usuario_id=$1
             RETURNING id""",
            usuario_id, plan_codigo, periodo, str(dias),
            customer_id, card_id, subscription_id, marca, ultimos)
        if not susc_id:
            log.error("Culqi activo la suscripcion %s del usuario %s pero no "
                      "hay fila local que actualizar", subscription_id, usuario_id)
            return False

        await conn.execute(
            """INSERT INTO pagos_suscripcion
                   (suscripcion_id, monto, moneda, estado, metodo,
                    culqi_charge_id, respuesta, confirmado_en)
               VALUES ($1, $2, 'PEN', 'pagado', 'culqi', $3, $4, NOW())
               -- El WHERE no sobra: el indice de culqi_charge_id es PARCIAL, y
               -- sin repetir su predicado Postgres no lo reconoce como arbitro
               -- ("there is no unique or exclusion constraint matching the ON
               -- CONFLICT specification") y el INSERT revienta. Se descubrio
               -- corriendo las pruebas contra una base de verdad; sin base,
               -- este error no aparece hasta el primer cliente.
               ON CONFLICT (culqi_charge_id) WHERE culqi_charge_id IS NOT NULL
               DO NOTHING""",
            susc_id, monto, charge_id,
            json.dumps(respuesta) if respuesta else None)

    log.info("Suscripcion de Culqi %s activada para el usuario %s (%s/%s, %s dias)",
             subscription_id, usuario_id, plan_codigo, periodo, dias)
    return True


async def aplicar_cargo_culqi(suscripcion_id: int, monto, charge_id: str,
                              event_id: str | None = None,
                              respuesta: dict | None = None,
                              periodo: str | None = None) -> str:
    """Aplica un cobro de Culqi ya COMPROBADO contra su API. Idempotente.

    Devuelve 'aplicado' | 'adoptado' | 'repetido'.

    LOS TRES CASOS SON DISTINTOS Y HAY QUE DISTINGUIRLOS

      repetido  Ese chr_ ya esta en la tabla. Culqi reintenta lo que no recibe
                200, asi que esto es lo NORMAL, no un error. No se toca nada.

      adoptado  Es el aviso del PRIMER cobro, el que disparo la creacion de la
                suscripcion en el checkout. Ese periodo ya se concedio alli
                (`activar_por_culqi` puso `vence` a un periodo vista), asi que
                aqui solo se le pega el chr_ real a esa fila y NO se extiende.

                Sin este caso, todo el que contrata recibiria dos periodos por
                un pago: uno en el checkout y otro al llegar el webhook. No da
                error, no aparece en ningun log, y son treinta dias regalados
                por cliente.

      aplicado  Una renovacion de verdad: Culqi cobro sola al vencer el
                periodo. Se registra el pago y se extiende.

    LA IDEMPOTENCIA LA GARANTIZA LA BASE, NO ESTE CODIGO

      El INSERT lleva ON CONFLICT sobre el indice unico parcial de
      culqi_charge_id. Comprobar antes con un SELECT y decidir despues es
      exactamente la carrera que dos avisos simultaneos ganan.
    """
    import json
    cuerpo = json.dumps(respuesta) if respuesta else None
    async with connection() as conn, conn.transaction():
        if await conn.fetchval(
                "SELECT 1 FROM pagos_suscripcion WHERE culqi_charge_id=$1",
                charge_id):
            return "repetido"

        # ¿Es el aviso del cobro inicial que el checkout ya asento? Se busca el
        # pago de Culqi mas reciente sin cargo asociado. La ventana de 2 dias
        # evita adoptar un pago viejo por accidente: un aviso que tarda dos
        # dias en llegar ya no es el del alta.
        adoptado = await conn.fetchval(
            """UPDATE pagos_suscripcion
                  SET culqi_charge_id=$2, culqi_event_id=$3,
                      respuesta=COALESCE(respuesta, $4)
                WHERE id = (SELECT id FROM pagos_suscripcion
                             WHERE suscripcion_id=$1 AND metodo='culqi'
                               AND culqi_charge_id IS NULL
                               AND created_at > NOW() - INTERVAL '2 days'
                             ORDER BY created_at DESC LIMIT 1)
             RETURNING id""",
            suscripcion_id, charge_id, event_id, cuerpo)
        if adoptado:
            log.info("Cargo %s adoptado por el pago inicial de la suscripcion %s: "
                     "el periodo ya se concedio en el checkout",
                     charge_id, suscripcion_id)
            return "adoptado"

        nuevo = await conn.fetchval(
            """INSERT INTO pagos_suscripcion
                   (suscripcion_id, monto, moneda, estado, metodo,
                    culqi_charge_id, culqi_event_id, respuesta, confirmado_en)
               VALUES ($1, $2, 'PEN', 'pagado', 'culqi', $3, $4, $5, NOW())
               -- Mismo predicado que el indice parcial: ver activar_por_culqi.
               ON CONFLICT (culqi_charge_id) WHERE culqi_charge_id IS NOT NULL
               DO NOTHING
               RETURNING id""",
            suscripcion_id, monto, charge_id, event_id, cuerpo)
        if not nuevo:
            # Otro aviso identico gano la carrera entre el SELECT y el INSERT.
            return "repetido"

        dias = _dias_de(periodo) if periodo else await conn.fetchval(
            "SELECT CASE WHEN periodo='anual' THEN 365 ELSE 30 END "
            "FROM suscripciones WHERE id=$1", suscripcion_id)
        # Se extiende desde el vencimiento si aun no paso; si ya paso, desde
        # hoy. Asi un cobro adelantado no regala dias ni los quita.
        await conn.execute(
            """UPDATE suscripciones
                  SET estado='activa', intentos_fallidos=0,
                      vence = GREATEST(COALESCE(vence, NOW()), NOW())
                              + ($2 || ' days')::interval
                WHERE id=$1""",
            suscripcion_id, str(dias))

    log.info("Cargo %s aplicado: suscripcion %s extendida", charge_id, suscripcion_id)
    return "aplicado"


async def limpiar_culqi(usuario_id: int) -> None:
    """Olvida la suscripcion de Culqi de una cuenta, conservando cliente y tarjeta.

    Se llama al cancelar y al cambiar de plan. El `cus_` y el `crd_` se
    conservan a proposito: son los que permiten crear la suscripcion nueva sin
    volver a pedirle la tarjeta al cliente. Lo que desaparece es el `sxn_`,
    que es lo unico que deja de existir en Culqi tras un DELETE.
    """
    async with connection() as conn:
        await conn.execute(
            "UPDATE suscripciones SET culqi_subscription_id=NULL WHERE usuario_id=$1",
            usuario_id)


# ─── Puerta de acceso al producto ────────────────────────

# Rutas que SIEMPRE se pueden usar, aunque la suscripcion este suspendida.
# Cortar el acceso a la pagina de pago seria dispararse en el pie: el cliente
# que quiere pagar no podria. Y cerrar sesion o recuperar la contrasena tampoco
# dependen de estar al dia.
RUTAS_LIBRES = (
    "/entrar", "/registro", "/salir", "/recuperar",
    "/suscripcion", "/webhooks/", "/salud", "/static",
    # El escaparate y el checkout publicos. Van aqui por dos motivos, y cada
    # uno basta por si solo:
    #
    #   1. Se ven SIN sesion. Hoy el portero ya deja pasar a quien no trae
    #      cookie, pero el dia que eso cambie, la pagina donde se vende el
    #      producto no puede quedar detras del cobro del producto.
    #   2. Con la suscripcion SUSPENDIDA tambien se ven. Quien llega aqui viene
    #      justamente a pagar: redirigirle a "tu suscripcion esta suspendida"
    #      seria devolverle al sitio del que acaba de salir.
    "/precios", "/comprar",
    # El Libro de Reclamaciones NUNCA puede quedar detras del cobro: la Ley
    # 29571 da derecho a reclamar a cualquiera, y el caso mas probable es
    # justo alguien a quien le cortamos el servicio.
    "/reclamaciones",
    # Las paginas legales se leen con la suscripcion suspendida o sin cuenta:
    # el derecho a saber que hacemos con tus datos y a pedir que los borremos
    # no depende de estar al dia con el pago.
    "/privacidad", "/terminos",
    # La vista de gasto del dueno. Va aqui porque este middleware corta el
    # producto cuando la suscripcion vence, y el dueno no deja de necesitar sus
    # metricas porque su propia cuenta caduque: es justo cuando mas falta hacen.
    # Quien puede entrar lo decide `web/admin.py` comparando el correo con
    # LICITAPRO_ADMIN_EMAIL, y sin esa variable responde 404 a todo el mundo.
    "/admin",
)


def ruta_libre(camino: str) -> bool:
    """Si el camino escapa del portero de suscripcion.

    POR QUE NO ES UN `startswith` A SECAS

      Lo era, y hoy no liberaba nada indebido de casualidad: ninguna de las 69
      rutas declaradas empieza por un prefijo libre sin ser suya. Pero con esa
      regla, el dia que alguien anada `/registro-empresa` o `/entrar-como`,
      esas rutas quedan SIN COBRAR y sin que nada lo diga -- porque empiezan
      por "/registro" y por "/entrar".

      Un fallo de este tipo no da error ni ticket: solo deja de facturar. Se
      exige el limite de segmento, que es lo que se pretendia desde el
      principio; los prefijos que de verdad quieren cubrir un arbol entero se
      escriben terminados en "/" y se ven a simple vista en RUTAS_LIBRES.
    """
    for r in RUTAS_LIBRES:
        if r.endswith("/"):
            if camino.startswith(r):
                return True
        elif camino == r or camino.startswith(r + "/"):
            return True
    return False


async def regiones_permitidas(usuario_id: int, regiones: list[str]) -> tuple[list[str], str]:
    """Recorta la lista de regiones al tope del plan. Devuelve (lista, aviso).

    Se recorta en vez de rechazar el formulario entero: al bajar de plan, el
    usuario tendria mas regiones de las que le tocan y no podria guardar ningun
    cambio hasta adivinar cuales quitar.
    """
    tope = (await estado_suscripcion(usuario_id)).get("max_regiones")
    if tope is None or len(regiones) <= tope:
        return regiones, ""
    return regiones[:tope], (
        f"Tu plan permite {tope} regiones y elegiste {len(regiones)}. "
        f"Se guardaron las {tope} primeras; cambia de plan para cubrir más.")


async def puede_usar_ia(usuario_id: int) -> bool:
    """El analisis de bases con IA solo esta en los planes que lo incluyen.

    Importa de verdad: cada analisis cuesta dinero en la API de Anthropic, y
    quien lo paga es el dueno de la plataforma, no el cliente.
    """
    susc = await estado_suscripcion(usuario_id)
    return bool(susc.get("acceso")) and bool(susc.get("analisis_ia"))


async def puede_recibir_alertas(usuario_id: int) -> bool:
    """Si su plan incluye avisos.

    Es la linea de pago del producto: mirar el panel es gratis, que te avisen a
    tiempo se paga. Se comprueba al ENVIAR y no al pintar la configuracion,
    porque cada mensaje de WhatsApp cuesta dinero y el ahorro solo es real si
    la comprobacion esta donde se gasta.
    """
    susc = await estado_suscripcion(usuario_id)
    return bool(susc.get("acceso")) and bool(susc.get("alertas"))
