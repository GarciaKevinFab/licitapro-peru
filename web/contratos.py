"""Post-adjudicacion: contratos, plazos y cobros.

Esto vivia en Telegram (/contratos, /plazos, /pagos, /ganar). Un timeline de
plazos y un estado de cuenta son tablas, y una tabla en un chat es ilegible.

Sin facturacion electronica SUNAT: aqui solo se registra lo que ya ocurrio
(factura emitida, pago recibido) para saber que falta cobrar. Emitir el
comprobante sigue siendo cosa del contador.

Igual que en propuestas, la propiedad se deriva por JOIN contra
empresas.usuario_id: nunca de un id que mande el cliente.
"""
import logging
import os
from datetime import date, timedelta
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from shared.db import connection
from shared.plazos_pago import (
    dias_de_mora,
    en_prorroga,
    enlace_consulta_mef,
    fecha_limite_pago,
)
from web.auth import usuario_actual
from shared import fechas

log = logging.getLogger("web.contratos")
router = APIRouter()

ESTADOS = ("adjudicado", "firmado", "en_ejecucion", "entregado", "pagado", "cancelado")


def _plantillas(request: Request):
    return request.app.state.templates


async def _contrato_del_usuario(contrato_id: int, usuario_id: int):
    async with connection() as conn:
        return await conn.fetchrow(
            """SELECT c.*, e.razon_social, l.objeto, l.entidad
                 FROM contratos c
                 JOIN empresas e ON e.id = c.empresa_id
                 LEFT JOIN licitaciones l ON l.id = c.licitacion_id
                WHERE c.id = $1 AND e.usuario_id = $2""",
            contrato_id, usuario_id)


