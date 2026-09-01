"""Libro de Reclamaciones virtual (Ley 29571 y D.S. 101-2022-PCM).

OBLIGATORIO, Y ADEMAS LO MIRA LA PASARELA

  Cualquiera que venda a un consumidor en Peru tiene que tenerlo, y el enlace
  debe estar visible en la pagina principal. Es de las primeras cosas que
  comprueba quien valida un comercio, junto al carrito y a los datos del
  proveedor.

PUBLICO Y SIN SESION, Y NO ES POR COMODIDAD

  La ley da derecho a reclamar a cualquiera: cliente o no, con sesion o sin
  ella. Pedir cuenta para reclamar seria justo lo contrario de lo que la norma
  protege -- y dejaria fuera el caso mas probable, que es alguien que ya no
  puede entrar.

LA COPIA AL CONSUMIDOR NO PUEDE DEPENDER DEL CORREO

  La ley exige entregarle copia de su hoja. Lo normal es mandarla por correo, y
  se manda. Pero el SMTP puede estar caido -- ahora mismo lo esta, rechazando la
  autenticacion -- y un reclamo perdido porque el correo no sale es exactamente
  el fallo que la norma busca impedir.

  Por eso el numero correlativo se ENSENA EN PANTALLA siempre, la hoja queda
  guardada pase lo que pase, y el fallo de envio se registra en el log en vez
  de tumbar la operacion. Guardar es lo obligatorio; avisar es lo deseable.
"""
import logging
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from shared.db import connection
from web.auth import usuario_actual

log = logging.getLogger("web.reclamaciones")
router = APIRouter()

# El plazo de respuesta que fija la norma.
DIAS_HABILES_RESPUESTA = 15

TIPOS = ("reclamo", "queja")
DOCUMENTOS = ("DNI", "RUC", "CE", "Pasaporte")


def _plantillas(request: Request):
    return request.app.state.templates


def limite_respuesta(desde: datetime, dias: int = DIAS_HABILES_RESPUESTA) -> datetime:
    """La fecha limite, contando dias HABILES.

    Contarlos como naturales daria una fecha ANTERIOR a la que manda la ley, y
    nos pondriamos en falta antes de tiempo sin haberlo hecho.

    No se descuentan feriados: no hay calendario peruano en el sistema. El
    plazo que se guarda es entonces el mas exigente de los dos posibles, que es
    el lado correcto por el que equivocarse -- responder antes nunca incumple.
    """
    fecha, restantes = desde, dias
    while restantes > 0:
        fecha += timedelta(days=1)
        if fecha.weekday() < 5:      # lunes a viernes
            restantes -= 1
    return fecha


def _codigo(numero: int) -> str:
    """LR-000001. Con ese numero se acude a INDECOPI, asi que se ensena entero."""
    return f"LR-{numero:06d}"


def _contexto(request, usuario, **extra) -> dict:
    base = {
        "request": request, "usuario": usuario, "tipos": TIPOS,
        "documentos": DOCUMENTOS, "dias_habiles": DIAS_HABILES_RESPUESTA,
        "error": "", "hoja": None, "v": {}, "menor": False,
    }
    base.update(extra)
    return base


@router.get("/reclamaciones", response_class=HTMLResponse)
async def formulario(request: Request):
    return _plantillas(request).TemplateResponse(
        "reclamaciones.html", _contexto(request, await usuario_actual(request)))


