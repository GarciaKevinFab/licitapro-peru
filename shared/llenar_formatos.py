"""Llena los formatos que manda la entidad, en vez de dibujar los nuestros.

POR QUE

  Una solicitud de cotizacion trae su propio PDF: la tabla de items con las
  columnas MARCA / PRECIO UNITARIO / PRECIO TOTAL vacias, y una declaracion
  jurada con lineas punteadas. La entidad espera recibir ESE documento lleno.
  Una cotizacion con formato propio, por correcta que sea, es "otra cosa" para
  quien la revisa, y el usuario la rechaza antes que la entidad.

COMO

  pdfplumber da las palabras con su posicion y las lineas de la tabla. Con eso
  se ubican las columnas (por las lineas verticales mas cercanas a MARCA y a
  los dos PRECIO), las filas (por las horizontales entre la cabecera y la fila
  TOTAL) y las etiquetas de la cabecera y de las condiciones. Lo que se
  escribe va en una capa de reportlab que pypdf funde con cada pagina: el
  original no se toca, solo se le pone tinta encima.

  En la declaracion jurada se buscan los tramos de puntos (".........") y se
  decide que va en cada uno por el texto que lo precede ("Yo," -> el
  representante, "DNI N°" -> el DNI...). Asi sirve tambien para la DJ de otra
  entidad que use las mismas palabras.

SI NO SE RECONOCE

  FormatoNoReconocido, con un mensaje para el usuario. Quien llama cae al PDF
  propio (shared/documentos_menores.py): nunca se entrega un formato mal
  llenado como si estuviera bien.
"""
from __future__ import annotations

import io
import logging
import os
import re
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from shared.config import normalizar

log = logging.getLogger("shared.llenar_formatos")

TINTA = (0.05, 0.15, 0.45)        # azul de boligrafo: se ve que es lo llenado
MAX_BYTES = 10 * 1024 * 1024


class FormatoNoReconocido(ValueError):
    """El PDF no tiene la forma esperada. El mensaje es para el usuario."""


# ─── Donde se guardan los formatos que sube el usuario ──────
#
# En el volumen de datos (data/ en el contenedor), uno por propuesta y tipo.
# No van a la base: son PDF de la entidad, se reemplazan enteros y solo los
# lee el generador de esta misma propuesta.

DIR = Path(os.getenv("FORMATOS_DIR", "data/formatos"))
TIPOS = {"sc": "Solicitud de cotización", "dj": "Declaración jurada",
         "eett": "Especificaciones técnicas"}


def ruta(propuesta_id: int, tipo: str) -> Path:
    if tipo not in TIPOS:
        raise ValueError(tipo)
    return DIR / f"propuesta_{int(propuesta_id)}" / f"{tipo}.pdf"


def guardados(propuesta_id: int) -> dict[str, bool]:
    return {t: ruta(propuesta_id, t).exists() for t in TIPOS}


def guardar(propuesta_id: int, tipo: str, contenido: bytes) -> None:
    """Valida que sea un PDF legible y lo guarda, reemplazando el anterior."""
    with _abrir(contenido) as pdf:
        if not pdf.pages:
            raise FormatoNoReconocido("El PDF no tiene páginas.")
    destino = ruta(propuesta_id, tipo)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(contenido)


def borrar(propuesta_id: int, tipo: str) -> None:
    ruta(propuesta_id, tipo).unlink(missing_ok=True)


async def formato(propuesta_id: int, licitacion_id: str, tipo: str) -> tuple[bytes, str] | None:
    """El PDF a llenar y su origen: el que subio el usuario manda (es una
    correccion suya); si no, el que bajo la PC puente del portal de la entidad."""
    subido = ruta(propuesta_id, tipo)
    if subido.exists():
        return subido.read_bytes(), "subido"
    from shared.db import contenido_documento
    contenido = await contenido_documento(licitacion_id, tipo)
    return (bytes(contenido), "portal") if contenido else None


async def disponibles(propuesta_id: int, licitacion_id: str) -> dict[str, str | None]:
    """{tipo: 'subido' | 'portal' | None} para la pantalla de la propuesta."""
    from shared.db import documentos_de
    del_portal = {d["tipo"] for d in await documentos_de(licitacion_id) if d["guardado"]}
    return {t: "subido" if ruta(propuesta_id, t).exists()
            else ("portal" if t in del_portal else None) for t in TIPOS}


# ─── Utilidades comunes ─────────────────────────────────────

def _abrir(contenido: bytes):
    import pdfplumber
    if not contenido or not contenido.startswith(b"%PDF"):
        raise FormatoNoReconocido("El archivo tiene que ser un PDF.")
    if len(contenido) > MAX_BYTES:
        raise FormatoNoReconocido("El PDF pesa demasiado (máximo 10 MB).")
    try:
        return pdfplumber.open(io.BytesIO(contenido))
    except Exception as e:
        raise FormatoNoReconocido("No se pudo leer el PDF. ¿Está dañado o protegido?") from e


