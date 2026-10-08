"""Pruebas del scraper de compras menores del IMA (Gobierno Regional de Cusco).

Ninguna toca la red: se prueba el parser sobre el HTML real de la pagina,
recortado a dos filas. Es lo que se rompe cuando el IMA toque su web, y lo
que la sonda no puede ver: una tabla que sigue respondiendo pero con las
columnas en otro orden saldria como "0 vigentes" y nadie se enteraria.
"""
from radar_bot.scrapers.ima_cusco import _cierre, parsear_listado
from shared import fechas

# Copiado tal cual de s.html el 2026-10-07; solo se recorto el resto de filas.
_HTML = """
<table width="80%" align="center" cellspacing="0" class="tabla">
    <thead><tr><th width="2%">&nbsp;</th><th>DESCRIPCION </th><th>TIPO</th><th>PLAZO</th><th>&nbsp;</th></tr></thead>
    <tbody>
      <tr>
	    <td valign="top"><img src="images/arrow4.png" alt=""/></td>
   	  <td>
		<span class="subtitulo-azul"><strong>SOLICITUD DE COTIZACION N° 2090-2026</strong></span><br /><br />
	    ADQUISICION DE TUBERIAS PVC PARA EL SISTEMA DE RIEGO HUARO<br />
		| Publicado el 06/10/2026 |<br /><br />
        </td>
   	  <td align="center" valign="top"><br /><br /> BIEN</td>
   	  <td align="center" valign="top"><br />
		09/10/2026<br />
		4:30 PM <br />
		(<SPAN STYLE='COLOR:GREEN;'>VIGENTE</SPAN>)
	  </td>
        <td align="center" valign="top"><a href="convmc_v2-files/SCV2_2210.pdf" target="_blank"><br /><br />
        <img src="transparencia-inst/images/ico_pdf3.png" alt="" border="1"/></a></td>
      </tr>
      <tr>
	    <td valign="top"><img src="images/arrow4.png" alt=""/></td>
   	  <td>
		<span class="subtitulo-azul"><strong>SOLICITUD DE COTIZACION N° 2085-2026</strong></span><br /><br />
	    SERVICIO DE ELABORACION DE PLAN DE IMPLEMENTACION DEL BIM<br />
		| Publicado el 09/02/2026 |<br /><br />
        </td>
   	  <td align="center" valign="top"><br /><br /> SERVICIO</td>
   	  <td align="center" valign="top"><br />
		11/02/2026<br />
		4:30 PM <br />
		(<SPAN STYLE='COLOR:RED;'>VENCIDO</SPAN>)
	  </td>
        <td align="center" valign="top"><a href="convmc_v2-files/SCV2_2203.pdf" target="_blank"><br /><br />
        <img src="transparencia-inst/images/ico_pdf3.png" alt="" border="1"/></a></td>
      </tr>
    </tbody>
</table>
"""


def test_solo_entra_la_vigente_y_con_todos_sus_datos():
    filas = parsear_listado(_HTML)
    assert len(filas) == 1
    f = filas[0]
    assert f["fuente"] == "ima_cusco"
    assert f["nomenclatura"] == "SC-2090-2026-IMA"
    assert f["departamento"] == "Cusco"
    assert f["objeto"] == "[BIEN] ADQUISICION DE TUBERIAS PVC PARA EL SISTEMA DE RIEGO HUARO"
    assert f["fecha_publicacion"] == fechas.fija(2026, 10, 6)
    # La hora de cierre se conserva: 4:30 PM es 16:30, no medianoche.
    assert f["fecha_cierre"] == fechas.fija(2026, 10, 9, 16, 30)
    assert f["url"] == "https://www.ima.org.pe/convmc_v2-files/SCV2_2210.pdf"
    assert f["bases_urls"] == [f["url"]]


def test_el_id_es_estable_entre_pasadas():
    """La misma solicitud tiene que caer en la misma fila o se duplica cada hora."""
    a = parsear_listado(_HTML)[0]["id"]
    b = parsear_listado(_HTML)[0]["id"]
    assert a == b and a.startswith("ima_")


def test_sin_tabla_no_hay_filas_ni_excepcion():
    assert parsear_listado("<html><body>Plesk</body></html>") == []


def test_la_hora_de_cierre_se_entiende_en_12_horas():
    assert _cierre("11/02/2026 4:30 PM") == fechas.fija(2026, 2, 11, 16, 30)
    assert _cierre("11/02/2026 12:00 PM") == fechas.fija(2026, 2, 11, 12, 0)
    assert _cierre("11/02/2026 12:15 AM") == fechas.fija(2026, 2, 11, 0, 15)
    assert _cierre("11/02/2026 9:00 a.m.") == fechas.fija(2026, 2, 11, 9, 0)
    # Sin hora: lo mas tarde posible ese dia, nunca una hora inventada.
    assert _cierre("11/02/2026") == fechas.fija(2026, 2, 11, 23, 59)
    assert _cierre("sin fecha") is None
