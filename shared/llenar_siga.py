"""Llenar la solicitud de cotizacion del SIGA (MEF) sobre su propio PDF.

QUE FORMATO ES

  "Sistema Integrado de Gestion Administrativa - Modulo de Logistica": la SC
  que imprime el SIGA del MEF, el que usan municipalidades y unidades
  ejecutoras de todo el pais. La primera que llega es la de la Municipalidad
  Provincial de Tambopata (su "expediente" de cada E/M), pero el formato es el
  mismo en cualquier entidad que use el SIGA.

  No es la SC del GOREMAD (shared/llenar_formatos.py). Alli cada item es una
  fila con sus lineas y hay una columna MARCA. Aqui:

    * los items son renglones de texto dentro de UNA caja: no hay lineas
      horizontales entre ellos. Cada item empieza donde hay un numero en la
      columna CANTIDAD;
    * no hay columna de marca: el formato pide "Indicar Marca de Procedencia"
      en las condiciones. La marca (y la presentacion ofertada) se escribe en
      la celda de la descripcion, junto al item al que corresponde, que es
      donde la entidad la compara con lo pedido;
    * la firma no tiene linea impresa: va bajo "Atentamente;".

  Como en el GOREMAD, si los items de la SC no coinciden en numero y cantidad
  con los de la propuesta, no se llena: un precio en la fila equivocada es
  peor que no tener formato.
"""
from __future__ import annotations

from decimal import Decimal

from shared.config import normalizar
from shared.llenar_formatos import (
    FormatoNoReconocido,
    _abrir,
    _cabecera,
    _Capa,
    _etiqueta,
    _fundir,
    _num,
    _palabras,
)


def es_siga(contenido: bytes) -> bool:
    """True si el PDF es una SC impresa por el SIGA."""
    try:
        with _abrir(contenido) as pdf:
            texto = normalizar(" ".join(w["text"] for w in _palabras(pdf.pages[0])[:40]))
    except FormatoNoReconocido:
        return False
    return "sistema integrado de gestion administrativa" in texto


def _verticales(pag, y: float) -> list[float]:
    """Las lineas verticales que cruzan la altura `y` (la cabecera de la tabla).

    Solo esas: el pie de la SC tiene recuadros propios ("Requerimientos
    Tecnicos") cuyos bordes, si se mezclan, parten la columna DESCRIPCION.
    """
    return sorted({round(e["x0"], 1) for e in pag.vertical_edges
                   if e["top"] - 2 <= y <= e["bottom"] + 2})


def _columnas(pag, palabras) -> dict | None:
    """Columnas de la tabla y el fondo de su cabecera. None si la pagina no la tiene.

    Dos variantes del SIGA:
      * bienes:    CANTIDAD | UNIDAD | ITEM | DESCRIPCION | P. UNITARIO | P. TOTAL
      * servicios: UNIDAD | ITEM | DESCRIPCION | VALOR TOTAL  (sin cantidad ni
        precio unitario: el servicio se cotiza por su valor total)
    La referencia es DESCRIPCION, que esta en las dos.
    """
    desc = next((w for w in palabras if normalizar(w["text"]) == "descripcion"), None)
    if not desc:
        return None

    def en_cabecera(nombre):
        return sorted((w for w in palabras if normalizar(w["text"]) == nombre
                       and abs(w["top"] - desc["top"]) < 4), key=lambda w: w["x0"])

    vs = _verticales(pag, (desc["top"] + desc["bottom"]) / 2)

    def entre(w):
        x = (w["x0"] + w["x1"]) / 2
        izq = [v for v in vs if v < x - 2]
        der = [v for v in vs if v > x + 2]
        if not izq or not der:
            raise FormatoNoReconocido("La tabla de la solicitud no tiene sus líneas.")
        return max(izq), min(der)

    precios, valor = en_cabecera("precio"), en_cabecera("valor")
    if len(precios) >= 2:
        cols = {"pu": entre(precios[0]), "pt": entre(precios[1])}
        ref = precios[0]
    elif valor:
        cols = {"pt": entre(valor[0])}
        ref = valor[0]
    else:
        return None
    cols["descripcion"] = entre(desc)
    for nombre in ("cantidad", "unidad", "item"):
        w = en_cabecera(nombre)
        if w:
            cols[nombre] = entre(w[0])
    fondo = max(w["bottom"] for w in palabras
                if ref["top"] - 2 <= w["top"] <= ref["top"] + 16 and w["x0"] >= ref["x0"] - 4)
    return {"cols": cols, "cab_fondo": fondo}


