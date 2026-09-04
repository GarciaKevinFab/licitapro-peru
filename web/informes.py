"""Con quien ganas, cuanto te deben, y que vence pronto.

QUE FALTABA

  El panel enseña lo que viene y las propuestas abiertas. No enseñaba nada de
  lo que ya paso: con que entidades se gana, cuanto se adjudica por mes, cuanto
  queda por cobrar. Esos datos estaban en la base -- 313 adjudicaciones -- y no
  los leia ninguna consulta del producto.

  Importa por dos motivos distintos. Al cliente le sirve para decidir donde
  insistir. Y al negocio le sirve como argumento de renovacion: un proveedor
  que ve aqui su propio historico no se da de baja igual de facil que uno que
  solo ve una lista de licitaciones que tambien puede mirar gratis.

POR QUE LOS VENCIMIENTOS VIVEN AQUI

  Son de la misma naturaleza: cosas que hay que mirar de vez en cuando y que no
  caben en el trabajo diario. Ponerlos solo en la ficha de empresa los
  escondería en un formulario que se abre una vez al ano.

TODO FILTRA POR usuario_id

  Cada consulta hace JOIN contra `empresas` y compara `usuario_id`. No se
  confia en que la sesion ya lo hiciera: son consultas agregadas, y en una
  agregacion un filtro que falta no da error -- da los numeros de todos.
"""
import csv
import io
import logging
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from shared.db import connection
from web.auth import usuario_actual
from shared import fechas

log = logging.getLogger("web.informes")
router = APIRouter()

# Cuanto antes se avisa de algo que vence. Dos meses dan tiempo a renovar un
# RNP -- el tramite no es inmediato -- sin llenar la pantalla de avisos
# lejanos que se aprende a ignorar.
DIAS_AVISO_VENCIMIENTO = 60


def _plantillas(request: Request):
    return request.app.state.templates


# ─── Vencimientos ────────────────────────────────────────