class _Capa:
    """Lo que se escribe sobre una pagina, en coordenadas de pdfplumber (y hacia abajo)."""

    def __init__(self, ancho: float, alto: float):
        from reportlab.pdfgen import canvas
        self.alto = alto
        self.buf = io.BytesIO()
        self.c = canvas.Canvas(self.buf, pagesize=(ancho, alto))
        self.usada = False

    def texto(self, x, base, t, size=8.0, negrita=False, ancho_max=None, derecha=False,
              centro=False, color=TINTA):
        from reportlab.pdfbase.pdfmetrics import stringWidth
        fuente = "Helvetica-Bold" if negrita else "Helvetica"
        t = str(t)
        if ancho_max:
            while size > 5 and stringWidth(t, fuente, size) > ancho_max:
                size -= 0.25
        self.c.setFillColorRGB(*color)
        self.c.setFont(fuente, size)
        y = self.alto - base
        if derecha:
            self.c.drawRightString(x, y, t)
        elif centro:
            self.c.drawCentredString(x, y, t)
        else:
            self.c.drawString(x, y, t)
        self.usada = True

    def tapar(self, x0, top, x1, bottom):
        self.c.setFillColorRGB(1, 1, 1)
        self.c.rect(x0, self.alto - bottom, x1 - x0, bottom - top, stroke=0, fill=1)
        self.usada = True

    def imagen(self, ruta: str, x_centro: float, base: float, ancho: float, alto: float) -> None:
        """Una imagen (firma o sello) con su borde inferior en `base`, centrada en x."""
        from reportlab.lib.utils import ImageReader
        try:
            img = ImageReader(ruta)
            iw, ih = img.getSize()
            escala = min(ancho / iw, alto / ih)
            w, h = iw * escala, ih * escala
            self.c.drawImage(img, x_centro - w / 2, self.alto - base, w, h, mask="auto")
            self.usada = True
        except Exception as e:  # noqa: BLE001
            # Una firma ilegible no impide el documento: queda el espacio para firmar.
            log.warning("No se pudo estampar %s: %s", ruta, e)

    def linea(self, x0, y, x1):
        self.c.setStrokeColorRGB(0, 0, 0)
        self.c.setLineWidth(0.6)
        self.c.line(x0, self.alto - y, x1, self.alto - y)
        self.usada = True

    def pdf(self) -> bytes:
        self.c.save()
        return self.buf.getvalue()


def _fundir(original: bytes, capas: dict[int, _Capa]) -> bytes:
    from pypdf import PdfReader, PdfWriter
    lector = PdfReader(io.BytesIO(original))
    escritor = PdfWriter()
    for i, pagina in enumerate(lector.pages):
        capa = capas.get(i)
        if capa and capa.usada:
            pagina.merge_page(PdfReader(io.BytesIO(capa.pdf())).pages[0])
        escritor.add_page(pagina)
    salida = io.BytesIO()
    escritor.write(salida)
    return salida.getvalue()


def _palabras(pagina) -> list[dict]:
    return pagina.extract_words(keep_blank_chars=False, use_text_flow=False)


def _cerca(valores: list[float], x: float, tol: float = 40) -> float | None:
    mejor = min(valores, key=lambda v: abs(v - x), default=None)
    return mejor if mejor is not None and abs(mejor - x) <= tol else None


def _num(texto: str) -> Decimal | None:
    t = texto.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    return Decimal(t)


def _etiqueta(palabras, *frases) -> dict | None:
    """La ultima palabra de la primera frase encontrada ("FORMA DE PAGO:")."""
    for frase in frases:
        partes = normalizar(frase).split()
        for i in range(len(palabras) - len(partes) + 1):
            grupo = palabras[i:i + len(partes)]
            if [normalizar(w["text"]) for w in grupo] == partes and \
                    len({round(w["top"]) for w in grupo}) == 1:
                return grupo[-1]
    return None


# ─── Solicitud de cotizacion ────────────────────────────────