@router.post("/reclamaciones", response_class=HTMLResponse)
async def registrar(
    request: Request,
    tipo: str = Form(...),
    nombre: str = Form(...),
    documento_tipo: str = Form("DNI"),
    documento_numero: str = Form(...),
    email: str = Form(...),
    telefono: str = Form(""),
    direccion: str = Form(""),
    es_menor_edad: str = Form(""),
    apoderado: str = Form(""),
    bien_contratado: str = Form("servicio"),
    descripcion_bien: str = Form(""),
    monto_reclamado: str = Form(""),
    detalle: str = Form(...),
    pedido: str = Form(...),
):
    """Registra la hoja y devuelve su numero. La hoja NO se borra nunca."""
    usuario = await usuario_actual(request)
    datos = {
        "tipo": (tipo or "").strip().lower(),
        "nombre": (nombre or "").strip(),
        "documento_tipo": (documento_tipo or "DNI").strip().upper(),
        "documento_numero": (documento_numero or "").strip(),
        "email": (email or "").strip().lower(),
        "telefono": (telefono or "").strip(),
        "direccion": (direccion or "").strip(),
        "apoderado": (apoderado or "").strip(),
        "bien_contratado": (bien_contratado or "servicio").strip(),
        "descripcion_bien": (descripcion_bien or "").strip(),
        "detalle": (detalle or "").strip(),
        "pedido": (pedido or "").strip(),
    }
    menor = bool(es_menor_edad)

    def con_error(msg: str):
        return _plantillas(request).TemplateResponse(
            "reclamaciones.html",
            _contexto(request, usuario, error=msg, v=datos, menor=menor),
            status_code=400)

    if datos["tipo"] not in TIPOS:
        return con_error("Indica si es un reclamo o una queja.")
    if len(datos["nombre"]) < 3:
        return con_error("Falta tu nombre completo.")
    if not datos["documento_numero"]:
        return con_error("Falta el número de tu documento.")
    if "@" not in datos["email"] or "." not in datos["email"].split("@")[-1]:
        return con_error("Ese correo no parece válido, y es donde te llega la copia.")
    if len(datos["detalle"]) < 10:
        return con_error("Cuéntanos qué pasó con algo más de detalle.")
    if len(datos["pedido"]) < 5:
        return con_error("Dinos qué esperas que hagamos.")
    # Un menor reclama a traves de su apoderado: sin ese dato la hoja queda
    # incompleta para la propia norma.
    if menor and len(datos["apoderado"]) < 3:
        return con_error("Si eres menor de edad, indica el nombre de tu apoderado.")

    monto = None
    if (monto_reclamado or "").strip():
        try:
            monto = round(float(monto_reclamado.replace(",", ".")), 2)
        except ValueError:
            return con_error("El monto reclamado tiene que ser un número.")

    async with connection() as conn:
        fila = await conn.fetchrow(
            """INSERT INTO reclamaciones
                 (tipo, nombre, documento_tipo, documento_numero, email, telefono,
                  direccion, es_menor_edad, apoderado, bien_contratado,
                  descripcion_bien, monto_reclamado, detalle, pedido,
                  limite_respuesta, usuario_id, ip_solicitud)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
               RETURNING numero, limite_respuesta""",
            datos["tipo"], datos["nombre"], datos["documento_tipo"],
            datos["documento_numero"], datos["email"], datos["telefono"] or None,
            datos["direccion"] or None, menor, datos["apoderado"] or None,
            datos["bien_contratado"], datos["descripcion_bien"] or None, monto,
            datos["detalle"], datos["pedido"],
            limite_respuesta(datetime.now()), usuario["id"] if usuario else None,
            request.client.host if request.client else None,
        )

    codigo = _codigo(fila["numero"])
    log.info("Libro de Reclamaciones: %s registrado (%s)", codigo, datos["tipo"])
    await _enviar_copias(codigo, datos, fila["limite_respuesta"])

    return _plantillas(request).TemplateResponse(
        "reclamaciones.html",
        _contexto(request, usuario, hoja={
            "codigo": codigo, "limite": fila["limite_respuesta"],
            "email": datos["email"], "tipo": datos["tipo"]}))


async def _enviar_copias(codigo: str, datos: dict, limite) -> None:
    """La copia al consumidor y el aviso interno. Ninguna puede tumbar el alta.

    Se hace DESPUES de guardar y con las excepciones capturadas a proposito: si
    el correo falla -- y ahora mismo falla --, la hoja ya existe y su numero ya
    se ensena en pantalla. Al reves, un fallo de SMTP haria perder el reclamo,
    que es justo lo que la ley quiere evitar.
    """
    # El import va DENTRO del try, y no es tiquismiquis: si el modulo de correo
    # no se puede ni importar -- falta una dependencia, se rompe una version --
    # un import al descubierto tumbaria la funcion entera. Y esta funcion corre
    # DESPUES de guardar la hoja, con el unico proposito de avisar. Nada de lo
    # que pase aqui puede convertir un reclamo registrado en un error.
    try:
        from shared.email_sender import enviar_email
    except Exception as e:
        log.error("Modulo de correo no disponible; %s queda registrado sin "
                  "copia enviada: %s", codigo, e)
        return

    cuerpo = f"""
    <p>Registramos tu {datos['tipo']} en nuestro Libro de Reclamaciones.</p>
    <p><b>Número de hoja: {codigo}</b><br>
       Guárdalo: es el que necesitas si acudes a INDECOPI.</p>
    <p>Tenemos hasta el <b>{limite.strftime('%d/%m/%Y')}</b> para responderte,
       y lo haremos a esta misma dirección.</p>
    <p style="color:#666;font-size:13px">Lo que nos contaste:<br>
       {datos['detalle'][:800]}</p>
    """
    try:
        await enviar_email(datos["email"],
                           f"Tu {datos['tipo']} {codigo} · LicitaPro", cuerpo)
    except Exception as e:
        log.error("No se pudo enviar la copia de %s a %s: %s", codigo,
                  datos["email"], e)

    interno = (os.getenv("LICITAPRO_CONTACTO_EMAIL")
               or os.getenv("LICITAPRO_ADMIN_EMAIL") or "").strip()
    if not interno:
        return
    try:
        await enviar_email(
            interno, f"[{codigo}] {datos['tipo']} nuevo en el Libro",
            f"<p><b>{codigo}</b> · {datos['tipo']} de {datos['nombre']} "
            f"({datos['documento_tipo']} {datos['documento_numero']})</p>"
            f"<p>Contacto: {datos['email']} {datos['telefono']}</p>"
            f"<p><b>Qué pasó:</b><br>{datos['detalle']}</p>"
            f"<p><b>Qué pide:</b><br>{datos['pedido']}</p>"
            f"<p>Plazo para responder: {limite.strftime('%d/%m/%Y')}</p>")
    except Exception as e:
        log.error("No se pudo avisar internamente de %s: %s", codigo, e)
