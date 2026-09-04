"""Cuando tiene que pagarte la entidad, segun la Ley 32069.

POR QUE ESTO Y NO UNA CONSULTA AL SIAF

  La pregunta del proveedor es "cuando me pagan". La respuesta obvia parecia
  consultar el SIAF del MEF, pero su formulario de consulta de expediente exige
  resolver un CAPTCHA (`j_captcha`). No se evade: saltarse un control anti-bot
  de un sistema del Estado no es una opcion tecnica a valorar. La Consulta
  Amigable si esta abierta, pero da ejecucion presupuestal POR ENTIDAD, no el
  estado de la factura de un proveedor.

  Resulta que el dato que de verdad sirve tampoco estaba en el SIAF. Lo que el
  proveedor necesita saber es si la entidad YA SE PASO del plazo, porque eso es
  lo que le da derecho a reclamar. Y ese plazo se calcula, no se consulta.

EL PLAZO, Y DE DONDE SALE

  Ley 32069 (Ley General de Contrataciones Publicas), vigente desde el 22 de
  abril de 2025: la entidad paga dentro de los 10 DIAS HABILES siguientes a la
  CONFORMIDAD, prorrogables 5 dias con justificacion.

  Dos cosas que el codigo anterior tenia mal, y las dos importan:

    - Contaba desde la FECHA DE FACTURA. El plazo corre desde la conformidad,
      que es otro momento y suele ser posterior. Anclarlo mal daba una fecha
      limite que no corresponde a ninguna obligacion real.
    - Usaba 30 dias corridos fijos. Ni son 30 ni son corridos. Un fin de semana
      largo peruano mueve la fecha varios dias, y reclamar antes de tiempo
      quema credito con la entidad igual que reclamar tarde pierde el derecho.

  Para OBRAS y CONSULTORIA DE OBRAS la norma fija plazos propios y distintos
  (la conformidad misma puede tardar hasta 50 dias calendario). Aqui NO se
  inventa un numero para esos casos: se devuelve el plazo como no determinado y
  se le dice al usuario que lo mire en su contrato. Es preferible admitir que
  no lo sabemos a darle una fecha inventada sobre la que reclame.

LOS FERIADOS

  Se calculan, no se listan a mano: una lista fija caduca cada 31 de diciembre y
  el fallo aparece en silencio, con plazos mal contados durante meses. Los 14
  feriados fijos estan por fecha y los dos moviles (Jueves y Viernes Santo)
  salen del computo de la Pascua, que vale para cualquier ano.
"""
import logging
from datetime import date, timedelta
from shared import fechas

log = logging.getLogger("shared.plazos_pago")

# Ley 32069: 10 dias habiles desde la conformidad, prorrogables 5 con
# justificacion. Se usa el plazo base para la fecha limite y la prorroga solo
# para no dar por moroso a quien esta dentro de una ampliacion legitima.
DIAS_HABILES_PAGO = 10
DIAS_HABILES_PRORROGA = 5

# Feriados nacionales de fecha fija (mes, dia).
FERIADOS_FIJOS = {
    (1, 1):   "Año Nuevo",
    (5, 1):   "Día del Trabajo",
    (6, 7):   "Batalla de Arica y Día de la Bandera",
    (6, 29):  "San Pedro y San Pablo",
    (7, 23):  "Día de la Fuerza Aérea",
    (7, 28):  "Fiestas Patrias",
    (7, 29):  "Fiestas Patrias",
    (8, 6):   "Batalla de Junín",
    (8, 30):  "Santa Rosa de Lima",
    (10, 8):  "Combate de Angamos",
    (11, 1):  "Todos los Santos",
    (12, 8):  "Inmaculada Concepción",
    (12, 9):  "Batalla de Ayacucho",
    (12, 25): "Navidad",
}


