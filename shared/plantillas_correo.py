"""
Plantillas de correo de LicitaPro.
==================================
Un solo sitio donde se decide como se ve un correo nuestro. Antes cada aviso
traia su HTML a mano en el punto de llamada: el verde #1D9E75 de la buena pro
no es el menta de la marca, win_bot tenia su PROPIA copia de esa plantilla -ya
divergida de la de shared-, y ninguno llevaba marca ni version en texto plano.

POR QUE ESTA ESCRITO ASI Y NO COMO UNA PAGINA NORMAL

El correo se pinta en clientes que van veinte anos por detras del navegador:
Outlook de escritorio usa el motor de Word. De ahi las tres reglas que
gobiernan el archivo entero.

1. TABLAS, NO DIVS. Ni flex ni grid: no existen. Maquetacion con tablas
   anidadas y role="presentation", para que los lectores de pantalla no las
   anuncien como si fueran datos.

2. ESTILOS EN LINEA. Varios clientes de Gmail descartan el <style> del <head>.
   Lo que no vaya en el atributo style de cada etiqueta, no existe.

3. FONDOS EXPLICITOS, Y ADEMAS EN bgcolor. Ver el apartado siguiente.

EL CORREO ES OSCURO, Y ESO OBLIGA A DEFENDERLO

La aplicacion es oscura y estos correos van a juego, con sus mismos tokens.
Pero un correo que YA viene oscuro es mas fragil que uno claro, porque varios
clientes -Outlook.com, Gmail en Android, Apple Mail- aplican su PROPIA
transformacion de modo oscuro encima. Cuando encuentran un diseno claro lo
oscurecen, que es inofensivo; cuando encuentran uno oscuro intentan
ACLARARLO, y ahi el texto claro sobre un fondo aclarado se queda ilegible.

Dos defensas, y hacen falta las dos:

  a) Las metas color-scheme y supported-color-schemes declaran que el mensaje
     ya trae su propio esquema oscuro. Los clientes que las respetan dejan de
     tocar los colores.

  b) El atributo bgcolor ADEMAS del style en cada celda de fondo. Outlook
     respeta bgcolor mucho mejor que background en CSS, y es lo que sostiene
     el diseno en los clientes que ignoran (a).

EL MENTA VUELVE A SER EL DE LA MARCA

Sobre blanco hubo que oscurecer el menta a #1E9E7C para que llegara a 4.5:1.
Sobre este fondo se usa el #34E0B4 de verdad, el de la aplicacion. A cambio,
el texto DENTRO del boton pasa a ser oscuro: blanco sobre menta claro no se
lee, y el acento puede ser tambien ambar o rojo, igual de luminosos.

EL ACENTO ES VARIABLE, Y ESO ES DELIBERADO

Las alertas de plazo cambian de color segun lo que queda: menta con holgura,
ambar a tres dias, rojo a uno. El color es informacion, no adorno, asi que
NUNCA va solo: el titulo dice tambien cuantos dias quedan. Quien no distingue
esos tonos -o lo abre en un cliente que los pisa- lee exactamente lo mismo.

LAS IMAGENES SE BLOQUEAN, Y EL CORREO TIENE QUE AGUANTARLO

Outlook y buena parte de Gmail no cargan imagenes remotas sin permiso. La
marca no vive solo en el logotipo: va sobre la banda oscura con la franja de
acento debajo, lleva alt en blanco -legible sobre ese fondo- y ningun dato
esta unicamente dentro de una imagen.
"""
import html as _html

MARCA = "LicitaPro"
SITIO = "https://licitapro.sisac.pe"
LOGO = SITIO + "/static/correo-licitapro.png"
LOGO_ANCHO = 118

# Los mismos tokens que la aplicacion. El fondo del archivo del logotipo es
# exactamente CABECERA, asi que la imagen funde con la banda sin costura.
MENTA = "#34E0B4"
AMBAR = "#FFB259"
ROJO = "#FF6B60"
TINTA_BOTON = "#06231B"   # texto DENTRO del boton: los tres acentos son claros

LIENZO = "#06080B"        # el fondo, por fuera de la tarjeta
PANEL = "#0E141A"         # la tarjeta
CABECERA = "#0A0D12"      # la banda del logotipo
PIE = "#090E13"
TINTA = "#F2F6F8"         # titulos
TEXTO = "#B9C6CF"         # cuerpo
TENUE = "#7E909C"         # etiquetas y notas
LINEA = "#1A242C"         # separadores
AVISO = "#121A21"         # el recuadro de la nota al pie

TIPO = ("-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,"
        "sans-serif")


class Marcado(str):
    """Texto que YA es HTML y no debe escaparse."""


