"""FastAPI server interno — N8N llama estos endpoints para ejecutar tareas."""
from datetime import date
from fastapi import FastAPI
from shared.db import get_pool, get_licitaciones_nuevas, get_plazos_proximos, get_contratos_activos

app = FastAPI(title="LicitaPro API", version="1.0")


@app.on_event("startup")
async def startup():
    await get_pool()


@app.post("/api/scrape")
async def trigger_scrape(sources: str = "all"):
    """N8N llama esto cada 60 min."""
    from radar_bot.scrapers.orchestrator import run_all_scrapers
    results = await run_all_scrapers()
    return results


@app.get("/api/daily-summary")
async def daily_summary():
    """Resumen diario para el workflow de 8 AM."""
    lics = await get_licitaciones_nuevas(limit=20)
    return {
        "date": str(date.today()),
        "nuevas": len(lics),
        "licitaciones": [dict(l) for l in lics],
    }


@app.post("/api/check-wins")
async def check_wins():
    """N8N llama esto cada 30 min para verificar adjudicaciones."""
    from win_bot.monitor import verificar_adjudicaciones
    adjudicaciones = await verificar_adjudicaciones()
    return {"checked": True, "wins": len(adjudicaciones), "detalle": adjudicaciones}


@app.get("/api/check-deadlines")
async def check_deadlines():
    """Alertas de plazos próximos a vencer."""
    plazos = await get_plazos_proximos(dias=7)
    return {"plazos_proximos": len(plazos), "detalle": [dict(p) for p in plazos]}


@app.get("/api/contratos")
async def list_contratos():
    contratos = await get_contratos_activos()
    return {"activos": len(contratos), "contratos": [dict(c) for c in contratos]}


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "licitapro-api"}


# Ejecutar: uvicorn shared.api_server:app --host 0.0.0.0 --port 8100
