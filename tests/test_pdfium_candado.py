"""pdfium desde varios hilos a la vez no tumba el proceso.

Sin el candado de `shared/pdfium_candado.py`, esto abortaba el interprete en
CI ("Fatal Python error: Aborted" dentro de pypdfium2), en cuanto el trabajo
de PDF paso a `asyncio.to_thread`.
"""
import io
from concurrent.futures import ThreadPoolExecutor

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from shared import llenar_formatos, membrete


def _pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for _ in range(3):
        c.setFont("Helvetica", 14)
        c.drawString(72, 780, "CABECERA DE PRUEBA")
        c.line(72, 770, 520, 770)
        c.drawString(72, 60, "pie de pagina")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_pintar_pdfs_desde_varios_hilos_a_la_vez(tmp_path):
    contenido = _pdf()
    ruta = tmp_path / "hoja.pdf"
    ruta.write_bytes(contenido)

    def trabajo(i):
        if i % 2:
            return membrete.medir(ruta)["cabecera_pt"] > 0
        return len(llenar_formatos._mapas_de_tinta(contenido)) == 3

    with ThreadPoolExecutor(max_workers=8) as hilos:
        assert all(hilos.map(trabajo, range(40)))
