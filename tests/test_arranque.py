"""Que los puntos de entrada no usen nombres que no existen.

EL FALLO QUE ESTO ATRAPA, Y POR QUE NO LO VIO NADIE

  `win_bot/main.py` llamaba a `generar_factura`, `generar_acta_conformidad`,
  `generar_informe_entrega` y `registrar_pago` sin importar ninguna de las
  cuatro. Los comandos `/factura`, `/conformidad`, `/entrega` y `/pago` estaban
  rotos, y aun asi:

    - El bot arrancaba sin quejarse. Python resuelve los nombres al ejecutar la
      linea, no al importar el modulo, asi que hasta que alguien no escribia el
      comando no pasaba nada.
    - El fallo era mudo. python-telegram-bot captura la excepcion del manejador
      y la registra; el cliente ve que su mensaje se queda sin respuesta y
      supone que el bot va lento.
    - Ninguna prueba lo cogia, porque probar un comando de Telegram exige
      montar la aplicacion entera.

  Un import ausente en una funcion que se ejecuta poco es de los fallos que mas
  duran: no hay sintoma hasta que hay un usuario delante.

POR QUE SE MIRA EL AST Y NO SE IMPORTA Y YA

  Importar el modulo NO habria detectado nada: `import win_bot.main` funciona
  perfectamente con los cuatro nombres ausentes. Hay que mirar el codigo, no
  ejecutarlo.

  No sustituye a un linter, que haria esto y mas. Mientras no haya uno en el
  CI, esta prueba cubre el caso concreto que ya costo cuatro comandos.
"""
import ast
import builtins
import pathlib

import pytest

# Los cuatro procesos que se arrancan de verdad, mas los routers del panel: ahi
# es donde un nombre sin resolver acaba delante de un usuario.
ENTRADAS = [
    "radar_bot/main.py",
    "prep_bot/main.py",
    "win_bot/main.py",
    "web/app.py",
    "web/auth.py",
    "web/propuestas.py",
    "web/contratos.py",
    "web/empresas.py",
    "web/configuracion.py",
    "web/suscripcion.py",
    "web/webhooks_whatsapp.py",
]

# Existen en tiempo de ejecucion pero no aparecen como asignaciones en el
# arbol, asi que se declaran conocidos a mano.
GLOBALES_DEL_MODULO = {"__file__", "__name__", "__doc__", "__package__"}


def _nombres_sin_resolver(ruta: pathlib.Path) -> list[str]:
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    conocidos = set(dir(builtins)) | GLOBALES_DEL_MODULO

    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            conocidos.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                conocidos.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.arg):
            conocidos.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            conocidos.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            conocidos.add(n.name)
        elif isinstance(n, ast.Global):
            conocidos.update(n.names)

    usados = {n.id for n in ast.walk(arbol)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(usados - conocidos)


@pytest.mark.parametrize("ruta", ENTRADAS)
def test_los_puntos_de_entrada_no_usan_nombres_inexistentes(ruta):
    archivo = pathlib.Path(__file__).parent.parent / ruta
    assert archivo.is_file(), f"{ruta} no existe: actualiza la lista ENTRADAS"

    faltan = _nombres_sin_resolver(archivo)
    assert not faltan, (
        f"{ruta} usa nombres que no estan definidos ni importados: {faltan}. "
        f"El modulo importa igual; el fallo sale al ejecutar esa linea.")
