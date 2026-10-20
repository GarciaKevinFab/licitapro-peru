"""Achicar un PDF escaneado para guardarlo en la base.

POR QUE

  Las EETT del GOREMAD son escaneos de 2 a 21 MB (220 MB entre las 36
  solicitudes abiertas de octubre de 2026). La base no puede con eso, y el
  postor las necesita: las adjunta a su cotizacion firmadas y selladas en
  cada hoja, como la ficha RUC y el RNP. Se guardan re-escaneadas: cada
  pagina a unos 100 ppp en gris, JPEG. Para leerlas y sellarlas sobra, y
  pesan diez veces menos.

  Un PDF que ya cabe en el objetivo vuelve tal cual: un documento de texto
  no se convierte en imagenes sin necesidad.
"""
from __future__ import annotations

import io

OBJETIVO = 2_500_000          # bytes: lo que se admite guardar
_ESCALAS = (1.4, 1.1, 0.85)   # 1.0 = 72 ppp; se baja hasta que quepa


def comprimir(pdf: bytes, objetivo: int | None = None) -> bytes:
    """El PDF, re-escaneado hasta pesar `objetivo` o menos. Si no se logra ni
    a la escala mas baja, se devuelve la version mas chica igual: antes que
    nada, que este."""
    objetivo = OBJETIVO if objetivo is None else objetivo
    if len(pdf) <= objetivo:
        return pdf
    import pypdfium2 as pdfium

    from shared.pdfium_candado import CANDADO

    with CANDADO:
        return _achicar(pdfium.PdfDocument(pdf), pdf, objetivo)


def _achicar(doc, pdf: bytes, objetivo: int) -> bytes:
    """El cuerpo de `comprimir`, con el candado de pdfium ya tomado."""
    mejor = None
    try:
        for escala in _ESCALAS:
            paginas = [p.render(scale=escala).to_pil().convert("L") for p in doc]
            buf = io.BytesIO()
            paginas[0].save(buf, "PDF", save_all=True, append_images=paginas[1:],
                            resolution=72 * escala, quality=55)
            salida = buf.getvalue()
            if mejor is None or len(salida) < len(mejor):
                mejor = salida
            if len(salida) <= objetivo:
                break
    finally:
        doc.close()
    return mejor if mejor is not None else pdf