async def proximos_vencimientos(usuario_id: int,
                                dias: int = DIAS_AVISO_VENCIMIENTO) -> list[dict]:
    """Lo que vence pronto o ya vencio, de todas sus empresas.

    Une dos origenes a proposito:

      - `empresas.rnp_vigencia`, atributo de la empresa exigido por ley y que
        el analisis de viabilidad consulta como tal.
      - `vencimientos`, la tabla de anotaciones libres para lo demas.

    Se unen al leer y no en el modelo porque son cosas distintas: el RNP
    vencido inhabilita para contratar; una poliza vencida es un problema del
    contrato. Lo que comparten es cuando hay que mirarlas.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            """
            SELECT e.razon_social, 'RNP' AS tipo,
                   COALESCE(e.rnp_categoria, 'Registro Nacional de Proveedores')
                       AS descripcion,
                   e.rnp_vigencia AS fecha, TRUE AS inhabilita
              FROM empresas e
             WHERE e.usuario_id = $1 AND e.activa
               AND e.rnp_vigencia IS NOT NULL
               AND e.rnp_vigencia <= CURRENT_DATE + make_interval(days => $2)

            UNION ALL

            SELECT e.razon_social, v.tipo, v.descripcion,
                   v.fecha_vencimiento AS fecha, FALSE AS inhabilita
              FROM vencimientos v
              JOIN empresas e ON e.id = v.empresa_id
             WHERE e.usuario_id = $1
               AND v.fecha_vencimiento <= CURRENT_DATE + make_interval(days => $2)

            ORDER BY fecha
            """, usuario_id, dias)

    hoy = fechas.hoy()
    return [{**dict(f), "dias": (f["fecha"] - hoy).days} for f in filas]


# ─── Informes ────────────────────────────────────────────

@router.get("/informes", response_class=HTMLResponse)
async def ver_informes(request: Request):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/informes", status_code=303)
    uid = usuario["id"]

    async with connection() as conn:
        resumen = await conn.fetchrow(
            """SELECT
                 (SELECT COUNT(*) FROM propuestas p JOIN empresas e ON e.id=p.empresa_id
                   WHERE e.usuario_id=$1)                                AS propuestas,
                 (SELECT COUNT(*) FROM contratos c JOIN empresas e ON e.id=c.empresa_id
                   WHERE e.usuario_id=$1)                                AS ganados,
                 (SELECT COALESCE(SUM(c.monto_adjudicado),0) FROM contratos c
                   JOIN empresas e ON e.id=c.empresa_id WHERE e.usuario_id=$1)
                                                                          AS adjudicado,
                 (SELECT COALESCE(SUM(pg.monto),0) FROM pagos pg
                   JOIN contratos c ON c.id=pg.contrato_id
                   JOIN empresas e ON e.id=c.empresa_id
                   WHERE e.usuario_id=$1 AND pg.estado <> 'pagado')       AS por_cobrar
            """, uid)

        # Con que entidades se gana. Es la pregunta que mas se repite: donde
        # vale la pena volver a presentarse.
        por_entidad = await conn.fetch(
            """SELECT l.entidad, COUNT(*) AS ganados,
                      COALESCE(SUM(c.monto_adjudicado),0) AS monto
                 FROM contratos c
                 JOIN empresas e ON e.id = c.empresa_id
                 LEFT JOIN licitaciones l ON l.id = c.licitacion_id
                WHERE e.usuario_id = $1 AND l.entidad IS NOT NULL
                GROUP BY l.entidad ORDER BY monto DESC LIMIT 12""", uid)

        por_mes = await conn.fetch(
            """SELECT date_trunc('month', c.fecha_adjudicacion)::date AS mes,
                      COUNT(*) AS ganados,
                      COALESCE(SUM(c.monto_adjudicado),0) AS monto
                 FROM contratos c
                 JOIN empresas e ON e.id = c.empresa_id
                WHERE e.usuario_id = $1 AND c.fecha_adjudicacion IS NOT NULL
                GROUP BY 1 ORDER BY 1 DESC LIMIT 12""", uid)

        # Propuestas por estado. Es el numero que dice si el filtro esta bien
        # puesto o se esta compitiendo donde no toca.
        por_estado = await conn.fetch(
            """SELECT p.estado, COUNT(*) AS n
                 FROM propuestas p JOIN empresas e ON e.id = p.empresa_id
                WHERE e.usuario_id = $1
                GROUP BY p.estado ORDER BY n DESC""", uid)

    return _plantillas(request).TemplateResponse("informes.html", {
        "request": request, "usuario": usuario,
        "r": resumen, "por_entidad": por_entidad, "por_mes": por_mes,
        "por_estado": por_estado,
        "vencimientos": await proximos_vencimientos(uid),
    })


# ─── Exportacion ─────────────────────────────────────────

def _csv(filas, cabeceras: list[str], nombre: str) -> StreamingResponse:
    """CSV que Excel en español abre sin romperse.

    Dos detalles que parecen manias y no lo son:

      - El BOM. Sin el, Excel lee el archivo como ANSI y toda tilde sale mal:
        "LICITACIÓN" aparece como "LICITACIÃ“N" en cada fila.
      - El punto y coma. Con la configuracion regional de Peru o España, Excel
        espera ';' como separador; con comas mete la fila entera en la primera
        columna, que es justo cuando el usuario decide que la exportacion no
        funciona.
    """
    buffer = io.StringIO()
    buffer.write("﻿")
    escritor = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    escritor.writerow(cabeceras)
    for fila in filas:
        escritor.writerow(list(fila))

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@router.get("/informes/licitaciones.csv")
async def exportar_licitaciones(request: Request):
    """Las licitaciones que encajan con sus filtros, como las ve en el panel."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    from shared.db import licitaciones_para_usuario
    lics = await licitaciones_para_usuario(usuario["id"], limite=2000)

    filas = ((l["id"], l.get("nomenclatura") or "", l["entidad"], l["objeto"],
              l.get("tipo") or "", l.get("departamento") or "",
              l.get("monto_referencial") or "",
              l["fecha_cierre"].strftime("%d/%m/%Y") if l.get("fecha_cierre") else "",
              round(l["score_viabilidad"]) if l.get("score_viabilidad") else "",
              l.get("url") or "")
             for l in lics)

    return _csv(filas,
                ["ID", "Nomenclatura", "Entidad", "Objeto", "Tipo",
                 "Departamento", "Monto referencial", "Cierre", "Puntaje", "URL"],
                f"licitaciones_{fechas.hoy():%Y-%m-%d}.csv")


@router.get("/informes/cobros.csv")
async def exportar_cobros(request: Request):
    """Lo facturado y lo cobrado. Es lo que pide un contador."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT e.razon_social, c.numero_contrato, l.entidad,
                      pg.concepto, pg.monto, pg.numero_factura,
                      pg.fecha_factura, pg.fecha_conformidad,
                      pg.fecha_limite_pago, pg.fecha_pago_real, pg.estado,
                      pg.expediente_siaf
                 FROM pagos pg
                 JOIN contratos c ON c.id = pg.contrato_id
                 JOIN empresas e  ON e.id = c.empresa_id
                 LEFT JOIN licitaciones l ON l.id = c.licitacion_id
                WHERE e.usuario_id = $1
                ORDER BY pg.fecha_limite_pago NULLS LAST, pg.id""",
            usuario["id"])

    def texto(v):
        if hasattr(v, "strftime"):
            return v.strftime("%d/%m/%Y")
        return v if v is not None else ""

    return _csv(([texto(v) for v in f] for f in filas),
                ["Empresa", "Contrato", "Entidad", "Concepto", "Monto",
                 "N.º factura", "Fecha factura", "Conformidad",
                 "Límite legal de pago", "Cobrado el", "Estado",
                 "Expediente SIAF"],
                f"cobros_{fechas.hoy():%Y-%m-%d}.csv")