def _filas(palabras, cols, desde, hasta) -> list[dict]:
    """Cada item: su cantidad, su unidad y las palabras de su descripcion.

    Un item empieza donde esta su codigo (columna ITEM, 12 digitos): viene
    siempre. La cantidad NO sirve de ancla: en los servicios sale en blanco.
    La descripcion puede ocupar varios renglones y su primer renglon queda un
    par de puntos por encima del codigo.
    """
    def en(col, w):
        if col not in cols:
            return False
        c0, c1 = cols[col]
        return c0 <= w["x0"] and w["x1"] <= c1 + 2

    if "item" in cols:
        arranques = [w for w in palabras if desde < w["top"] < hasta and en("item", w)
                     and w["text"].isdigit() and len(w["text"]) >= 6]
    else:
        arranques = [w for w in palabras if desde < w["top"] < hasta and en("cantidad", w)
                     and _num(w["text"]) is not None]
    arranques.sort(key=lambda w: w["top"])
    filas = []
    for i, w in enumerate(arranques):
        ini = w["top"] - 4
        fin = arranques[i + 1]["top"] - 4 if i + 1 < len(arranques) else hasta
        cerca = [p for p in palabras if ini <= p["top"] < fin]
        cant = [p for p in cerca if en("cantidad", p) and _num(p["text"]) is not None
                and abs(p["top"] - w["top"]) < 8]
        unidad = [p for p in cerca if en("unidad", p) and abs(p["top"] - w["top"]) < 8]
        desc = sorted((p for p in cerca if en("descripcion", p)),
                      key=lambda p: (round(p["top"]), p["x0"]))
        filas.append({"cantidad": _num(cant[0]["text"]) if cant else None,
                      "unidad": " ".join(p["text"] for p in sorted(unidad, key=lambda p: p["x0"])),
                      "top": w["top"], "bottom": w["bottom"], "fin": fin, "desc": desc,
                      "descripcion": " ".join(p["text"] for p in desc)})
    return filas


def _todas_las_filas(paginas):
    """(filas, pagina del TOTAL, palabra TOTAL, columnas de esa pagina, primera
    pagina con tabla)."""
    filas, primera = [], None
    for n, pag in enumerate(paginas):
        pal = _palabras(pag)
        tabla = _columnas(pag, pal)
        if tabla is None:
            continue
        primera = n if primera is None else primera
        cols = tabla["cols"]
        # El rotulo TOTAL queda justo a la izquierda de la columna del importe.
        total_w = next((w for w in pal if normalizar(w["text"]) == "total"
                        and w["top"] > tabla["cab_fondo"] + 5
                        and cols["pt"][0] - 75 <= w["x0"] and w["x1"] <= cols["pt"][0] + 2),
                       None)
        hasta = total_w["top"] - 3 if total_w else pag.height - 40
        for f in _filas(pal, cols, tabla["cab_fondo"] + 1, hasta):
            f.update(pagina=n, cols=cols)
            filas.append(f)
        if total_w:
            return filas, n, total_w, cols, primera
    return filas, None, None, None, primera


def leer_items_siga(contenido: bytes) -> list[dict]:
    """Los items de la SC, en su orden: descripcion, cantidad y unidad.

    Es lo que el radar guarda en `licitaciones.descripcion` para que la
    propuesta nazca con sus items (shared/cotizacion.sembrar). Un servicio sin
    cantidad cuenta como 1.
    """
    with _abrir(contenido) as pdf:
        filas = _todas_las_filas(pdf.pages)[0]
    return [{"descripcion": f["descripcion"], "cantidad": f["cantidad"] or Decimal(1),
             "unidad": f["unidad"] or None} for f in filas if f["descripcion"]]


