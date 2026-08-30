"""Marca `tests` como paquete de verdad, y no es una formalidad.

  Varios modulos hacen `from tests.conftest import sin_base`. Sin este archivo,
  `tests` es un paquete de espacio de nombres, y esos pierden SIEMPRE contra un
  paquete normal que se llame igual en cualquier punto de sys.path. Basta con
  que una dependencia cualquiera haya publicado un `tests/` en site-packages
  -- pasa mas de lo que parece -- para que la suite entera deje de cargar con:

      ModuleNotFoundError: No module named 'tests.conftest'

  Que es un error que apunta al proyecto cuando la causa esta en el entorno de
  quien lo ejecuta. En el CI no se ve, porque alli el entorno esta limpio: solo
  se rompe en la maquina de alguien, que es donde mas cuesta diagnosticarlo.
"""
