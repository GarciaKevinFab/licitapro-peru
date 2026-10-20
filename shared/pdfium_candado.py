"""Un solo hilo a la vez dentro de pdfium.

POR QUE

  pdfium (la biblioteca de Chrome que envuelve pypdfium2) no es segura entre
  hilos: dos hilos abriendo o pintando PDFs a la vez la corrompen y el proceso
  entero muere con "Fatal Python error: Aborted", sin excepcion que capturar.
  Mientras el renderizado corria en el bucle de eventos no podia pasar. Al
  mandarlo a `asyncio.to_thread` (para no congelar la web con un escaneo
  grande), dos peticiones simultaneas -- o dos pruebas -- lo tumbaron en CI.

  Todo uso de pypdfium2 va dentro de `with CANDADO:`. Es reentrante por si una
  funcion que ya lo tiene llama a otra que tambien lo pide.
"""
import threading

CANDADO = threading.RLock()