def llenar_solicitud_siga(contenido: bytes, empresa: dict, items: list[dict],
                          condiciones: dict, total: Decimal,
                          imagenes: dict | None = None) -> bytes:
    """La SC del SIGA con precios, marca por item, cabecera, condiciones y firma."""
    from shared.cotizacion import monto, precio_legible

    imagenes = imagenes or {}
    with _abrir(contenido) as pdf:
        paginas = pdf.pages
        capas = {i: _Capa(p.width, p.height) for i, p in enumerate(paginas)}
        filas, pagina_total, fila_total, cols_total, primera_tabla = _todas_las_filas(paginas)
        if primera_tabla is None:
            raise FormatoNoReconocido(
                "No encontramos la tabla CANTIDAD / DESCRIPCIÓN / PRECIO de la solicitud.")
        if pagina_total is None:
            raise FormatoNoReconocido("No encontramos la fila TOTAL de la solicitud.")
        if len(filas) != len(items):
            raise FormatoNoReconocido(
                f"La solicitud tiene {len(filas)} ítems y la propuesta {len(items)}: "
                "revisa los ítems antes de llenarla.")
        for f, it in zip(filas, items, strict=True):
            if (f["cantidad"] is not None and it.get("cantidad") is not None
                    and f["cantidad"] != Decimal(str(it["cantidad"]))):
                raise FormatoNoReconocido(
                    f"La cantidad {f['cantidad']} de la solicitud no coincide con la del "
                    f"ítem «{(it.get('descripcion') or '')[:40]}»: revisa el orden de los ítems.")

        for f, it in zip(filas, items, strict=True):
            capa = capas[f["pagina"]]
            cols = f["cols"]
            base = f["bottom"] - 1
            if it.get("precio_unitario") is not None and "pu" in cols:
                capa.texto(cols["pu"][1] - 4, base, precio_legible(it["precio_unitario"]), 8.5,
                           derecha=True)
            if it.get("subtotal") is not None:
                capa.texto(cols["pt"][1] - 4, base, monto(it["subtotal"]), 8.5, derecha=True)
            _marca_en_la_descripcion(capa, f, it)

        tc = cols_total["pt"]
        capa = capas[pagina_total]
        capa.texto(tc[1] - 4, fila_total["bottom"] - 1, monto(total), 9, negrita=True,
                   derecha=True)

        _cabecera(capas, paginas, primera_tabla, empresa)
        _email(capas, paginas, primera_tabla, empresa)
        _condiciones(capas[pagina_total], paginas[pagina_total], condiciones)
        _firma(capas, paginas, empresa, imagenes)
        return _fundir(contenido, capas)