def _pascua(anio: int) -> date:
    """Domingo de Pascua por el algoritmo anonimo (calendario gregoriano)."""
    a = anio % 19
    b, c = divmod(anio, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(anio, mes, dia + 1)


def feriados_de(anio: int) -> dict[date, str]:
    """Feriados nacionales de ese ano. Incluye los dos moviles de Semana Santa."""
    dias = {date(anio, m, d): nombre for (m, d), nombre in FERIADOS_FIJOS.items()}
    pascua = _pascua(anio)
    dias[pascua - timedelta(days=3)] = "Jueves Santo"
    dias[pascua - timedelta(days=2)] = "Viernes Santo"
    return dias


def es_habil(dia: date) -> bool:
    """Ni sabado, ni domingo, ni feriado nacional."""
    if dia.weekday() >= 5:
        return False
    return dia not in feriados_de(dia.year)


def sumar_dias_habiles(desde: date, dias: int) -> date:
    """Fecha resultante de contar `dias` habiles a partir de `desde`.

    El dia de partida no cuenta: el plazo empieza a correr al dia siguiente de
    la conformidad, que es como se cuentan los plazos administrativos.
    """
    actual = desde
    restantes = dias
    while restantes > 0:
        actual += timedelta(days=1)
        if es_habil(actual):
            restantes -= 1
    return actual


# Categorias tal como las publica OCDS en mainProcurementCategory.
CATEGORIAS_CON_PLAZO_CLARO = frozenset({"goods", "services"})


def plazo_legal(categoria: str | None) -> tuple[int | None, str]:
    """(dias habiles, explicacion). None cuando la norma fija reglas propias.

    Devolver None para obras es deliberado: la Ley 32069 les da plazos distintos
    y la conformidad puede tardar hasta 50 dias calendario. Inventar un numero
    aqui llevaria al proveedor a reclamar fuera de tiempo, que es peor que no
    darle fecha.
    """
    if categoria in CATEGORIAS_CON_PLAZO_CLARO:
        return DIAS_HABILES_PAGO, (
            f"La entidad tiene {DIAS_HABILES_PAGO} días hábiles desde la "
            f"conformidad para pagarte (Ley 32069). Puede ampliarlo "
            f"{DIAS_HABILES_PRORROGA} días más si lo justifica.")
    return None, (
        "En obras y consultoría de obras la ley fija plazos propios y la "
        "conformidad puede demorar. Revisa el plazo pactado en tu contrato: "
        "no queremos darte una fecha que no te sirva para reclamar.")


def fecha_limite_pago(fecha_conformidad: date | None,
                      categoria: str | None) -> date | None:
    """Ultimo dia en que la entidad puede pagar sin estar en mora.

    Sin conformidad no hay fecha, y eso es informacion, no un fallo: el plazo
    todavia no empezo a correr. Devolver una fecha basada en la factura seria
    inventarse el inicio del computo.
    """
    if not fecha_conformidad:
        return None
    dias, _ = plazo_legal(categoria)
    if dias is None:
        return None
    return sumar_dias_habiles(fecha_conformidad, dias)


def dias_de_mora(limite: date | None, hoy: date | None = None) -> int:
    """Dias habiles de retraso. 0 si aun esta en plazo o no hay fecha.

    Se cuentan habiles, igual que el plazo: mezclar corridos con habiles daria
    una mora mayor que la real, y con eso se reclama de mas.
    """
    if not limite:
        return 0
    hoy = hoy or fechas.hoy()
    if hoy <= limite:
        return 0
    dias, actual = 0, limite
    while actual < hoy:
        actual += timedelta(days=1)
        if es_habil(actual):
            dias += 1
    return dias


def en_prorroga(limite: date | None, hoy: date | None = None) -> bool:
    """True si se paso del plazo pero sigue dentro de la ampliacion legal.

    Distinguirlo evita decirle al cliente que reclame cuando la entidad puede
    estar amparada en la prorroga de 5 dias: reclamar ahi le quema credito para
    cuando de verdad tenga razon.
    """
    mora = dias_de_mora(limite, hoy)
    return 0 < mora <= DIAS_HABILES_PRORROGA


def enlace_consulta_mef() -> dict:
    """Datos para que el usuario compruebe el expediente en el MEF por su cuenta.

    No se automatiza porque ese formulario pide un CAPTCHA. Lo util que si
    podemos hacer es llevarlo directo a la pagina y decirle exactamente que
    tres datos necesita, que es la parte que la gente no sabe de memoria.
    """
    return {
        "url": "https://apps2.mef.gob.pe/consulta-vfp-webapp/consultaExpediente.jspx",
        "necesitas": [
            "El año de ejecución del expediente.",
            "El código de Unidad Ejecutora de la entidad (te lo da la propia "
            "entidad; no es el RUC ni el código del SEACE).",
            "El número de expediente SIAF, que figura en la orden de compra o "
            "de servicio.",
        ],
        "nota": ("La consulta oficial del MEF pide un código de verificación, "
                 "así que hay que hacerla a mano. Aquí llevamos la cuenta del "
                 "plazo legal, que es lo que decide si puedes reclamar."),
    }
