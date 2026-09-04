# N999 se dispara por el nombre de la CARPETA RAIZ, no por este paquete.
#
# En el runner de GitHub el repositorio se clona en `licitapro-peru`, con
# guion, y ruff lo lee como si fuera un nombre de modulo -- que en Python no
# puede llevar guiones. No lo es: es el nombre del repositorio, y renombrarlo
# romperia todas las URLs, los remotos de cada copia y el despliegue.
#
# En local no se veia porque la carpeta de trabajo se llama `licitapro`, sin
# guion: el aviso solo aparece en el CI.