def _e(v):
    return _html.escape(str(v if v is not None else ""), quote=True)


def _t(v):
    return str(v) if isinstance(v, Marcado) else _e(v)


def _filas(filas, acento):
    """Etiqueta arriba en versalitas, valor debajo en grande.

    En dos columnas, a 320 px -que es un movil- el valor se parte en tres
    lineas o hay que encogerlo hasta lo ilegible. Una fila marcada como
    destacada sale en el color de acento y a mayor tamano: es el dato que
    justifica el correo, normalmente un importe o una fecha limite.
    """
    if not filas:
        return ""
    trozos = []
    for i, fila in enumerate(filas):
        etiqueta, valor = fila[0], fila[1]
        fuerte = len(fila) > 2 and fila[2]
        borde = "" if i == 0 else f"border-top:1px solid {LINEA};"
        estilo_valor = (f"font:700 21px/1.35 {TIPO};color:{acento};"
                        if fuerte else
                        f"font:500 16px/1.45 {TIPO};color:{TINTA};")
        trozos.append(
            f'<tr><td style="{borde}padding:14px 0 12px;">'
            f'<div style="font:600 11px/1.2 {TIPO};letter-spacing:.08em;'
            f'text-transform:uppercase;color:{TENUE};padding-bottom:5px;">{_e(etiqueta)}</div>'
            f'<div style="{estilo_valor}">{_t(valor)}</div></td></tr>')
    return ('<table role="presentation" width="100%" cellpadding="0" '
            'cellspacing="0" border="0" style="margin:26px 0 6px;">'
            + "".join(trozos) + "</table>")


def _boton(boton, acento):
    """Boton a prueba de Outlook: el area pulsable la da la celda, no el <a>.

    El texto va en TINTA_BOTON, no en blanco: los tres acentos posibles son
    colores claros y el blanco encima no se lee.
    """
    if not boton:
        return ""
    return ('<table role="presentation" cellpadding="0" cellspacing="0" '
            'border="0" style="margin:30px 0 8px;"><tr>'
            '<td bgcolor="{}" style="background:{};border-radius:8px;">'
            '<a href="{}" style="display:inline-block;padding:15px 32px;'
            'font:700 15px/1 {};color:{};text-decoration:none;'
            'border-radius:8px;">{}</a></td></tr></table>'.format(acento, acento, _e(boton["url"]), TIPO, TINTA_BOTON,
               _e(boton["texto"])))


def _parrafos(lineas, color=TEXTO, tam=15):
    return "".join(
        f'<p style="margin:0 0 14px;font:400 {tam}px/1.65 {TIPO};color:{color};">'
        f'{_t(l)}</p>' for l in lineas)


def _lista(items, acento):
    if not items:
        return ""
    puntos = "".join(
        f'<li style="margin:0 0 8px;font:400 15px/1.6 {TIPO};color:{TEXTO};">{_t(i)}</li>' for i in items)
    return (f'<div style="margin:26px 0 0;border-top:1px solid {LINEA};'
            'padding-top:20px;">'
            f'<p style="margin:0 0 12px;font:600 12px/1.3 {TIPO};letter-spacing:.08em;'
            f'text-transform:uppercase;color:{acento};">Próximos pasos</p>'
            f'<ul style="margin:0;padding-left:20px;">{puntos}</ul></div>')


