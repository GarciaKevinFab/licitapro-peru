"""API interna que llaman los flujos de n8n.

ATENCION ANTES DE LEVANTARLA: DUPLICA A LOS PLANIFICADORES

  Hay dos mecanismos que hacen lo mismo y no deben correr a la vez:

    - Los planificadores de APScheduler dentro de radar_bot y win_bot. Son los
      que corren en produccion y los que reparten avisos POR USUARIO, cobran
      las renovaciones y avisan de los pagos vencidos.
    - Estos endpoints, que llaman los cuatro flujos de n8n-workflows/. n8n solo
      esta en el compose de desarrollo, y ningun servicio arranca este modulo.

  Y no es solo duplicacion. `/api/daily-summary` usa `get_licitaciones_nuevas`,
  que se apoya en el flag GLOBAL `licitaciones.notificado`: la logica de un
  solo inquilino que se reemplazo porque quemaba cada licitacion para todos los
  clientes menos el primero. Activar n8n sin mas devolveria ese fallo.

  Si se quiere conservar el camino de n8n, hay que reescribir esos endpoints
  contra shared.notificaciones. Si no, sobra este modulo entero junto con
  n8n-workflows/.

POR QUE LLEVA UNA CLAVE

  Antes no tenia ninguna comprobacion de acceso, y entre sus endpoints esta
  `/api/contratos`, que devuelve los contratos activos de TODOS los inquilinos.
  El comentario del final invitaba a levantarla en 0.0.0.0:8100, o sea abierta
  a la red. Cualquiera que siguiera esa instruccion publicaba los contratos de
  su cartera entera sin saberlo.

  Ahora exige LICITAPRO_API_TOKEN en la cabecera X-API-Token, y sin esa
  variable configurada se niega a responder. Falla cerrado a proposito: una API
  interna sin token no es "una API sin proteger todavia", es una filtracion
  esperando a que alguien la arranque.
"""
import hmac
import os
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException
from shared.db import get_pool, get_licitaciones_nuevas, get_plazos_proximos, get_contratos_activos

app = FastAPI(title="LicitaPro API", version="1.0")


def exigir_token(x_api_token: str = Header(default="")) -> None:
    """Comprueba el token compartido. Sin variable configurada, no pasa nadie."""
    esperado = os.getenv("LICITAPRO_API_TOKEN")
    if not esperado:
        raise HTTPException(
            status_code=503,
            detail="Falta LICITAPRO_API_TOKEN: la API interna esta deshabilitada.")
    # compare_digest evita filtrar por tiempo cuanto coincide el token.
    if not hmac.compare_digest(x_api_token or "", esperado):
        raise HTTPException(status_code=401, detail="Token invalido")


@app.on_event("startup")
async def startup():
    await get_pool()


@app.post("/api/scrape", dependencies=[Depends(exigir_token)])
async def trigger_scrape(sources: str = "all"):
    """N8N llama esto cada 60 min."""
    from radar_bot.scrapers.orchestrator import run_all_scrapers
    results = await run_all_scrapers()
    return results


@app.get("/api/daily-summary", dependencies=[Depends(exigir_token)])
async def daily_summary():
    """Resumen diario para el workflow de 8 AM."""
    lics = await get_licitaciones_nuevas(limit=20)
    return {
        "date": str(date.today()),
        "nuevas": len(lics),
        "licitaciones": [dict(l) for l in lics],
    }


@app.post("/api/check-wins", dependencies=[Depends(exigir_token)])
async def check_wins():
    """N8N llama esto cada 30 min para verificar adjudicaciones."""
    from win_bot.monitor import verificar_adjudicaciones
    adjudicaciones = await verificar_adjudicaciones()
    return {"checked": True, "wins": len(adjudicaciones), "detalle": adjudicaciones}


@app.get("/api/check-deadlines", dependencies=[Depends(exigir_token)])
async def check_deadlines():
    """Alertas de plazos próximos a vencer."""
    plazos = await get_plazos_proximos(dias=7)
    return {"plazos_proximos": len(plazos), "detalle": [dict(p) for p in plazos]}


@app.get("/api/contratos", dependencies=[Depends(exigir_token)])
async def list_contratos():
    contratos = await get_contratos_activos()
    return {"activos": len(contratos), "contratos": [dict(c) for c in contratos]}


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "licitapro-api"}


# Ejecutar SOLO si se decidio conservar el camino de n8n, y nunca en 0.0.0.0:
#   uvicorn shared.api_server:app --host 127.0.0.1 --port 8100
# Detras de la red interna de compose o de un proxy. Escuchar en todas las
# interfaces publica los contratos de todos los inquilinos en el puerto 8100.