@router.get("/contratos", response_class=HTMLResponse)
async def listar(request: Request, aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/contratos", status_code=303)

    async with connection() as conn:
        contratos = await conn.fetch(
            """SELECT c.*, e.razon_social, l.objeto, l.entidad,
                      (SELECT COUNT(*) FROM plazos pl
                        WHERE pl.contrato_id = c.id AND pl.completado = FALSE
                          AND pl.fecha_limite <= CURRENT_DATE + 7) AS plazos_urgentes,
                      COALESCE((SELECT SUM(monto) FROM pagos pg
                                 WHERE pg.contrato_id = c.id
                                   AND pg.estado <> 'pagado'), 0) AS por_cobrar
                 FROM contratos c
                 JOIN empresas e ON e.id = c.empresa_id
                 LEFT JOIN licitaciones l ON l.id = c.licitacion_id
                WHERE e.usuario_id = $1
                ORDER BY (c.estado <> 'pagado') DESC,
                         c.fecha_entrega_final ASC NULLS LAST""",
            usuario["id"])
        # Propuestas sin contrato: son las que se pueden marcar como ganadas.
        candidatas = await conn.fetch(
            """SELECT p.id, l.objeto, l.entidad, e.razon_social
                 FROM propuestas p
                 JOIN empresas e ON e.id = p.empresa_id
                 LEFT JOIN licitaciones l ON l.id = p.licitacion_id
                WHERE e.usuario_id = $1
                  AND NOT EXISTS (SELECT 1 FROM contratos c WHERE c.propuesta_id = p.id)
                ORDER BY p.created_at DESC LIMIT 40""",
            usuario["id"])

    return _plantillas(request).TemplateResponse("contratos.html", {
        "request": request, "usuario": usuario, "contratos": contratos,
        "candidatas": candidatas, "aviso": aviso, "error": error,
    })


@router.post("/contratos/ganar")
async def registrar_buena_pro(request: Request, propuesta_id: int = Form(...),
                              monto: float = Form(...), plazo_dias: int = Form(30)):
    """Registra la buena pro y arma el timeline de plazos del contrato."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    async with connection() as conn:
        prop = await conn.fetchrow(
            """SELECT p.id, p.licitacion_id, p.empresa_id
                 FROM propuestas p JOIN empresas e ON e.id = p.empresa_id
                WHERE p.id = $1 AND e.usuario_id = $2""",
            propuesta_id, usuario["id"])
        if not prop:
            return RedirectResponse("/contratos?error=Esa+propuesta+no+es+tuya",
                                    status_code=303)
        ya = await conn.fetchval(
            "SELECT id FROM contratos WHERE propuesta_id=$1", propuesta_id)
        if ya:
            return RedirectResponse(f"/contratos/{ya}", status_code=303)

        contrato_id = await conn.fetchval(
            """INSERT INTO contratos (propuesta_id, licitacion_id, empresa_id,
                   monto_adjudicado, fecha_adjudicacion, plazo_ejecucion_dias,
                   fecha_entrega_final, estado)
               VALUES ($1,$2,$3,$4,$5,$6,$7,'adjudicado') RETURNING id""",
            propuesta_id, prop["licitacion_id"], prop["empresa_id"], monto,
            fechas.hoy(), plazo_dias, fechas.hoy() + timedelta(days=plazo_dias))
        await conn.execute(
            "UPDATE propuestas SET estado='ganada', updated_at=NOW() WHERE id=$1",
            propuesta_id)

    try:
        from win_bot.timeline import crear_timeline_contrato
        await crear_timeline_contrato(contrato_id)
    except Exception as e:
        log.error("No se pudo crear el timeline de %s: %s", contrato_id, e, exc_info=True)

    return RedirectResponse(f"/contratos/{contrato_id}?aviso=Buena+pro+registrada",
                            status_code=303)


@router.get("/contratos/{contrato_id}", response_class=HTMLResponse)
async def detalle(request: Request, contrato_id: int, aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    contrato = await _contrato_del_usuario(contrato_id, usuario["id"])
    if not contrato:
        return RedirectResponse("/contratos?error=Ese+contrato+no+es+tuyo",
                                status_code=303)

    async with connection() as conn:
        plazos = await conn.fetch(
            "SELECT * FROM plazos WHERE contrato_id=$1 ORDER BY fecha_limite",
            contrato_id)
        pagos = await conn.fetch(
            """SELECT * FROM pagos WHERE contrato_id=$1
               ORDER BY fecha_factura NULLS LAST, id""", contrato_id)

    cobrado = sum(p["monto"] or 0 for p in pagos if p["estado"] == "pagado")
    pendiente = sum(p["monto"] or 0 for p in pagos if p["estado"] != "pagado")

    return _plantillas(request).TemplateResponse("contrato.html", {
        "request": request, "usuario": usuario, "c": contrato,
        "plazos": plazos,
        # La mora se calcula al leer: cambia sola cada dia habil, y guardarla
        # exigiria un proceso nocturno que puede fallar en silencio.
        "pagos": [
            {**dict(pg),
             "dias_mora": dias_de_mora(pg["fecha_limite_pago"]),
             "en_prorroga": en_prorroga(pg["fecha_limite_pago"])}
            for pg in pagos
        ],
        "consulta_mef": enlace_consulta_mef(),
        "estados": ESTADOS,
        "cobrado": cobrado, "pendiente": pendiente, "hoy": fechas.hoy(),
        "aviso": aviso, "error": error,
    })


@router.post("/contratos/{contrato_id}/estado")
async def cambiar_estado(request: Request, contrato_id: int, estado: str = Form(...)):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _contrato_del_usuario(contrato_id, usuario["id"]):
        return RedirectResponse("/contratos?error=Ese+contrato+no+es+tuyo", status_code=303)
    if estado not in ESTADOS:
        return RedirectResponse(f"/contratos/{contrato_id}?error=Estado+no+valido",
                                status_code=303)
    async with connection() as conn:
        await conn.execute(
            "UPDATE contratos SET estado=$2, updated_at=NOW() WHERE id=$1",
            contrato_id, estado)
    return RedirectResponse(f"/contratos/{contrato_id}?aviso=Estado+actualizado",
                            status_code=303)


@router.post("/contratos/{contrato_id}/plazo")
async def agregar_plazo(request: Request, contrato_id: int,
                        descripcion: str = Form(...), fecha_limite: str = Form(...),
                        tipo: str = Form("hito")):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _contrato_del_usuario(contrato_id, usuario["id"]):
        return RedirectResponse("/contratos?error=Ese+contrato+no+es+tuyo", status_code=303)
    try:
        limite = date.fromisoformat(fecha_limite)
    except ValueError:
        return RedirectResponse(f"/contratos/{contrato_id}?error=Fecha+no+valida",
                                status_code=303)
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO plazos (contrato_id, tipo, descripcion, fecha_limite)
               VALUES ($1,$2,$3,$4)""",
            contrato_id, tipo, descripcion.strip(), limite)
    return RedirectResponse(f"/contratos/{contrato_id}?aviso=Plazo+agregado",
                            status_code=303)