def _localizar_tabla(pagina) -> dict:
    """Columnas MARCA / P. UNITARIO / P. TOTAL y las filas de items."""
    palabras = _palabras(pagina)
    marca = next((w for w in palabras if normalizar(w["text"]) == "marca"), None)
    precios = sorted((w for w in palabras if normalizar(w["text"]) == "precio"),
                     key=lambda w: w["x0"])
    if not marca or len(precios) < 2:
        raise FormatoNoReconocido(
            "No encontramos las columnas MARCA y PRECIO de la solicitud de cotización.")
    pu, pt = precios[0], precios[1]

    verticales = sorted({round(e["x0"], 1) for e in pagina.vertical_edges})
    horizontales = sorted({round(e["top"], 1) for e in pagina.horizontal_edges})

    def borde(x):
        v = _cerca(verticales, x, tol=60)
        if v is None:
            raise FormatoNoReconocido("La tabla de la solicitud no tiene sus líneas.")
        return v

    c_marca = (marca["x0"] + marca["x1"]) / 2
    c_pu = (pu["x0"] + pu["x1"]) / 2
    c_pt = (pt["x0"] + pt["x1"]) / 2
    cols = {
        # DESCRIPCION: desde la linea vertical anterior al borde de MARCA.
        "descripcion": (max(v for v in verticales if v < borde(c_marca - (c_pu - c_marca) / 2) - 5),
                        borde(c_marca - (c_pu - c_marca) / 2)),
        "marca": (borde(c_marca - (c_pu - c_marca) / 2), borde((c_marca + c_pu) / 2)),
        "pu": (borde((c_marca + c_pu) / 2), borde((c_pu + c_pt) / 2)),
        "pt": (borde((c_pu + c_pt) / 2), borde(c_pt + (c_pt - c_pu) / 2)),
    }

    # Fin de la cabecera: la linea horizontal bajo "UNITARIO"/"TOTAL" de la cabecera.
    cab_bottom = max(w["bottom"] for w in palabras
                     if w["top"] >= pu["top"] - 2 and w["top"] <= pu["top"] + 16
                     and w["x0"] >= cols["pu"][0] - 2)
    inicio = _cerca([y for y in horizontales if y >= cab_bottom], cab_bottom, tol=20)
    # La fila TOTAL: la palabra "TOTAL" bajo la cabecera, en la columna P.U.
    total = next((w for w in palabras
                  if normalizar(w["text"]) == "total" and w["top"] > cab_bottom + 5
                  and cols["pu"][0] - 5 <= w["x0"] <= cols["pu"][1]), None)
    if not total or inicio is None:
        raise FormatoNoReconocido("No encontramos la fila TOTAL de la solicitud.")

    lineas = [y for y in horizontales if inicio - 0.5 <= y <= total["top"] + 1]
    if len(lineas) < 2:
        raise FormatoNoReconocido("No se distinguen las filas de la tabla de la solicitud.")
    bandas = list(pairwise(lineas))
    fila_total = next(((a, b) for a, b in pairwise(horizontales)
                       if a <= (total["top"] + total["bottom"]) / 2 <= b),
                      (total["top"] - 4, total["bottom"] + 4))

    # La cantidad de cada fila: el primer numero de la columna izquierda.
    izquierda = min(verticales)
    cantidades = []
    for top, bottom in bandas:
        nums = [w for w in palabras if top <= (w["top"] + w["bottom"]) / 2 <= bottom
                and w["x0"] < izquierda + 70 and _num(w["text"]) is not None]
        cantidades.append(_num(nums[0]["text"]) if nums else None)
    return {"cols": cols, "bandas": bandas, "cantidades": cantidades,
            "fila_total": fila_total, "palabras": palabras, "izquierda": izquierda}


