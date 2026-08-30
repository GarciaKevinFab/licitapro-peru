"""Pruebas del alta de una cuenta nueva.

QUE PROTEGEN

  Lo unico que un cliente viene a comprar aqui es que le avisemos. El sistema
  sabia emparejar licitaciones, tenia el correo montado y funcionando, y aun
  asi llevaba desde su creacion con CERO avisos enviados.

  La causa no estaba en el emparejamiento ni en el envio: `repartir()` recorre
  tres canales -- Telegram, WhatsApp y correo -- y los tres nacian apagados.
  Telegram hay que vincularlo, WhatsApp hay que configurarlo, y
  `email_notificaciones` -- que guarda la direccion de destino -- nacia en
  NULL.

  Asi que quien se registraba pasaba sus 14 dias de prueba, que es la ventana
  que decide si paga, sin recibir una sola alerta. Y no habia forma de
  enterarse: no falla nada, no hay excepcion, el panel se ve bien. Solo no
  llega nada.

POR QUE ESTAS PRUEBAS NECESITAN BASE

  Lo que se comprueba es el estado con el que NACE una cuenta, y eso lo
  reparten `crear_usuario` y los DEFAULT de la tabla entre los dos. Fingir la
  base probaria la mitad, y la mitad que casi nunca es la rota.
"""
from shared.db import connection
from tests.conftest import sin_base


@sin_base
async def test_una_cuenta_nueva_nace_con_el_aviso_por_correo_encendido(usuario):
    """El correo del registro queda como destino de las alertas.

    Es la direccion que el usuario acaba de dar para esto, y es el unico canal
    que no exige configurar nada despues. Sigue siendo suyo: en Configuracion
    puede cambiarlo o vaciarlo, y vaciarlo apaga el canal.
    """
    async with connection() as c:
        destino = await c.fetchval(
            "SELECT email_notificaciones FROM user_config WHERE usuario_id = $1",
            usuario["id"])
    assert destino == usuario["email"].lower()


@sin_base
async def test_una_cuenta_nueva_puede_recibir_avisos_desde_el_primer_minuto(usuario):
    """Los tres requisitos de `repartir()`, comprobados juntos.

    Se miran a la vez a proposito: cada uno por separado estaba bien y el
    conjunto no enviaba nada. Un aviso solo sale si la cuenta esta activa, la
    configuracion no esta apagada, y hay al menos un canal con destino.
    """
    async with connection() as c:
        fila = await c.fetchrow(
            """SELECT u.activo, c.activo AS cfg_activo, c.email_notificaciones,
                      c.horario_inicio, c.horario_fin
                 FROM usuarios u JOIN user_config c ON c.usuario_id = u.id
                WHERE u.id = $1""", usuario["id"])

    assert fila["activo"] is True
    assert fila["cfg_activo"] is True
    assert fila["email_notificaciones"], "sin canal no sale ningun aviso"
    # La franja por defecto tiene que cubrir una jornada peruana. El contenedor
    # corre con TZ=America/Lima (ver Dockerfile), asi que estas horas son las
    # que el usuario cree que son.
    assert fila["horario_inicio"] == "07:00"
    assert fila["horario_fin"] == "22:00"


@sin_base
async def test_la_cuenta_nueva_nace_con_su_prueba(usuario):
    """Sin suscripcion el panel no sabria que plan aplicar ni cuantas empresas."""
    from shared.suscripciones import estado_suscripcion

    susc = await estado_suscripcion(usuario["id"])
    assert susc["existe"] is True
    assert susc["estado_efectivo"] == "prueba"
    assert susc["acceso"] is True
    # Y con derecho a alertas: es lo que se esta probando durante la prueba.
    assert susc["alertas"] is True
