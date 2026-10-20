"""Un documento para la entidad al que le falta un dato obligatorio.

Antes, cuando faltaba el DNI, el CCI o la razon social, el documento salia
igual: con puntos suspensivos, con un marcador sin reemplazar o con la linea
en blanco. El usuario lo enviaba asi y la entidad lo descalificaba. Ahora el
llenador levanta esta excepcion y la web le dice al usuario que dato falta,
antes de que descargue nada.

Los datos opcionales (garantia, cuando la convocatoria no la pide) siguen
pudiendo quedar en blanco: esto es solo para lo que invalida la oferta.
"""


class DocumentoIncompleto(Exception):
    def __init__(self, documento: str, faltantes: list[str]):
        self.documento = documento
        self.faltantes = list(faltantes)
        super().__init__(f"{documento}: falta {', '.join(self.faltantes)}")

    def mensaje(self) -> str:
        """Texto para mostrar al usuario."""
        return (f"No se puede generar {self.documento}: falta "
                f"{', '.join(self.faltantes)}. Completalo en los datos de la empresa.")