def llenar_solicitud(contenido: bytes, empresa: dict, items: list[dict], condiciones: dict,
                     total_letras: str, total: Decimal, imagenes: dict | None = None) -> bytes:
    """La SC de la entidad con marca y precios por fila, cabecera y condiciones.

    Los items van en el orden de la SC. Si el numero de filas o las cantidades
    no coinciden con los items de la propuesta, no se llena: un precio en la
    fila equivocada es peor que una cotizacion con formato propio."""
    from shared.cotizacion import monto, precio_legible

    with _abrir(contenido) as pdf:
        paginas = pdf.pages
        donde, error = None, None
        for n, pag in enumerate(paginas):
            try:
                donde = (n, pag, _localizar_tabla(pag))
                break
            except FormatoNoReconocido as e:
                error = error or e
        if donde is None:
            raise error or FormatoNoReconocido("El PDF no tiene páginas.")
        n, pag, tabla = donde

        filas = [(b, c) for b, c in zip(tabla["bandas"], tabla["cantidades"]) if c is not None]
        if len(filas) != len(items):
            raise FormatoNoReconocido(
                f"La solicitud tiene {len(filas)} ítems y la propuesta {len(items)}: "
                "revisa los ítems antes de llenar el formato.")
        for (_, cant), it in zip(filas, items):
            if Decimal(str(it["cantidad"])) != cant:
                raise FormatoNoReconocido(
                    f"La cantidad de «{it['descripcion'][:40]}» no coincide con la solicitud "
                    f"({it['cantidad']} frente a {cant}).")

        capas = {i: _Capa(p.width, p.height) for i, p in enumerate(paginas)}
        capa = capas[n]
        cm0, cm1 = tabla["cols"]["marca"]
        _, cu1 = tabla["cols"]["pu"]
        _, ct1 = tabla["cols"]["pt"]
        cd0 = tabla["cols"]["descripcion"]
        for ((top, bottom), _), it in zip(filas, items):
            medio = (top + bottom) / 2
            # La presentacion ofertada ("x 330 g", "a granel") va al lado de la
            # descripcion, como se anota a mano; en MARCA solo la marca.
            presentacion = (it.get("presentacion") or "").strip()
            if presentacion:
                capa.texto(cd0[1] - 4, bottom - 4, presentacion, 7, derecha=True,
                           ancho_max=(cd0[1] - cd0[0]) * 0.6)
            marca = (it.get("marca") or "").strip()
            if marca:
                partes = _partir(marca, cm1 - cm0 - 4, 7)
                primera = medio + 2.5 - 4.25 * (len(partes) - 1)
                for k, linea in enumerate(partes):
                    capa.texto((cm0 + cm1) / 2, primera + k * 8.5, linea, 7, centro=True,
                               ancho_max=cm1 - cm0 - 4)
            pu = it.get("precio_unitario")
            st = it.get("subtotal")
            if pu is not None:
                capa.texto(cu1 - 4, medio + 3, precio_legible(pu), 9, derecha=True)
            if st is not None:
                capa.texto(ct1 - 4, medio + 3, monto(st), 9, derecha=True)
        t0, t1 = tabla["fila_total"]
        capa.texto(ct1 - 4, (t0 + t1) / 2 + 3.5, monto(total), 9.5, negrita=True, derecha=True)
        # El importe en letras NO se escribe: el formato del GOREMAD no lo trae y
        # el usuario pidio respetar el formato tal cual (`total_letras` se
        # conserva para la cotizacion propia, que si lo lleva).

        _cabecera(capas, paginas, n, empresa)

        # Condiciones: la respuesta a continuacion de cada etiqueta.
        pal = tabla["palabras"]
        for frases, valor in (
                (("FORMA DE PAGO:",), condiciones.get("forma_pago")),
                (("GARANTÍA:", "GARANTIA:"), condiciones.get("garantia")),
                (("INCLUIR I.G.V.",), "Sí, los precios incluyen I.G.V."),
                (("EJECUCIÓN DEL SERVICIO:", "PLAZO DE ENTREGA:"), condiciones.get("plazo_entrega")),
                (("TIPO DE MONEDA:",), condiciones.get("moneda") or "Soles (S/)"),
                (("VALIDEZ DE LA COTIZACIÓN:",), condiciones.get("validez"))):
            w = _etiqueta(pal, *frases)
            if w and valor:
                capa.texto(w["x1"] + 5, w["bottom"] - 1.5, str(valor).upper(), 7.5,
                           ancho_max=pag.width - w["x1"] - 50)

        _bloque_postor(capas, paginas, empresa, imagenes or {})
        return _fundir(contenido, capas)


def _cabecera(capas, paginas, n, empresa) -> None:
    """Señores / R.U.C. / Direccion / Telefono, en la primera pagina que los tenga."""
    for pi in sorted({0, n}):
        pal = _palabras(paginas[pi])
        etiquetas = {}
        for clave, frases in (("senores", ("Señores",)), ("ruc", ("R.U.C.", "RUC")),
                              ("direccion", ("Dirección",)), ("telefono", ("Teléfono",))):
            w = _etiqueta(pal, *frases)
            if w:
                etiquetas[clave] = w
        if "senores" not in etiquetas:
            continue
        cp, ancho = capas[pi], paginas[pi].width
        # Los valores van tras los ":" de la columna de etiquetas.
        dos_puntos = [w for w in pal if w["text"] == ":" and
                      abs(w["top"] - etiquetas["senores"]["top"]) < 3 and
                      w["x0"] < etiquetas["senores"]["x1"] + 30]
        x_valor = (dos_puntos[0]["x1"] if dos_puntos else etiquetas["senores"]["x1"] + 14) + 4
        ruc_w = etiquetas.get("ruc")
        fax = _etiqueta(pal, "Fax")
        for clave, valor, size, neg in (
                ("senores", empresa.get("razon_social"), 9, True),
                ("direccion", empresa.get("direccion"), 7.5, False),
                ("telefono", empresa.get("telefono"), 7.5, False)):
            w = etiquetas.get(clave)
            if not w or not valor:
                continue
            limite = ancho - 40
            if clave == "senores" and ruc_w and abs(ruc_w["top"] - w["top"]) < 4:
                limite = ruc_w["x0"] - 8
            if clave == "telefono" and fax and abs(fax["top"] - w["top"]) < 4:
                limite = fax["x0"] - 6
            cp.texto(x_valor, w["bottom"] - 1.5, valor, size, negrita=neg,
                     ancho_max=limite - x_valor)
        if ruc_w and empresa.get("ruc"):
            # El SIGA pone los dos puntos aparte ("R.U.C.  :"): el RUC va tras ellos.
            dp = next((w for w in pal if w["text"] == ":" and abs(w["top"] - ruc_w["top"]) < 3
                       and 0 <= w["x0"] - ruc_w["x1"] <= 16), None)
            cp.texto((dp or ruc_w)["x1"] + 6, ruc_w["bottom"] - 1.5, empresa["ruc"], 9,
                     negrita=True)
        return