@router.post("/plazos/{plazo_id}/completar")
async def completar_plazo(request: Request, plazo_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    async with connection() as conn:
        # El contrato del plazo tiene que ser del usuario.
        contrato_id = await conn.fetchval(
            """SELECT pl.contrato_id FROM plazos pl
                 JOIN contratos c ON c.id = pl.contrato_id
                 JOIN empresas e ON e.id = c.empresa_id
                WHERE pl.id = $1 AND e.usuario_id = $2""",
            plazo_id, usuario["id"])
        if not contrato_id:
            return RedirectResponse("/contratos?error=Ese+plazo+no+es+tuyo",
                                    status_code=303)
        await conn.execute(
            """UPDATE plazos SET completado = NOT completado,
                   fecha_completado = CASE WHEN completado THEN NULL ELSE CURRENT_DATE END
               WHERE id=$1""", plazo_id)
    return RedirectResponse(f"/contratos/{contrato_id}", status_code=303)


@router.post("/contratos/{contrato_id}/pago")
async def registrar_cobro(request: Request, contrato_id: int,
                          concepto: str = Form(...), monto: float = Form(...),
                          numero_factura: str = Form(""), cobrado: str = Form(""),
                          fecha_conformidad: str = Form(""),
                          expediente_siaf: str = Form("")):
    """Registra una factura emitida o un cobro recibido.

    No emite comprobantes: la facturacion electronica quedo fuera de alcance a
    peticion del cliente. Aqui solo se anota lo que ya paso.

    La fecha limite sale del plazo LEGAL, no de un numero redondo. Esta ruta
    tenia el mismo "+30 dias" que payment_tracker, y por eso el fallo sobrevivio
    a la primera correccion: habia dos caminos y solo se arreglo uno. Ahora
    ambos llaman al mismo calculo.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _contrato_del_usuario(contrato_id, usuario["id"]):
        return RedirectResponse("/contratos?error=Ese+contrato+no+es+tuyo", status_code=303)

    conformidad = None
    if fecha_conformidad.strip():
        try:
            conformidad = date.fromisoformat(fecha_conformidad.strip())
        except ValueError:
            return RedirectResponse(
                f"/contratos/{contrato_id}?error=Fecha+de+conformidad+invalida",
                status_code=303)

    ya_cobrado = bool(cobrado)
    async with connection() as conn:
        categoria = await conn.fetchval(
            """SELECT l.categoria FROM contratos c
                 JOIN licitaciones l ON l.id = c.licitacion_id
                WHERE c.id = $1""",
            contrato_id)
        limite = fecha_limite_pago(conformidad, categoria)
        await conn.execute(
            """INSERT INTO pagos (contrato_id, concepto, monto, numero_factura,
                   fecha_factura, fecha_pago_real, fecha_conformidad,
                   fecha_limite_pago, fecha_pago_esperada, expediente_siaf, estado)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8,$9,$10)""",
            contrato_id, concepto.strip(), monto, numero_factura.strip() or None,
            fechas.hoy(), fechas.hoy() if ya_cobrado else None,
            conformidad, limite, expediente_siaf.strip() or None,
            "pagado" if ya_cobrado else "facturado")
    return RedirectResponse(f"/contratos/{contrato_id}?aviso=Registrado", status_code=303)


@router.post("/pagos/{pago_id}/cobrado")
async def marcar_cobrado(request: Request, pago_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    async with connection() as conn:
        contrato_id = await conn.fetchval(
            """SELECT pg.contrato_id FROM pagos pg
                 JOIN contratos c ON c.id = pg.contrato_id
                 JOIN empresas e ON e.id = c.empresa_id
                WHERE pg.id = $1 AND e.usuario_id = $2""",
            pago_id, usuario["id"])
        if not contrato_id:
            return RedirectResponse("/contratos?error=Ese+pago+no+es+tuyo",
                                    status_code=303)
        await conn.execute(
            """UPDATE pagos SET estado='pagado', fecha_pago_real=CURRENT_DATE
               WHERE id=$1""", pago_id)
    return RedirectResponse(f"/contratos/{contrato_id}?aviso=Cobro+registrado",
                            status_code=303)


# ─── Documentos del contrato ─────────────────────────────
#
# Los generadores vivian SOLO en Telegram (`/factura`, `/conformidad`). El
# producto se mudo al panel hace tiempo y estos dos se quedaron atras: quien no
# usara Telegram no tenia forma de sacarlos. Se generan al vuelo y no se
# guardan, porque son un reflejo de filas que ya estan en la base -- guardarlos
# obligaria a regenerarlos en cada cambio, o a repartir copias desactualizadas.

@router.get("/contratos/{contrato_id}/pagos/{pago_id}/proforma")
async def descargar_proforma(request: Request, contrato_id: int, pago_id: int):
    """Proforma de cobro de un pago ya registrado.

    NO es un comprobante de pago. La facturacion electronica quedo fuera de
    alcance, asi que este documento sirve para acompanar el tramite, no para
    sustituir a la factura que hay que emitir ante SUNAT. El propio DOCX lo
    dice en la portada.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _contrato_del_usuario(contrato_id, usuario["id"]):
        return RedirectResponse("/contratos?error=Ese+contrato+no+es+tuyo",
                                status_code=303)

    async with connection() as conn:
        # El contrato_id va en el WHERE ademas del pago: los dos llegan por la
        # URL y ya se comprobo que el contrato es suyo. Sin esto, un pago de
        # otro contrato se colaria cambiando un numero.
        pago = await conn.fetchrow(
            "SELECT * FROM pagos WHERE id=$1 AND contrato_id=$2",
            pago_id, contrato_id)
    if not pago:
        return RedirectResponse(f"/contratos/{contrato_id}?error=Ese+pago+no+existe",
                                status_code=303)

    try:
        from win_bot.invoice_gen import generar_factura
        ruta = await generar_factura(contrato_id, float(pago["monto"]),
                                     pago["concepto"] or "Servicio prestado",
                                     pago["numero_factura"])
    except Exception as e:
        log.error("Proforma del pago %s fallo: %s", pago_id, e, exc_info=True)
        ruta = None

    if not ruta or not os.path.isfile(ruta):
        return RedirectResponse(
            f"/contratos/{contrato_id}?error={quote_plus('No se pudo generar la proforma')}",
            status_code=303)
    return FileResponse(ruta, filename=os.path.basename(ruta),
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".wordprocessingml.document")


@router.get("/contratos/{contrato_id}/conformidad")
async def descargar_conformidad(request: Request, contrato_id: int,
                                observaciones: str = ""):
    """Acta de conformidad del bien o servicio entregado.

    Importa mas de lo que parece: el plazo legal de pago se cuenta en dias
    habiles DESDE la conformidad, no desde la factura. Tener el acta a mano es
    lo que permite reclamar con una fecha concreta encima de la mesa.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _contrato_del_usuario(contrato_id, usuario["id"]):
        return RedirectResponse("/contratos?error=Ese+contrato+no+es+tuyo",
                                status_code=303)

    try:
        from win_bot.conformity_gen import generar_acta_conformidad
        ruta = await generar_acta_conformidad(contrato_id, observaciones.strip())
    except Exception as e:
        log.error("Acta de conformidad de %s fallo: %s", contrato_id, e,
                  exc_info=True)
        ruta = None

    if not ruta or not os.path.isfile(ruta):
        return RedirectResponse(
            f"/contratos/{contrato_id}?error={quote_plus('No se pudo generar el acta')}",
            status_code=303)
    return FileResponse(ruta, filename=os.path.basename(ruta),
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".wordprocessingml.document")
