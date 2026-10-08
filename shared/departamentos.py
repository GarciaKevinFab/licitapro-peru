"""Completa la columna `departamento` de las licitaciones que llegaron sin ella.

POR QUE HACE FALTA

  El filtro por regiones del panel y de las alertas compara contra
  `licitaciones.departamento`, y esa columna se deduce del texto del proceso
  ("... - DEPARTAMENTO DE PIURA"). Cuando el texto no lo dice, queda NULL, y
  el panel ensena esas filas a TODO el mundo por si acaso. Medido el
  2026-10-07: 1.682 de 8.262 filas (20%) sin departamento; en el panel de un
  cliente con cuatro regiones, 120 de 227 filas eran de otra parte.

LA PISTA QUE SI ESTA: EL RUC DE LA ENTIDAD

  Todas las filas de OECE traen `entidad_ruc`. Una municipalidad o un
  gobierno regional compra siempre para su territorio, asi que si el mismo
  RUC ya tiene filas con departamento, ese departamento vale para las que no
  lo tienen.

  Con una condicion, y no es un detalle: solo cuando ese RUC apunta a UN
  departamento de forma clara. EsSalud, el Ejercito o SUNAT compran para todo
  el pais y su RUC tiene filas en quince departamentos; asignarles el mas
  frecuente seria inventar. Se exige que un solo departamento reuna al menos
  el 90% de las filas del RUC y que haya al menos dos filas: una sola puede
  ser un acierto del detector de texto o un fallo suyo, y no se distingue.

NO ES UN ARREGLO DE UNA VEZ

  Corre al final de cada pasada del orquestador, despues del scoring: cada
  hora entran filas nuevas sin departamento y la regla es la misma. La
  reparacion de las filas que el detector de texto etiqueto MAL (el fallo de
  "publICA" -> Ica) si es de una vez, y vive en tools/reparar_departamentos.py.
"""
import logging

from shared.db import connection

log = logging.getLogger("shared.departamentos")

CUOTA_MINIMA = 0.9
FILAS_MINIMAS = 2


async def completar_por_ruc(cuota: float = CUOTA_MINIMA,
                            minimo: int = FILAS_MINIMAS) -> int:
    """Rellena `departamento` donde el RUC de la entidad lo delata. Filas tocadas."""
    async with connection() as conn:
        resultado = await conn.execute(
            """WITH por_ruc AS (
                   SELECT entidad_ruc, departamento, COUNT(*) AS n
                     FROM licitaciones
                    WHERE entidad_ruc IS NOT NULL AND departamento IS NOT NULL
                    GROUP BY entidad_ruc, departamento),
               total AS (
                   SELECT entidad_ruc, SUM(n) AS t FROM por_ruc GROUP BY entidad_ruc),
               claro AS (
                   SELECT DISTINCT ON (p.entidad_ruc) p.entidad_ruc, p.departamento
                     FROM por_ruc p JOIN total USING (entidad_ruc)
                    WHERE total.t >= $2 AND p.n::float / total.t >= $1
                    ORDER BY p.entidad_ruc, p.n DESC)
               UPDATE licitaciones l
                  SET departamento = claro.departamento,
                      updated_at = NOW()
                 FROM claro
                WHERE l.departamento IS NULL
                  AND l.entidad_ruc = claro.entidad_ruc""",
            cuota, minimo)
    # asyncpg devuelve "UPDATE 123".
    try:
        tocadas = int(resultado.split()[-1])
    except (ValueError, IndexError):
        tocadas = 0
    if tocadas:
        log.info("Departamento completado por RUC en %d licitaciones", tocadas)
    return tocadas