def _partir(texto: str, ancho: float, size: float) -> list[str]:
    """Hasta dos lineas que quepan en la celda."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    if stringWidth(texto, "Helvetica", size) <= ancho:
        return [texto]
    lineas, actual = [], ""
    for p in texto.split():
        prueba = f"{actual} {p}".strip()
        if stringWidth(prueba, "Helvetica", size) <= ancho or not actual:
            actual = prueba
        else:
            lineas.append(actual)
            actual = p
    lineas.append(actual)
    return lineas if len(lineas) <= 2 else [lineas[0], " ".join(lineas[1:])]


def _firma_junto_a_la_entidad(capas: dict, paginas, empresa: dict, imagenes: dict) -> bool:
    """Firma y sello a la derecha de la firma de la entidad ("Area de Logistica"),
    a su misma altura: asi se presenta la SC del GOREMAD firmada a mano. False si
    la SC no tiene esa linea de firma (entonces va el bloque del postor)."""
    for n in range(len(paginas) - 1, -1, -1):
        pag = paginas[n]
        palabras = _palabras(pag)
        rotulo = next((w for w in palabras if normalizar(w["text"]).startswith("logistica")), None)
        if not rotulo:
            continue
        lineas = [ln for ln in pag.lines if abs(ln["top"] - ln["bottom"]) < 1
                  and rotulo["top"] - 20 < ln["top"] < rotulo["top"]
                  and ln["x0"] < rotulo["x1"] and ln["x1"] > rotulo["x0"]]
        if not lineas:
            continue
        linea = lineas[0]
        y = linea["top"]
        cx = min(pag.width - 125, linea["x1"] + (pag.width - linea["x1"]) / 2)
        capa = capas[n]
        if imagenes.get("sello") or imagenes.get("firma"):
            if imagenes.get("sello"):
                capa.imagen(imagenes["sello"], cx, y + 48, 180, 72)
            if imagenes.get("firma"):
                capa.imagen(imagenes["firma"], cx, y + 26, 110, 55)
            return True
        # Sin imagenes (DNIe o firma a mano): la linea y los datos de quien firma.
        capa.linea(cx - 90, y, cx + 90)
        cargo = (empresa.get("cargo_representante") or "Representante legal").upper()
        for k, (t, neg) in enumerate(((empresa.get("representante_legal") or "", True),
                                      (f"DNI N° {empresa.get('dni_representante') or ''}", False),
                                      (cargo, False),
                                      (empresa.get("razon_social") or "", True))):
            capa.texto(cx, y + 11 + k * 10, t, 7.5, negrita=neg, centro=True,
                       ancho_max=200)
        return True
    return False


def _bloque_postor(capas: dict, paginas, empresa: dict, imagenes: dict) -> None:
    """Firma del postor: junto a la de la entidad si la SC la tiene; si no, datos
    del postor y linea de firma en el espacio libre de la ultima pagina."""
    if _firma_junto_a_la_entidad(capas, paginas, empresa, imagenes):
        return
    ult = len(paginas) - 1
    pag = paginas[ult]
    cuerpo = [w for w in _palabras(pag) if w["bottom"] < pag.height - 60]
    libre_desde = max((w["bottom"] for w in cuerpo), default=80) + 40
    if pag.height - 70 - libre_desde < 190:
        return  # sin sitio: no se encima nada sobre el texto de la entidad
    capa = capas[ult]
    y = libre_desde
    capa.texto(42, y, "DATOS DEL POSTOR", 9, negrita=True)
    for rotulo, valor in (("Razón social:", empresa.get("razon_social")),
                          ("RUC:", empresa.get("ruc")),
                          ("Domicilio fiscal:", empresa.get("direccion")),
                          ("Teléfono:", empresa.get("telefono")),
                          ("Correo:", empresa.get("email"))):
        if valor:
            y += 13
            capa.texto(42, y, rotulo, 8, negrita=True)
            capa.texto(125, y, valor, 8, ancho_max=pag.width - 170)
    y += 95
    centro = pag.width / 2
    # Como se firma en papel: el sello sobre la linea y la firma encima de el.
    if imagenes.get("sello"):
        capa.imagen(imagenes["sello"], centro, y - 1, 200, 80)
    if imagenes.get("firma"):
        capa.imagen(imagenes["firma"], centro, y - 6, 120, 62)
    capa.linea(centro - 110, y, centro + 110)
    cargo = (empresa.get("cargo_representante") or "Representante legal").upper()
    for k, (t, neg) in enumerate(((empresa.get("representante_legal") or "", True),
                                  (f"DNI N° {empresa.get('dni_representante') or ''}", False),
                                  (f"{cargo} - REPRESENTANTE LEGAL", False),
                                  (empresa.get("razon_social") or "", True))):
        capa.texto(centro, y + 12 + k * 11, t, 8, negrita=neg, centro=True)
    capa.texto(centro, y + 64, "(Firma y sello del postor)", 7.5, centro=True,
               color=(0.4, 0.4, 0.4))


# ─── Declaracion jurada ─────────────────────────────────────

_RELLENO = set("._…·")


def _tramos_de_puntos(pagina) -> list[dict]:
    """Los tramos de puntos/guiones bajos de 5 o mas caracteres, en orden de lectura."""
    chars = sorted(pagina.chars, key=lambda c: (round(c["top"]), c["x0"]))
    tramos, actual = [], []
    for c in chars:
        if c["text"] in _RELLENO:
            if actual and (abs(c["top"] - actual[-1]["top"]) > 2 or c["x0"] - actual[-1]["x1"] > 6):
                tramos.append(actual)
                actual = []
            actual.append(c)
        elif actual and c["text"].strip():
            tramos.append(actual)
            actual = []
    if actual:
        tramos.append(actual)
    return [{"x0": t[0]["x0"], "x1": t[-1]["x1"], "top": min(c["top"] for c in t),
             "bottom": max(c["bottom"] for c in t)} for t in tramos if len(t) >= 5]


def _contexto(pagina, tramo: dict, n: int = 40) -> str:
    """El texto que hay justo antes del tramo: en su linea o, si empieza la linea,
    al final de la anterior."""
    misma = [c for c in pagina.chars if c["text"] not in _RELLENO
             and abs(c["top"] - tramo["top"]) < 3 and c["x1"] <= tramo["x0"] + 1]
    misma.sort(key=lambda c: c["x0"])
    texto = "".join(c["text"] for c in misma)
    if len(normalizar(texto).strip()) < 3:
        previa = [c for c in pagina.chars if c["text"] not in _RELLENO
                  and tramo["top"] - 18 < c["top"] < tramo["top"] - 3]
        previa.sort(key=lambda c: c["x0"])
        texto = "".join(c["text"] for c in previa) + " " + texto
    return re.sub(r"\s+", " ", normalizar(texto)).strip()[-n:]


def llenar_declaracion_jurada(contenido: bytes, empresa: dict, cci: str | None,
                              dia: str, mes: str, imagenes: dict | None = None) -> bytes:
    """La DJ de la entidad con los datos del postor en sus lineas punteadas."""
    with _abrir(contenido) as pdf:
        pag = pdf.pages[0]
        capa = _Capa(pag.width, pag.height)
        tramos = _tramos_de_puntos(pag)
        if not tramos:
            raise FormatoNoReconocido("La declaración jurada no tiene campos punteados que llenar.")
        llenados, detectados = set(), set()
        for tr in tramos:
            ctx = _contexto(pag, tr)
            campo = None
            if re.search(r"\byo,?$", ctx):
                campo = "representante_legal"
            elif ctx.endswith("empresa"):
                campo = "razon_social"
            elif re.search(r"dni n\S?$", ctx):
                campo = "dni_representante"
            elif re.search(r"ruc( n\S?)?$", ctx) or re.search(r"^n\S?$", ctx[-2:]) and "ruc" in ctx:
                campo = "ruc"
            elif ctx.endswith("fiscal en"):
                campo = "direccion"
            elif ctx.endswith("interbancaria"):
                campo = "cci"
            if campo:
                detectados.add(campo)
            valor = cci if campo == "cci" else empresa.get(campo) if campo else None
            if not valor or campo in llenados:
                continue   # sin dato: la linea queda para escribirla a mano
            chico = campo == "direccion"
            capa.tapar(tr["x0"] - 0.5, tr["top"] - 0.5, tr["x1"] + 0.5, tr["bottom"] + 0.5)
            capa.texto(tr["x0"] + 2, tr["bottom"] - 1.8, valor, 7.5 if chico else 9,
                       negrita=not chico, ancho_max=tr["x1"] - tr["x0"] - 3)
            llenados.add(campo)
        # Se exige reconocer el FORMATO (que tenga nombre, DNI y RUC), no que la
        # empresa tenga todos los datos: lo que falte queda en blanco.
        if not {"representante_legal", "dni_representante", "ruc"} <= detectados:
            raise FormatoNoReconocido(
                "No reconocimos los campos de la declaración jurada (nombre, DNI, RUC).")

        # Fecha "  /   / 2026": dia antes de la primera barra y mes entre las dos.
        palabras = _palabras(pag)
        fecha = _etiqueta(palabras, "Fecha:")
        if fecha:
            barras = [w for w in palabras if w["text"].startswith("/") and
                      abs(w["top"] - fecha["top"]) < 3 and w["x0"] > fecha["x1"]]
            if len(barras) >= 2:
                capa.texto((fecha["x1"] + barras[0]["x0"]) / 2, fecha["bottom"] - 1.5, dia, 9,
                           negrita=True, centro=True)
                capa.texto((barras[0]["x1"] + barras[1]["x0"]) / 2, fecha["bottom"] - 1.5, mes,
                           9, negrita=True, centro=True)

        # Firma y sello sobre la linea de "Firma, nombre y/o sello".
        imagenes = imagenes or {}
        rotulo = max((w for w in palabras if normalizar(w["text"]).startswith("firma")),
                     key=lambda w: w["top"], default=None)
        if rotulo and (imagenes.get("firma") or imagenes.get("sello")):
            linea = max((t for t in tramos if t["bottom"] <= rotulo["top"] + 2),
                        key=lambda t: t["bottom"], default=None)
            base = (linea["top"] if linea else rotulo["top"]) - 1
            centro = (linea["x0"] + linea["x1"]) / 2 if linea else pag.width / 2
            # El hueco que queda sobre la linea hasta el texto de la entidad: la
            # imagen se encoge a lo que cabe para no tapar la "NOTA" de encima.
            encima = [w["bottom"] for w in palabras
                      if w["bottom"] < base - 2 and centro - 110 < w["x0"] < centro + 110]
            alto = min(80.0, max(28.0, base - (max(encima) if encima else base - 80) - 3))
            if imagenes.get("sello"):
                capa.imagen(imagenes["sello"], centro, base, alto * 2.5, alto)
            if imagenes.get("firma"):
                capa.imagen(imagenes["firma"], centro, base - 3, alto * 1.6, alto * 0.85)
        return _fundir(contenido, {0: capa})


def sellar_paginas(contenido: bytes, imagenes: dict | None) -> bytes:
    """Firma y sello en la esquina inferior derecha de CADA pagina.

    Asi presenta el postor lo que adjunta a la cotizacion -- ficha RUC,
    constancia del RNP, EETT de la entidad --: cada hoja firmada y sellada,
    como se lo piden en papel. Sin imagenes (firma con DNIe) vuelve tal cual.
    """
    imagenes = imagenes or {}
    if not (imagenes.get("sello") or imagenes.get("firma")):
        return contenido
    tintas = _mapas_de_tinta(contenido)
    with _abrir(contenido) as pdf:
        capas = {}
        for n, pag in enumerate(pdf.pages):
            capa = _Capa(pag.width, pag.height)
            cx, base, k = _hueco_para_el_sello(pag, tintas[n] if n < len(tintas) else None)
            if imagenes.get("sello"):
                capa.imagen(imagenes["sello"], cx, base, SELLO_ANCHO * k, 68 * k)
            if imagenes.get("firma"):
                capa.imagen(imagenes["firma"], cx, base - 20 * k, 105 * k, 52 * k)
            capas[n] = capa
    return _fundir(contenido, capas)


SELLO_ANCHO, SELLO_ALTO = 170, 90   # sello + firma encima, en puntos
_ESCALA_TINTA = 0.5                  # pixeles por punto del mapa de tinta (36 ppp)


def _mapas_de_tinta(contenido: bytes) -> list:
    """Cada pagina en gris a 36 ppp, para ver la tinta de un escaneo.

    Un TDR escaneado es una sola imagen: pdfplumber no ve que abajo a la
    derecha ya estan la firma y el sello del supervisor de la entidad, y el
    nuestro caia encima (E/M 4202-2026 de la MPT). Mirando los pixeles si se
    ve. Si la pagina no se puede pintar, se sigue sin mapa."""
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(contenido)
        try:
            return [doc[i].render(scale=_ESCALA_TINTA).to_pil().convert("L")
                    for i in range(len(doc))]
        finally:
            doc.close()
    except Exception as e:  # noqa: BLE001
        log.info("Sin mapa de tinta para sellar: %s", e)
        return []


def _tinta_en(mapa, x0, y0, x1, y1) -> float:
    """Area oscura (en puntos cuadrados) dentro de la caja."""
    if mapa is None:
        return 0.0
    e = _ESCALA_TINTA
    caja = (max(0, int(x0 * e)), max(0, int(y0 * e)),
            min(mapa.width, int(x1 * e)), min(mapa.height, int(y1 * e)))
    if caja[2] <= caja[0] or caja[3] <= caja[1]:
        return 0.0
    oscuros = sum(mapa.crop(caja).histogram()[:150])
    return oscuros / (e * e)


def _hueco_para_el_sello(pag, tinta=None) -> tuple[float, float, float]:
    """(centro x, base y, escala) del sitio donde el sello tapa lo menos posible.

    Se prefiere abajo a la derecha, como se sella a mano; pero la ficha RUC
    lleva ahi el codigo QR con el que SUNAT la valida, y un pie de pagina con
    texto. Cada sitio posible paga por lo que tapa (texto e imagenes, caro;
    lineas y fondos, barato; y la tinta que se ve en la pagina pintada, que es
    lo unico que distingue algo en un escaneo), por lo que sube y por irse al
    centro o a la izquierda; gana el mas barato. En una hoja llena de texto
    de arriba abajo (un TDR escaneado) se prueba tambien al 70%: entra en el
    margen de un renglon corto sin pisar letras.
    """
    caros, baratos = [], []
    for k in ("chars", "images", "rects", "curves", "lines"):
        for o in getattr(pag, k, []):
            if o["x1"] - o["x0"] >= pag.width * 0.98 and o["bottom"] - o["top"] >= pag.height * 0.98:
                continue   # un fondo de pagina entera no dice nada
            (caros if k in ("chars", "images") else baratos).append(
                (o["x0"], o["top"], o["x1"], o["bottom"]))

    def tapado(cajas, x0, y0, x1, y1):
        return sum(max(0.0, min(x1, bx1) - max(x0, bx0)) * max(0.0, min(y1, by1) - max(y0, by0))
                   for bx0, by0, bx1, by1 in cajas)

    abajo = pag.height - 50
    mejor, coste_mejor = (pag.width - 100, abajo, 1.0), None
    for k, castigo_k in ((1.0, 0), (0.7, 400)):
        ancho, alto = SELLO_ANCHO * k, SELLO_ALTO * k
        sitios = ((pag.width - 30 - ancho / 2, 0), (pag.width / 2, 150), (30 + ancho / 2, 300))
        for base in range(int(abajo), int(pag.height * 0.4), -10):
            for cx, castigo in sitios:
                x0, x1 = cx - ancho / 2 - 6, cx + ancho / 2 + 6
                y0, y1 = base - alto - 6, base + 6
                coste = (3 * tapado(caros, x0, y0, x1, y1) + tapado(baratos, x0, y0, x1, y1)
                         + 4 * _tinta_en(tinta, x0, y0, x1, y1)
                         + 2 * (abajo - base) + castigo + castigo_k)
                if coste_mejor is None or coste < coste_mejor:
                    mejor, coste_mejor = (cx, float(base), k), coste
    return mejor


# ─── Lo que llaman la web y el expediente ───────────────────

async def sc_llena(propuesta_id: int, licitacion_id: str, empresa: dict, items: list[dict],
                   condiciones: dict, imagenes: dict | None = None) -> bytes | None:
    """La SC de la entidad llena, o None si no hay formato (ni subido ni del portal)."""
    from shared import cotizacion
    hallado = await formato(propuesta_id, licitacion_id, "sc")
    if not hallado:
        return None
    suma = cotizacion.total(items)
    from shared.llenar_siga import es_siga, llenar_solicitud_siga
    if es_siga(hallado[0]):
        # La SC del SIGA (municipalidades: Tambopata): otra tabla, sin columna MARCA.
        return llenar_solicitud_siga(hallado[0], empresa, items, condiciones, suma, imagenes)
    return llenar_solicitud(hallado[0], empresa, items, condiciones,
                            cotizacion.monto_en_letras(suma), suma, imagenes)


async def dj_llena(propuesta_id: int, licitacion_id: str, empresa: dict, cci: str | None,
                   imagenes: dict | None = None) -> bytes | None:
    """La DJ de la entidad llena, o None si no hay formato."""
    from shared import fechas
    hallado = await formato(propuesta_id, licitacion_id, "dj")
    if not hallado:
        return None
    hoy = fechas.hoy()
    return llenar_declaracion_jurada(hallado[0], empresa, cci, f"{hoy.day:02d}",
                                     f"{hoy.month:02d}", imagenes)


async def eett_selladas(propuesta_id: int, licitacion_id: str,
                        imagenes: dict | None = None) -> bytes | None:
    """Las EETT de la entidad con firma y sello en cada hoja, o None si no hay."""
    hallado = await formato(propuesta_id, licitacion_id, "eett")
    if not hallado:
        return None
    return sellar_paginas(hallado[0], imagenes)