def _marca_en_la_descripcion(capa, fila, it) -> None:
    """"Marca: X - presentacion" en la celda de la descripcion, sin pisar texto.

    Por orden de preferencia: en un renglon propio bajo la descripcion, si cabe
    antes del proximo item; a la derecha del ultimo renglon, que suele ser
    corto ("OFICIO", "100 HOJAS"); a la derecha del primero. Si en ninguno cabe
    entero, va en el hueco mas ancho, achicada.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth
    marca = (it.get("marca") or "").strip()
    presentacion = (it.get("presentacion") or "").strip()
    texto = " - ".join(t for t in (f"Marca: {marca}" if marca else "", presentacion) if t)
    if not texto:
        return
    d0, d1 = fila["cols"]["descripcion"]
    desc = fila["desc"]
    ultimo = max((w["bottom"] for w in desc), default=fila["bottom"])
    if ultimo + 9 <= fila["fin"]:
        capa.texto(d0 + 4, ultimo + 7.5, texto, 7, ancho_max=d1 - d0 - 8)
        return
    renglones: dict[int, list] = {}
    for w in desc:
        renglones.setdefault(round(w["top"]), []).append(w)
    huecos = []
    for top in sorted(renglones, reverse=True):          # el ultimo primero
        ws = renglones[top]
        libre = d1 - 4 - (max(w["x1"] for w in ws) + 8)
        huecos.append((libre, max(w["bottom"] for w in ws) - 1))
    ancho = stringWidth(texto, "Helvetica", 7)
    for libre, base in huecos:
        if libre >= ancho:
            capa.texto(d1 - 4, base, texto, 7, derecha=True)
            return
    libre, base = max(huecos, default=(d1 - d0 - 8, fila["bottom"] - 1))
    capa.texto(d1 - 4, base, texto, 7, derecha=True, ancho_max=max(libre, 40))


def _email(capas, paginas, n, empresa) -> None:
    """El SIGA trae "Email :" en la cabecera; `_cabecera` (GOREMAD) no lo llena."""
    if not empresa.get("email"):
        return
    pal = _palabras(paginas[n])
    # "Email" y sus dos puntos pueden salir como una palabra ("Email:") o dos.
    w = next((p for p in pal if normalizar(p["text"]).rstrip(":") in ("email", "e-mail")), None)
    if not w:
        return
    dos = [p for p in pal if p["text"] == ":" and abs(p["top"] - w["top"]) < 3
           and p["x0"] > w["x1"] - 2 and p["x0"] < w["x1"] + 45]
    x = (dos[-1]["x1"] if dos else w["x1"] + 30) + 4
    fecha = _etiqueta(pal, "Fecha")
    limite = fecha["x0"] - 8 if fecha and abs(fecha["top"] - w["top"]) < 4 else x + 140
    capas[n].texto(x, w["bottom"] - 1.5, empresa["email"], 7.5, ancho_max=limite - x)


def _condiciones(capa, pag, condiciones: dict) -> None:
    pal = _palabras(pag)
    # La columna derecha ("Requerimientos Tecnicos") limita lo que se escribe.
    req = _etiqueta(pal, "Requerimientos")
    tope = (req["x0"] - 6) if req else pag.width - 40
    for frases, valor in (
            (("Forma de Pago:",), condiciones.get("forma_pago")),
            (("Garantía:", "Garantia:"), condiciones.get("garantia")),
            (("Ejecución del Servicio :", "Ejecución del Servicio:", "Plazo de Entrega"),
             condiciones.get("plazo_entrega")),
            (("Tipo de Moneda :", "Tipo de Moneda:"), condiciones.get("moneda") or "Soles (S/)"),
            (("Validez de la cotización :", "Validez de la cotización:"),
             condiciones.get("validez")),
            (("Indicar Marca de Procedencia",), "Indicada en cada ítem"),
            (("Tipo de Cambio :", "Tipo de Cambio:"), "No aplica")):
        w = _etiqueta(pal, *frases)
        if w and valor:
            capa.texto(w["x1"] + 4, w["bottom"] - 1.5, str(valor).upper(), 7,
                       ancho_max=max(tope - w["x1"] - 4, 40))


def _firma(capas, paginas, empresa: dict, imagenes: dict) -> None:
    """Bajo "Atentamente;" de la ultima pagina que lo tenga."""
    for n in range(len(paginas) - 1, -1, -1):
        pag = paginas[n]
        aten = next((w for w in _palabras(pag)
                     if normalizar(w["text"]).startswith("atentamente")), None)
        if not aten:
            continue
        capa = capas[n]
        # A la derecha de las condiciones y a la altura de "Atentamente;": bajo
        # el saludo apenas queda margen, y una firma ahi tapa las condiciones.
        cx = pag.width - 135
        linea = min(aten["bottom"] + 58, pag.height - 22)
        if imagenes.get("sello") or imagenes.get("firma"):
            if imagenes.get("sello"):
                capa.imagen(imagenes["sello"], cx, linea + 8, 190, 76)
            if imagenes.get("firma"):
                capa.imagen(imagenes["firma"], cx, linea - 4, 115, 56)
            return
        capa.linea(cx - 95, linea, cx + 95)
        cargo = (empresa.get("cargo_representante") or "Representante legal").upper()
        for k, (t, neg) in enumerate(((empresa.get("representante_legal") or "", True),
                                      (f"DNI N° {empresa.get('dni_representante') or ''}", False),
                                      (cargo, False),
                                      (empresa.get("razon_social") or "", True))):
            capa.texto(cx, linea + 9 + k * 8.5, t, 7, negrita=neg, centro=True, ancho_max=200)
        return
