"""Crea usuarios y empresas de prueba para desarrollo.

Reemplaza a las empresas sembradas en shared/schema.sql. Una migracion no debe
traer datos de una empresa concreta: el esquema es del producto, los datos son
del despliegue.

Uso:
    PYTHONPATH=. python tools/datos_dev.py
    PYTHONPATH=. python tools/datos_dev.py --verificar-aislamiento
"""
import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

from shared.db import connection

CUENTAS = [
    {
        "email": "demo.uno@licitapro.local",
        "nombre": "Demo Uno",
        "telegram_chat_id": None,
        "regiones": ["Madre de Dios", "Cusco"],
        "keywords": ["software", "servidores", "cableado estructurado"],
        "empresas": [("DEMO UNO S.A.C.", "20100000001", ["tecnología", "redes"])],
    },
    {
        "email": "demo.dos@licitapro.local",
        "nombre": "Demo Dos",
        "telegram_chat_id": None,
        "regiones": ["Lima", "Arequipa"],
        "keywords": ["consultoría", "capacitación"],
        "empresas": [("DEMO DOS E.I.R.L.", "20100000002", ["consultoría"])],
    },
]


async def sembrar():
    async with connection() as conn:
        for c in CUENTAS:
            uid = await conn.fetchval(
                """INSERT INTO usuarios (email, nombre, telegram_chat_id, plan)
                   VALUES ($1, $2, $3, 'trial')
                   ON CONFLICT (email) DO UPDATE SET nombre = EXCLUDED.nombre
                   RETURNING id""",
                c["email"], c["nombre"], c["telegram_chat_id"],
            )
            for razon, ruc, rubros in c["empresas"]:
                await conn.execute(
                    """INSERT INTO empresas (razon_social, ruc, rubros, usuario_id, activa)
                       VALUES ($1, $2, $3, $4, TRUE)
                       ON CONFLICT (ruc) DO UPDATE SET usuario_id = EXCLUDED.usuario_id""",
                    razon, ruc, rubros, uid,
                )
            await conn.execute(
                """INSERT INTO user_config (user_id, usuario_id, regiones, keywords)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (usuario_id) DO UPDATE
                       SET regiones = EXCLUDED.regiones, keywords = EXCLUDED.keywords""",
                -uid, uid, c["regiones"], c["keywords"],
            )
            print(f"  usuario {uid}: {c['email']}")


async def verificar_aislamiento():
    """Comprueba que ninguna cuenta ve datos de otra."""
    from shared.db import empresa_es_de, empresas_de, get_config_usuario

    async with connection() as conn:
        ids = [r["id"] for r in await conn.fetch(
            "SELECT id FROM usuarios ORDER BY id")]

    print(f"\ncuentas en la BD: {ids}")
    fallos = 0
    todas = {}
    for uid in ids:
        emps = await empresas_de(uid)
        todas[uid] = {e["id"] for e in emps}
        cfg = await get_config_usuario(uid)
        regs = list(cfg["regiones"]) if cfg and cfg["regiones"] else []
        print(f"  usuario {uid}: {len(emps)} empresas, regiones={regs}")

    for uid, propias in todas.items():
        for otro, ajenas in todas.items():
            if uid == otro:
                continue
            cruce = propias & ajenas
            if cruce:
                print(f"  FUGA: usuario {uid} y {otro} comparten empresas {cruce}")
                fallos += 1
            for eid in ajenas:
                if await empresa_es_de(eid, uid):
                    print(f"  FUGA: usuario {uid} puede acceder a la empresa {eid} de {otro}")
                    fallos += 1

    print("\nAISLAMIENTO OK: ninguna cuenta ve datos de otra" if not fallos
          else f"\n{fallos} FUGAS DETECTADAS")
    return fallos


async def main():
    if "--verificar-aislamiento" in sys.argv:
        sys.exit(1 if await verificar_aislamiento() else 0)
    print("sembrando cuentas de desarrollo:")
    await sembrar()
    await verificar_aislamiento()


asyncio.run(main())
