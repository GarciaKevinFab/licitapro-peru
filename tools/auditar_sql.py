"""Audita cada literal SQL del proyecto preparandolo contra la BD real.

No adivina: le pasa cada consulta a Postgres via PREPARE. Si la columna o la
tabla no existe, el motor lo dice. Es la unica forma de estar seguro.
"""
import ast
import asyncio
import os
import pathlib
import re
import sys

RAIZ = pathlib.Path(os.getenv('AUDIT_ROOT', '.'))
# La palabra clave SOLA no es una consulta: hace falta algo detras.
#
# Sin ese `\s+\S`, "DELETE" a secas cuenta como SQL, y ese literal aparece en
# dos sitios donde es un VERBO HTTP y no una consulta:
#
#   shared/culqi.py   _peticion("DELETE", f"{RUTA_SUSCRIPCIONES}/{sxn}")
#   web/limites.py    ("DELETE", None, 120, 60)  -- una regla de limite
#
# Los dos daban "PostgresSyntaxError: syntax error at end of input", que es
# justo lo que responde Postgres a un DELETE sin FROM: el auditor tenia razon
# en que eso no es SQL valido, pero es que no era SQL. El primero dejo la rama
# `main` en rojo al fusionar el PR #18.
#
# Una consulta de verdad siempre tiene mas palabras, asi que exigir un token
# mas no deja pasar nada que antes se detectara.
SQL_INICIO = re.compile(r'^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b\s+\S', re.IGNORECASE)


def _es_ajeno(py: pathlib.Path) -> bool:
    """Si el archivo NO es del proyecto y por tanto no se audita.

    POR QUE NO BASTA CON LISTAR 'venv' Y '.venv'

      El puente se instala en `.venv-tarea/`, dentro del propio repositorio y
      tal como manda DESPLIEGUE.md. Ese nombre no estaba en la lista, asi que
      el auditor se ponia a preparar contra Postgres los docstrings de pytest
      y de anyio: 50 fallos inventados que tapaban los de verdad.

      En el CI no se ve, porque alli no existe ese directorio. Solo se rompe en
      la maquina de quien monto el puente -- es decir, en la unica maquina
      donde el puente corre.

      Se filtra por site-packages y por cualquier carpeta que empiece por
      "venv" o ".venv", que cubre tambien el siguiente entorno que alguien
      cree con otro sufijo.
    """
    for parte in py.parts:
        if parte in ('__pycache__', 'site-packages', 'graphify-out'):
            return True
        if parte.startswith(('venv', '.venv')):
            return True
    return False


def literales_sql():
    for py in sorted(RAIZ.rglob('*.py')):
        if _es_ajeno(py):
            continue
        try:
            arbol = ast.parse(py.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        # Los docstrings no son SQL aunque empiecen con "Insert...", y los
        # trozos constantes de un f-string reaparecen como ast.Constant al
        # recorrer el arbol. Sin excluirlos, el auditor grita lobo en cada build.
        ignorar = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                cuerpo = getattr(nodo, 'body', None)
                if cuerpo and isinstance(cuerpo[0], ast.Expr) and isinstance(cuerpo[0].value, ast.Constant):
                    ignorar.add(id(cuerpo[0].value))
            elif isinstance(nodo, ast.JoinedStr):
                for v in nodo.values:
                    if isinstance(v, ast.Constant):
                        ignorar.add(id(v))

        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                if id(nodo) not in ignorar and SQL_INICIO.match(nodo.value):
                    yield py, nodo.lineno, nodo.value, False
            elif isinstance(nodo, ast.JoinedStr):
                texto = ''.join(v.value for v in nodo.values if isinstance(v, ast.Constant))
                if SQL_INICIO.match(texto):
                    yield py, nodo.lineno, texto, True


async def main():
    import asyncpg
    conn = await asyncpg.connect(
        host=os.getenv('POSTGRES_HOST', 'localhost'),
        port=int(os.getenv('POSTGRES_PORT', '5433')),
        database=os.getenv('POSTGRES_DB', 'licitapro'),
        user=os.getenv('POSTGRES_USER', 'licitapro'),
        password=os.getenv('POSTGRES_PASSWORD', 'licitapro'),
    )
    fallos, dinamicos, ok = [], [], 0
    for archivo, linea, sql, es_fstring in literales_sql():
        ref = f"{archivo.as_posix()}:{linea}"
        if es_fstring:
            dinamicos.append((ref, sql))
            continue
        try:
            await conn.prepare(sql)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fallos.append((ref, sql, f"{type(e).__name__}: {e}"))
    await conn.close()

    print(f"consultas validadas OK : {ok}")
    print(f"consultas que FALLAN   : {len(fallos)}")
    print(f"SQL dinamico (f-string): {len(dinamicos)}")
    print()
    for ref, sql, err in fallos:
        print('=' * 76)
        print(f"FALLA  {ref}")
        print(f"  {err.splitlines()[0][:150]}")
        print('  ' + ' '.join(sql.split())[:140])
    if dinamicos:
        print()
        print('=' * 76)
        print("SQL CON f-string (no verificable por PREPARE, revisar a mano):")
        for ref, sql in dinamicos:
            print(f"  {ref}: {' '.join(sql.split())[:96]}")

    # Los f-string no se pueden validar por PREPARE: se listan, no cuentan
    # como fallo. Los fallos reales si rompen el build.
    return 1 if fallos else 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