def documento(titulo, intro=(), filas=(), boton=None, aviso=None, pasos=(),
              cierre=(), preencabezado="", acento=MENTA):
    oculto = ""
    if preencabezado:
        oculto = ('<div style="display:none;max-height:0;overflow:hidden;'
                  'opacity:0;mso-hide:all;">{}{}</div>'.format(_e(preencabezado), "&#8203;&nbsp;" * 60))

    caja_aviso = ""
    if aviso:
        caja_aviso = ('<table role="presentation" width="100%" cellpadding="0" '
                      'cellspacing="0" border="0" style="margin:24px 0 0;"><tr>'
                      f'<td bgcolor="{AVISO}" style="background:{AVISO};'
                      f'border-left:3px solid {LINEA};padding:14px 16px;">'
                      f'<p style="margin:0;font:400 13px/1.6 {TIPO};color:{TENUE};">{_t(aviso)}</p>'
                      '</td></tr></table>')

    return """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>%(titulo)s</title></head>
<body bgcolor="%(lienzo)s" style="margin:0;padding:0;background:%(lienzo)s;">
%(oculto)s
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0"
       border="0" bgcolor="%(lienzo)s" style="background:%(lienzo)s;">
<tr><td align="center" style="padding:32px 12px;">

  <table role="presentation" width="600" cellpadding="0" cellspacing="0"
         border="0" bgcolor="%(panel)s"
         style="width:100%%;max-width:600px;background:%(panel)s;
         border:1px solid %(linea)s;border-radius:12px;overflow:hidden;">

    <tr><td align="center" bgcolor="%(cabecera)s"
            style="background:%(cabecera)s;padding:24px 24px 20px;">
      <img src="%(logo)s" width="%(logo_ancho)d" alt="%(marca)s"
           style="display:block;border:0;width:%(logo_ancho)dpx;max-width:40%%;
                  height:auto;font:700 18px/1.2 %(tipo)s;color:#FFFFFF;">
    </td></tr>
    <tr><td bgcolor="%(acento)s" style="background:%(acento)s;height:4px;
                   line-height:4px;font-size:0;">&nbsp;</td></tr>

    <tr><td bgcolor="%(panel)s"
            style="padding:34px 36px 30px;background:%(panel)s;">
      <h1 style="margin:0 0 18px;font:700 23px/1.3 %(tipo)s;color:%(tinta)s;
                 letter-spacing:-.01em;">%(titulo)s</h1>
      %(intro)s
      %(filas)s
      %(boton)s
      %(pasos)s
      %(cierre)s
      %(aviso)s
    </td></tr>

    <tr><td bgcolor="%(pie)s" style="background:%(pie)s;
                   border-top:1px solid %(linea)s;padding:22px 36px 26px;">
      <p style="margin:0 0 5px;font:600 13px/1.5 %(tipo)s;
                color:%(texto)s;">%(marca)s Perú</p>
      <p style="margin:0 0 10px;font:400 12px/1.6 %(tipo)s;color:%(tenue)s;">
        Del aviso de la licitación hasta que te pagan &middot;
        <a href="%(sitio)s" style="color:%(tenue)s;text-decoration:underline;"
           >licitapro.sisac.pe</a>
      </p>
      <p style="margin:0;font:400 11px/1.6 %(tipo)s;color:#67777F;">
        Correo automático, no hace falta responder.
        Un producto de Star Insights IT by SISAC.
      </p>
    </td></tr>

  </table>
</td></tr></table>
</body></html>""" % {  # noqa: UP031  (ver el comentario de abajo)
    # El `%` con diccionario se queda a proposito, y no es pereza.
    #
    # Esto es una PLANTILLA: sesenta lineas de HTML de correo con sus huecos
    # nombrados. Pasarla a f-string obliga a interpolar en el sitio, o sea a
    # mezclar el HTML con el codigo que lo rellena, y se pierde justo lo que
    # hace legible este fichero -- que la plantilla se lee como HTML.
    #
    # Ademas el HTML lleva `width="100%%"` en varios sitios: con `%` esos
    # escapes son necesarios y estan puestos; en f-string habria que quitarlos
    # todos y duplicar las llaves de los `style="{...}"`. Mucho ruido a cambio
    # de nada.
        "titulo": _e(titulo), "oculto": oculto, "lienzo": LIENZO,
        "panel": PANEL, "cabecera": CABECERA, "linea": LINEA, "logo": LOGO,
        "logo_ancho": LOGO_ANCHO, "marca": MARCA, "tipo": TIPO,
        "tinta": TINTA, "acento": acento, "texto": TEXTO, "tenue": TENUE,
        "pie": PIE, "sitio": SITIO, "intro": _parrafos(intro),
        "filas": _filas(filas, acento), "boton": _boton(boton, acento),
        "pasos": _lista(pasos, acento), "aviso": caja_aviso,
        "cierre": _parrafos(cierre, color=TENUE, tam=14),
    }


def version_texto(titulo, intro=(), filas=(), boton=None, aviso=None, pasos=(),
                  cierre=(), preencabezado="", acento=None):
    """El correo entero en texto plano, no un resumen: hay clientes que solo
    pintan texto y los filtros penalizan lo que llega solo en HTML."""
    p = [titulo, "=" * len(titulo), ""]
    if intro:
        p += [str(l) for l in intro] + [""]
    if filas:
        p += [f"{f[0]}: {f[1]}" for f in filas] + [""]
    if boton:
        p += ["{}:".format(boton["texto"]), boton["url"], ""]
    if pasos:
        p += ["Próximos pasos:"] + [f"  - {s}" for s in pasos] + [""]
    if cierre:
        p += [str(l) for l in cierre] + [""]
    if aviso:
        p += [str(aviso), ""]
    p += ["-" * 44, f"{MARCA} Perú — {SITIO}",
          "Correo automático, no hace falta responder.",
          "Un producto de Star Insights IT by SISAC."]
    return "\n".join(p)


def componer(**kw):
    """Devuelve (texto, html)."""
    return version_texto(**kw), documento(**kw)
