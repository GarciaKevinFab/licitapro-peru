"""Rutas del disco ancladas a la raiz del proyecto, no al directorio actual.

POR QUE: "data/expedientes" relativo se resolvia contra el cwd del proceso.
En el contenedor el cwd es /app (la raiz) y funcionaba de casualidad; un
comando lanzado desde otra carpeta (una tarea programada, un script de tools/,
pytest desde un subdirectorio) escribia los archivos donde la web no los busca
y los documentos salian sin firma o el ZIP sin anexos.

En produccion el resultado es identico: la raiz es /app, asi que
RAIZ / "data/expedientes" es /app/data/expedientes, que es donde ya estan.
"""
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def en_raiz(ruta: str | os.PathLike) -> Path:
    """`ruta` tal cual si es absoluta; si es relativa, contra la raiz.

    Sirve tambien para las rutas guardadas en la base como texto relativo
    ("data/firmas/empresa_6_sello_x.png", expediente_zip_path de filas
    viejas): se escribieron con el cwd en /app, que es la raiz.
    """
    p = Path(ruta)
    return p if p.is_absolute() else RAIZ / p


def directorio(variable: str, por_defecto: str) -> Path:
    """El directorio de la variable de entorno `variable`, o `por_defecto`.

    Los dos se anclan a la raiz si son relativos: un .env de desarrollo con
    EXPEDIENTES_DIR=data/expedientes tampoco debe depender del cwd.
    """
    return en_raiz(os.getenv(variable) or por_defecto)
