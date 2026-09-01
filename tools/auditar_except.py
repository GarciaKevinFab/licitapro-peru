"""Encuentra los except que se tragan el error sin dejar rastro."""
import ast
import os
import pathlib
import sys

RAIZ = pathlib.Path(os.getenv('AUDIT_ROOT', '.'))

def registra(nodo):
    """True si el cuerpo del except loguea o relanza."""
    for n in ast.walk(nodo):
        if isinstance(n, ast.Raise):
            return True
        if isinstance(n, ast.Call):
            f = n.func
            nombre = getattr(f, 'attr', None) or getattr(f, 'id', None)
            if nombre in ('error', 'warning', 'exception', 'critical', 'warn', 'print'):
                return True
    return False

mudos = []
for py in sorted(RAIZ.rglob('*.py')):
    if any(p in py.parts for p in ('__pycache__', 'venv', '.venv', 'graphify-out')):
        continue
    try:
        arbol = ast.parse(py.read_text(encoding='utf-8'))
    except SyntaxError:
        continue
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ExceptHandler) and not registra(nodo):
            cuerpo = nodo.body
            solo_pass = len(cuerpo) == 1 and isinstance(cuerpo[0], ast.Pass)
            tipo = 'except: pass' if solo_pass else 'sin log ni raise'
            amplio = nodo.type is None or getattr(nodo.type, 'id', '') == 'Exception'
            mudos.append((f"{py.as_posix()}:{nodo.lineno}", tipo, amplio))

anchos = [m for m in mudos if m[2]]
print(f"except que no dejan rastro: {len(mudos)}  (de ellos, amplios/Exception: {len(anchos)})")
print()
print("AMPLIOS Y MUDOS -- son los que esconden bugs de esquema:")
for ref, tipo, _ in anchos:
    print(f"  {ref:44s} {tipo}")

# Informativo: no rompe el build. Muchos except amplios en scrapers son
# resiliencia deliberada por fila.
sys.exit(0)
