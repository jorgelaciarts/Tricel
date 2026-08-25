"""
Extrae el "Estado Diario" del Tribunal Calificador de Elecciones (TCE).

El sitio (https://tricel.lexsoft.cl/tce/estadoDiario) es una app Knockout.js
con tres niveles de navegación, todos dentro de la misma SPA (sin URLs
propias, todo vía JavaScript):

  1. Tabla principal: una fila por fecha, con un botón "Detalle" que abre
     un modal (#showDetalle) listando las causas de ese día (ROL, Carátula,
     Número de Trámites).
  2. Dentro del modal, cada causa tiene un botón "Entrar" (openCausa) que
     navega a la vista de esa causa, mostrando su tabla de trámites
     (Trámite | Descargar | Detalles | Referencia | Fecha | Parte |
     Firmado Por | Foja).
  3. Ahí, los trámites de tipo "Resolución" tienen un botón de descarga
     (descargarDocumento) que dispara la descarga de un PDF generado por
     el servidor (sin URL fija -es un evento de descarga del navegador-).

Este script usa Playwright para recorrer los 3 niveles y, cuando encuentra
un trámite "Resolución" en una causa NUEVA (no vista en ejecuciones
anteriores), descarga el PDF y lo guarda en docs/data/resoluciones/,
para poder enlazarlo desde la página publicada.

IMPORTANTE - PRIMER USO:
Los selectores para entrar a una causa y descargar su resolución se
construyeron a partir de fragmentos de HTML que el usuario copió
manualmente desde el navegador, pero no pudieron probarse en vivo antes
de la primera ejecución real en GitHub Actions (no hay acceso de red a
ese dominio desde este entorno). Es muy probable que la primera corrida
necesite ajustes. Revisar:
  - docs/data/estado_diario_raw.html (HTML de la página principal)
  - Los "Aviso:" impresos en el log de la ejecución de GitHub Actions
"""

import copy
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
from playwright.sync_api import sync_playwright

URL = "https://tricel.lexsoft.cl/tce/estadoDiario"
PAGES_BASE_URL = "https://jorgelaciarts.github.io/Tricel/"

# Por defecto el sitio solo muestra los últimos días del Estado Diario.
# Para traer el historial completo se usa el buscador por rango de fecha
# que trae el propio formulario ('Fecha Desde' / 'Fecha Hasta'). Se puede
# sobreescribir sin tocar el código definiendo la variable de entorno
# FECHA_DESDE en el workflow (formato DD-MM-AAAA, igual que el sitio).
FECHA_DESDE_HISTORICA = os.environ.get("FECHA_DESDE", "21-06-2026")

# El filtro por rango de fecha (buscador "Fecha Desde/Hasta" del sitio)
# falló el 100% de las veces en la práctica -el modal de carga
# "Buscando..." se queda pegado en pantalla y nunca se cierra-, así que
# por ahora queda DESACTIVADO por defecto para no romper ni alargar cada
# ejecución diaria. Se puede activar a propósito definiendo la variable
# de entorno ACTIVAR_FILTRO_FECHA=true en el workflow, una vez que se
# entienda por qué el sitio no acepta la búsqueda automatizada tal como
# está implementada.
ACTIVAR_FILTRO_FECHA = os.environ.get("ACTIVAR_FILTRO_FECHA", "false").lower() == "true"

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "docs" / "data"
DATA_FILE = DATA_DIR / "estado_diario.json"
RAW_HTML_FILE = DATA_DIR / "estado_diario_raw.html"
HISTORY_DIR = DATA_DIR / "history"
RESOLUCIONES_DIR = DATA_DIR / "resoluciones"
HISTORICO_MANUAL_DIR = DATA_DIR / "historico_manual"

# Límite de causas nuevas a procesar por ejecución (protección ante
# ejecuciones inesperadamente largas, p. ej. la primera corrida con
# muchas causas históricas).
MAX_CAUSAS_NUEVAS_POR_CORRIDA = 80


def sanitize_filename(text):
    text = re.sub(r"[^\w\-.]", "_", text.strip())
    return text[:100]


def _normalizar_texto(texto):
    return re.sub(r"\s+", " ", texto).strip()


def extraer_texto_pdf(pdf_path):
    """Extrae todo el texto de un PDF (usado para leer el contenido de las Sentencias)."""
    texto = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for pagina in pdf.pages:
                texto += (pagina.extract_text() or "") + "\n"
    except Exception as exc:
        print(f"Aviso: no se pudo leer el texto del PDF {pdf_path}: {exc}")
    return texto


MESES_INGLES_A_NUM = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def extraer_fecha_estado_diario_pdf(texto):
    """
    Extrae la fecha del encabezado 'Santiago, <día de la semana>,  DD de
    <Mes en inglés> de AAAA' que traen los PDF de 'Estado Diario de
    Causas' descargados manualmente del sitio, y la devuelve en formato
    DD-MM-AAAA (igual al resto del sistema).
    """
    t = _normalizar_texto(texto)
    m = re.search(r"Santiago,\s*\w+,\s*(\d{1,2})\s*de\s*(\w+)\s*de\s*(\d{4})", t, re.IGNORECASE)
    if not m:
        return ""
    dia, mes_ingles, anio = m.groups()
    mes_num = MESES_INGLES_A_NUM.get(mes_ingles.lower())
    if not mes_num:
        return ""
    return f"{int(dia):02d}-{mes_num}-{anio}"


def extraer_causas_estado_diario_pdf(texto):
    """
    Extrae la lista de causas (ROL, Carátula/Recurrente) de un PDF
    'Estado Diario de Causas' descargado manualmente desde el sitio
    (botón 'Descargar documento' de la tabla principal, junto a
    'Detalle'). Formato de cada fila: 'N.- ROL RECURRENTES N°RESOL'.

    Algunas filas con carátulas muy largas quedan interrumpidas por el
    diseño del PDF (el texto de la columna vecina se intercala en medio),
    lo que rompe el patrón principal. Para esos casos se captura igual el
    ROL, pero la Carátula queda vacía en vez de intentar adivinar un
    fragmento -un intento anterior de "adivinar" terminaba a veces
    arrastrando texto de la fila siguiente y mezclando causas distintas,
    que es un error peor que simplemente dejarla en blanco-.
    """
    t = _normalizar_texto(texto)
    causas = []
    roles_vistos = set()

    for rol, caratula in re.findall(
        r"\d+\.-\s*([\dA-Za-z]+-20\d{2})\s+(.*?)\.\s*Jurisdiccional\s*\d+", t
    ):
        rol = rol.strip()
        if rol not in roles_vistos:
            causas.append(_nueva_causa_desde_pdf(rol, caratula.strip()))
            roles_vistos.add(rol)

    for orden, rol in re.findall(r"(\d+)\.-\s*([\dA-Za-z]+-20\d{2})", t):
        rol = rol.strip()
        if rol not in roles_vistos:
            causas.append(_nueva_causa_desde_pdf(rol, ""))
            roles_vistos.add(rol)

    return causas


def _nueva_causa_desde_pdf(rol, caratula):
    return {
        "ROL": rol,
        "Caratula": caratula,
        "Numero_Tramites": "",
        "Resoluciones": [],
        "Eleccion": "",
        "Pronunciamiento": "",
        "Materia": "",
        "Estado": "",
        "Solicitud_IA": "",
        "Revisado": False,
    }


def importar_pdfs_historicos(previous_entries):
    """
    Lee los PDF 'Estado Diario de Causas' que el usuario haya subido
    manualmente a docs/data/historico_manual/ (uno por fecha, descargados
    con el botón 'Descargar documento' del sitio) y agrega esas fechas a
    'previous_entries' -sin necesidad de que el navegador automatizado
    visite el sitio para esas fechas-.

    Solo aporta el listado básico de causas (ROL y Carátula); no incluye
    las Resoluciones/Sentencias individuales, ya que esos PDF no las
    traen (habría que descargarlas aparte, causa por causa).

    Si la fecha del PDF YA existía (por ejemplo, se corrige una versión
    anterior con errores de lectura), se vuelve a parsear y actualiza esa
    fecha, causa por causa -pero conservando cualquier causa que ya haya
    sido revisada de verdad (Revisado=True, con sus Resoluciones reales),
    para no pisar datos buenos con el listado básico del PDF-.
    """
    if not HISTORICO_MANUAL_DIR.exists():
        return

    entries_por_fecha = {e.get("Fecha"): e for e in previous_entries if e.get("Fecha")}
    pdfs = sorted(HISTORICO_MANUAL_DIR.glob("*.pdf"))

    for pdf_path in pdfs:
        texto = extraer_texto_pdf(pdf_path)
        if not texto:
            continue

        fecha = extraer_fecha_estado_diario_pdf(texto)
        if not fecha:
            print(f"Aviso: no se pudo determinar la fecha del PDF histórico {pdf_path.name}")
            continue

        causas_nuevas = extraer_causas_estado_diario_pdf(texto)
        if not causas_nuevas:
            print(f"Aviso: no se encontraron causas en el PDF histórico {pdf_path.name} (fecha {fecha})")
            continue

        entry_existente = entries_por_fecha.get(fecha)

        if entry_existente is None:
            nueva_entry = {
                "Fecha": fecha,
                "Numero_Causas": str(len(causas_nuevas)),
                "Numero_Tramites": str(len(causas_nuevas)),
                "Causas": causas_nuevas,
            }
            previous_entries.append(nueva_entry)
            entries_por_fecha[fecha] = nueva_entry
            print(f"Importado desde PDF histórico: {fecha} ({len(causas_nuevas)} causas) — {pdf_path.name}")
            continue

        # La fecha ya existía: volver a parsear y actualizar, conservando
        # las causas que ya tengan datos reales revisados.
        causas_previas_por_rol = {c.get("ROL"): c for c in entry_existente.get("Causas", [])}
        causas_final = []
        hubo_cambios = False
        for causa in causas_nuevas:
            rol = causa.get("ROL")
            previa = causas_previas_por_rol.get(rol)
            if previa is not None and previa.get("Revisado") is True:
                causas_final.append(previa)
            else:
                if previa is None or previa.get("Caratula") != causa.get("Caratula"):
                    hubo_cambios = True
                causas_final.append(causa)

        if hubo_cambios:
            entry_existente["Causas"] = causas_final
            entry_existente["Numero_Causas"] = str(len(causas_final))
            entry_existente["Numero_Tramites"] = str(len(causas_final))
            print(f"Corregido desde PDF histórico: {fecha} ({len(causas_final)} causas) — {pdf_path.name}")


def extraer_eleccion(texto):
    """
    Busca el cargo al que postula el reclamante, con el patrón
    'candidata/candidato al cargo de <CARGO> de la...'. Ej: 'Concejal',
    'Alcalde', 'Gobernador Regional'. Si el documento no menciona una
    candidatura (p. ej. reclamos sobre el propio Servicio Electoral sin
    mencionar el cargo), devuelve cadena vacía.
    """
    t = _normalizar_texto(texto)
    m = re.search(
        r"al cargo de ([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:\s[A-ZÁÉÍÓÚÑa-záéíóúñ]+){0,2}?)\s(?:de la|,|;)",
        t,
    )
    return m.group(1).strip().upper() if m else ""


def extraer_pronunciamiento(texto):
    """
    Busca la frase que sigue a 'Por estas consideraciones,' -donde el
    TCE declara su decisión- y la clasifica en una etiqueta corta.
    """
    t = _normalizar_texto(texto)
    m = re.search(r"[Pp]or estas consideraciones,?\s*(.+?)[,;.]", t)
    if not m:
        return ""
    frase = m.group(1).strip().lower()
    if "acoge" in frase and "parcial" in frase:
        return "ACOGE PARCIAL"
    if "no acoge" in frase or "rechaza" in frase:
        return "RECHAZA"
    if "acoge" in frase:
        return "ACOGE TOTAL"
    if "no ha lugar" in frase:
        return "NO HA LUGAR"
    # Si no calza con ninguna etiqueta conocida, se guarda el fragmento
    # de texto tal cual (recortado) para no perder la información y
    # poder revisarlo manualmente.
    return frase.upper()[:80]


MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

VERBO_POR_PRONUNCIAMIENTO = {
    "ACOGE TOTAL": "se acoge",
    "ACOGE PARCIAL": "se acoge parcialmente",
    "RECHAZA": "se rechaza",
    "NO HA LUGAR": "no se da lugar a",
}

# Temas frecuentes en reclamaciones ante el TCE. Se busca coincidencia
# textual directa antes de intentar capturar una frase genérica, porque
# son más confiables y suelen aparecer en la redacción tal cual.
MATERIAS_CONOCIDAS = [
    "cuenta general de ingresos y gastos electorales",
    "propaganda electoral",
    "gasto electoral",
    "aporte reservado",
    "rendición de cuentas",
    "publicidad electoral",
]


def convertir_fecha_larga(fecha_ddmmaaaa):
    """Convierte 'DD-MM-AAAA' a 'D de <mes> de AAAA'. Si falla, devuelve tal cual."""
    try:
        d, m, a = fecha_ddmmaaaa.split("-")
        return f"{int(d)} de {MESES_ES[int(m) - 1]} de {a}"
    except Exception:
        return fecha_ddmmaaaa


def extraer_nombre_reclamante(texto):
    t = _normalizar_texto(texto)
    m = re.search(r"reclama\s+(?:do[ñn]a|don)\s+([^,]+?),\s*candidat[ao]", t)
    return m.group(1).strip() if m else ""


def extraer_tratamiento(texto):
    """'doña' o 'don', según cómo se refiera el documento al reclamante."""
    t = _normalizar_texto(texto)
    if re.search(r"reclama\s+do[ñn]a", t):
        return "doña"
    if re.search(r"reclama\s+don\b", t):
        return "don"
    return ""


def extraer_comuna(texto):
    t = _normalizar_texto(texto)
    m = re.search(r"de la comuna de ([^,]+),", t)
    return m.group(1).strip() if m else ""


def extraer_cargo_y_region(texto):
    """
    Devuelve (cargo, región). Maneja tanto cargos de nivel comunal
    ('candidata al cargo de Concejal de la comuna de X, Región de Y')
    como cargos de nivel regional, sin comuna
    ('candidato al cargo de Consejero Regional por la Región de X').
    """
    t = _normalizar_texto(texto)
    m = re.search(
        r"al cargo de ([A-ZÁÉÍÓÚÑa-záéíóúñ]+(?:\s[A-ZÁÉÍÓÚÑa-záéíóúñ]+){0,2}?)\s*"
        r"(?:de la comuna de [^,]+,\s*Regi[oó]n de ([^,]+)|"
        r"por la Regi[oó]n(?:\s+de)?\s+([^,]+)|"
        r"Regi[oó]n(?:\s+de)?\s+([^,]+))",
        t,
    )
    if not m:
        return "", ""
    cargo = m.group(1).strip()
    region = next((g for g in m.groups()[1:] if g), "")
    return cargo, region.strip()


def extraer_materia_central(texto):
    t = _normalizar_texto(texto)
    t_lower = t.lower()
    for materia in MATERIAS_CONOCIDAS:
        if materia in t_lower:
            return materia
    m = re.search(
        r"Servicio Electoral,\s*que\s+(?:aprueba|rechaza|objeta)(?:\s+con\s+observaciones)?\s+(.+?),\s*solicitando",
        t,
    )
    return m.group(1).strip() if m else ""


def extraer_numero_resolucion_servel(texto):
    """El número de la resolución ORIGINAL del Servicio Electoral que se reclama (ej. 'G8751')."""
    t = _normalizar_texto(texto)
    m = re.search(r"contra la resolución N[°º]?\s*([A-Za-z]?\d+)", t)
    return m.group(1).strip() if m else ""


def extraer_verbo_materia(texto):
    """El verbo con que el Servicio Electoral resolvió (aprueba/rechaza/objeta), capitalizado."""
    t = _normalizar_texto(texto)
    m = re.search(r"Servicio Electoral,\s*que\s+(aprueba|rechaza|objeta)\b", t, re.IGNORECASE)
    return m.group(1).capitalize() if m else ""


def construir_materia(texto):
    """
    Arma la oración de MATERIA con el formato:
    'Reclamación interpuesta contra la resolución N° X, del Servicio
    Electoral, que <Verbo> la <materia central> de <tratamiento> <nombre>,
    candidato/a al cargo de <cargo> de la comuna de <comuna>, Región de
    <región>.' (o 'candidato/a a <cargo> por la Región <región>' si es un
    cargo regional sin comuna).

    Si faltan datos clave (nombre, materia, número de resolución o verbo),
    devuelve cadena vacía en vez de una oración incompleta.
    """
    t = _normalizar_texto(texto)
    nombre = extraer_nombre_reclamante(t)
    materia_central = extraer_materia_central(t)
    numero_resolucion = extraer_numero_resolucion_servel(t)
    verbo = extraer_verbo_materia(t)
    if not (nombre and materia_central and numero_resolucion and verbo):
        return ""

    tratamiento = extraer_tratamiento(t) or "don/doña"
    comuna = extraer_comuna(t)
    cargo, region = extraer_cargo_y_region(t)

    frase = (f"Reclamación interpuesta contra la resolución N° {numero_resolucion}, "
             f"del Servicio Electoral, que {verbo} la {materia_central} de {tratamiento} {nombre}")
    if cargo:
        sufijo_genero = "a" if tratamiento == "doña" else "o"
        if comuna:
            frase += f", candidat{sufijo_genero} al cargo de {cargo.title()} de la comuna de {comuna}"
            if region:
                frase += f", Región de {region}"
        else:
            frase += f", candidat{sufijo_genero} a {cargo.title()}"
            if region:
                frase += f" por la Región {region}"
    frase += "."
    return frase


# Ejemplos reales que definen el estilo exacto esperado para "Materia".
# Se usan como guía (few-shot) para el modelo de IA.
EJEMPLOS_MATERIA = [
    "Reclamación interpuesta contra la resolución N°G613 del Servicio Electoral que rechazó, por "
    "omisiones graves, la cuenta general de ingresos y gastos electorales de don Álvaro Gonzalo "
    "Bravo Núñez, candidato a Concejal por la comuna de Providencia, en elecciones de 26 y 27 de "
    "octubre de 2024.",
    "Reclamación interpuesta contra la resolución N° G3441 del Servicio Electoral, que Rechaza la "
    "rendición de cuenta de ingresos y gastos electorales de don Sergio Tapia Sandoval, candidato "
    "a Alcalde por la comuna de Tiltil, en elecciones municipales 2024.",
    "Reclamación interpuesta contra la resolución N° G2600 del Servicio Electoral, que Aprueba con "
    "Observaciones la cuenta general de ingresos y gastos electorales de don Tomas Vodanovic "
    "Escudero, candidato a Alcalde por la comuna de Maipú, en elecciones municipales 2024.",
    "Reclamación interpuesta contra la resolución N°G14697 del Servicio Electoral, que Rechaza la "
    "cuenta general de ingresos y gastos electorales de doña Camila Nisleth Jarpa Fernandez, "
    "candidata a Consejera Regional por la Región de Biobío.",
    "Reclamación interpuesta contra la resolución N°G3591 del Servicio Electoral, que Rechaza la "
    "cuenta general de ingresos y gastos electorales de don Julio Cesar Sanzana Cárdenas candidato "
    "a Consejero Regional por la Región de Los Lagos, en las elecciones municipales 2024.",
]


def generar_materia_con_ia(texto_pdf, rol):
    """
    Le pide a un modelo de Claude (vía API) que redacte la oración de
    MATERIA a partir del texto completo de la Sentencia, siguiendo el
    estilo exacto de EJEMPLOS_MATERIA. Esto maneja mucho mejor las
    variaciones de redacción entre documentos (verbos, calificadores como
    "por omisiones graves", distintas formas de mencionar la elección)
    que las reglas de texto fijas.

    Requiere la variable de entorno ANTHROPIC_API_KEY. Si no está
    configurada, o si la llamada falla incluso tras un reintento (red,
    cuota, error transitorio de la API), devuelve cadena vacía sin
    interrumpir la ejecución -el llamador debe usar construir_materia()
    como respaldo en ese caso-.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""

    ejemplos_formateados = "\n".join(f"{i+1}. \"{ej}\"" for i, ej in enumerate(EJEMPLOS_MATERIA))
    texto_recortado = texto_pdf[:6000]

    prompt = (
        "Eres un asistente que redacta la columna \"Materia\" de una planilla de seguimiento "
        "de causas del Tribunal Calificador de Elecciones (TCE) de Chile, a partir del texto "
        "de una Sentencia.\n\n"
        "Sigue este estilo EXACTO, con estos ejemplos reales como referencia:\n\n"
        f"{ejemplos_formateados}\n\n"
        "Notarás que el verbo, los calificadores (como 'por omisiones graves' o 'con "
        "Observaciones') y la forma de mencionar la elección varían según lo que diga cada "
        "documento textualmente -- usa siempre lo que dice el documento, no inventes ni "
        "normalices a una única forma fija.\n\n"
        "Ahora, a partir del siguiente texto de una Sentencia del TCE, escribe UNA sola "
        "oración de Materia, en el mismo estilo. No agregues nada más (sin comillas, sin "
        "explicación, sin encabezados ni notas) -- responde solo con la oración.\n\n"
        f"Texto de la Sentencia (causa ROL {rol}):\n\"\"\"\n{texto_recortado}\n\"\"\""
    )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    ultimo_error = None
    for intento in range(2):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            texto_generado = "".join(
                getattr(block, "text", "") for block in response.content
            ).strip()
            if texto_generado:
                return texto_generado
            ultimo_error = "la IA devolvió una respuesta vacía"
        except Exception as exc:
            ultimo_error = exc
            if intento == 0:
                time.sleep(3)  # espera antes de reintentar, por si es un error pasajero

    print(f"Aviso: no se pudo generar la Materia con IA para la causa {rol} (tras reintento): {ultimo_error}")
    return ""


def esperar_red_inactiva(page, timeout=30000):
    """
    Espera a que la red esté inactiva ('networkidle'), pero sin lanzar una
    excepción si no se alcanza a tiempo -algunos scripts de fondo del
    sitio (p. ej. de Cloudflare) pueden mantener actividad de red
    constante y nunca llegar a un 'networkidle' real-. Si se agota el
    tiempo, el programa sigue adelante igual: como esto se llama siempre
    después de un page.goto(..., wait_until="domcontentloaded"), el
    contenido principal ya está cargado de todas formas.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


def aplicar_filtro_fecha(page, fecha_desde=FECHA_DESDE_HISTORICA, fecha_hasta=None):
    """
    Llena los campos 'Fecha Desde' y 'Fecha Hasta' del formulario de
    búsqueda del Estado Diario y lo envía, para traer resultados
    históricos (por defecto el sitio solo muestra los últimos días).

    IMPORTANTE: ambos campos son obligatorios en el sitio (marcados con
    "*"). La primera versión de esta función solo llenaba 'Fecha Desde' y
    dejaba 'Fecha Hasta' vacío, lo que hacía que la búsqueda fallara en
    silencio siempre (el modal de carga quedaba pegado en pantalla para
    siempre). Se confirmó manualmente que llenando ambos campos con el
    formato DD-MM-AAAA la búsqueda sí funciona.

    Si no se especifica 'fecha_hasta', se usa la fecha de hoy.

    Al enviar el formulario aparece un modal de carga
    (#buscandoestadodiariopublico, "Buscando..."). Hay que esperar a que
    se cierre solo; si por algún motivo queda atascado abierto, se cierra
    a la fuerza vía jQuery/Bootstrap para no dejar su fondo oscuro
    bloqueando todos los clics del resto de la ejecución.
    """
    if not ACTIVAR_FILTRO_FECHA:
        return

    if fecha_hasta is None:
        fecha_hasta = datetime.now(timezone.utc).astimezone().strftime("%d-%m-%Y")

    try:
        campo_desde = page.locator("#datetimepicker1 input")
        campo_desde.click()
        campo_desde.fill("")  # limpiar por si el datepicker puso algo por defecto
        campo_desde.type(fecha_desde, delay=50)  # escritura simulada, tecla por tecla
        campo_desde.press("Tab")

        campo_hasta = page.locator("#datetimepicker2 input")
        campo_hasta.click()
        campo_hasta.fill("")
        campo_hasta.type(fecha_hasta, delay=50)
        campo_hasta.press("Tab")

        # Respaldo: disparar también un evento 'change' nativo por si el
        # datepicker (bootstrap-datetimepicker) no quedó sincronizado con
        # Knockout solo con la escritura simulada.
        try:
            page.evaluate(
                """() => {
                    document.querySelectorAll('#datetimepicker1 input, #datetimepicker2 input')
                        .forEach(el => el.dispatchEvent(new Event('change', { bubbles: true })));
                }"""
            )
        except Exception:
            pass

        page.click("form button[type='submit']")

        # Esperar a que aparezca el modal de carga (puede ser demasiado
        # rápido para alcanzar a verlo, no es un error si no aparece)
        try:
            page.wait_for_selector("#buscandoestadodiariopublico.in", state="visible", timeout=3000)
        except Exception:
            pass

        # Esperar a que se cierre solo (tiempo reducido: si va a fallar,
        # que falle rápido para no perder tanto tiempo por causa)
        page.wait_for_selector("#buscandoestadodiariopublico", state="hidden", timeout=8000)
        page.wait_for_timeout(1000)

    except Exception as exc:
        print(f"Aviso: no se pudo aplicar el filtro de fecha desde {fecha_desde} hasta {fecha_hasta}: {exc}")
        # Seguridad: si el modal de "Buscando..." quedó atascado abierto,
        # forzarlo a cerrar para que no bloquee los clics del resto de la
        # ejecución (aunque en ese caso el filtro histórico puede no
        # haberse aplicado, y el resto de la corrida sigue con lo que el
        # sitio muestre por defecto).
        try:
            page.evaluate(
                "if (window.jQuery) { "
                "jQuery('#buscandoestadodiariopublico').modal('hide'); "
                "jQuery('.modal-backdrop').remove(); "
                "jQuery('body').removeClass('modal-open'); "
                "}"
            )
            page.wait_for_timeout(500)
        except Exception:
            pass


def extract_causas_from_modal(page):
    """Lee las filas de la tabla del modal 'Información de Causa' (#showDetalle)."""
    causas = []
    causa_rows = page.query_selector_all("#showDetalle tbody tr")
    for crow in causa_rows:
        cells = [c.inner_text().strip() for c in crow.query_selector_all("td")]
        if not cells or not any(cells):
            continue
        causas.append({
            "ROL": cells[0] if len(cells) > 0 else "",
            "Caratula": cells[1] if len(cells) > 1 else "",
            "Numero_Tramites": cells[2] if len(cells) > 2 else "",
            "Resoluciones": [],
        })
    return causas


def find_tramites_table(page):
    """
    Tras entrar a una causa, busca la tabla de trámites (la que tiene
    columnas 'Trámite' y 'Descargar'). Se identifica por su texto de
    encabezado, ya que no conocemos su id/clase exacta.
    """
    try:
        page.wait_for_function(
            """() => {
                const tables = document.querySelectorAll('table');
                for (const t of tables) {
                    const head = t.innerText.slice(0, 300);
                    if (head.includes('Trámite') && head.includes('Descargar')) return true;
                }
                return false;
            }""",
            timeout=15000,
        )
    except Exception:
        return None

    tables = page.query_selector_all("table")
    for t in tables:
        head = t.inner_text()[:300]
        if "Trámite" in head and "Descargar" in head:
            return t
    return None


def download_resoluciones_for_causa(page, rol, fecha):
    """
    Dentro de la vista de una causa ya abierta, busca trámites tipo
    'Resolución' y descarga su documento. Si el trámite es de tipo
    'Sentencia', además lee el texto del PDF para extraer ELECCIÓN,
    PRONUNCIAMIENTO y MATERIA.

    Devuelve (resoluciones, eleccion, pronunciamiento, materia).
    """
    resoluciones = []
    eleccion = ""
    pronunciamiento = ""
    materia = ""

    table = find_tramites_table(page)
    if table is None:
        print(f"Aviso: no se encontró la tabla de trámites para la causa {rol}")
        return resoluciones, eleccion, pronunciamiento, materia

    rows = table.query_selector_all("tbody tr")
    for idx, row in enumerate(rows):
        cells = row.query_selector_all("td")
        if not cells:
            continue
        tramite_tipo = cells[0].inner_text().strip()
        if tramite_tipo.strip().lower() not in ("resolución", "resolucion"):
            continue

        download_span = row.query_selector("span[title='Descargar Documento']")
        if download_span is None:
            continue

        try:
            with page.expect_download(timeout=20000) as download_info:
                download_span.click()
            download = download_info.value

            referencia = cells[3].inner_text().strip() if len(cells) > 3 else ""
            fecha_tramite = cells[4].inner_text().strip() if len(cells) > 4 else ""

            filename = f"{sanitize_filename(rol)}_{sanitize_filename(fecha_tramite or fecha)}_{idx}.pdf"
            dest = RESOLUCIONES_DIR / filename
            download.save_as(str(dest))

            resoluciones.append({
                "archivo_relativo": f"data/resoluciones/{filename}",
                "url_publica": PAGES_BASE_URL + f"data/resoluciones/{filename}",
                "referencia": referencia,
                "fecha_tramite": fecha_tramite,
            })

            # Solo se lee el contenido de las "Sentencia" (ahí está la
            # decisión final del caso); "Téngase presente", "Dese cuenta",
            # etc. son trámites intermedios sin pronunciamiento de fondo.
            if referencia.strip().lower() == "sentencia":
                texto_pdf = extraer_texto_pdf(dest)
                if texto_pdf:
                    eleccion_encontrada = extraer_eleccion(texto_pdf)
                    pronunciamiento_encontrado = extraer_pronunciamiento(texto_pdf)
                    if eleccion_encontrada:
                        eleccion = eleccion_encontrada
                    if pronunciamiento_encontrado:
                        pronunciamiento = pronunciamiento_encontrado
                    materia_encontrada = generar_materia_con_ia(texto_pdf, rol) or construir_materia(texto_pdf)
                    if materia_encontrada:
                        materia = materia_encontrada

        except Exception as exc:
            print(f"Aviso: no se pudo descargar una resolución de la causa {rol}: {exc}")

    return resoluciones, eleccion, pronunciamiento, materia


def build_previous_rol_index(previous_entries):
    """
    Arma un índice {ROL: causa_dict} con todas las causas ya vistas en
    la corrida anterior, para no reprocesar (ni redescargar) lo mismo.
    """
    index = {}
    for entry in previous_entries:
        for causa in entry.get("Causas", []) or []:
            rol = causa.get("ROL")
            if rol:
                index[rol] = causa
    return index


def backfill_missing_fields(previous_entries):
    """
    Corrige causas que ya fueron procesadas (Revisado=True) ANTES de que
    existiera la lectura de PDF (o cuya extracción no encontró nada la
    primera vez), pero que ya tienen el PDF de la Sentencia guardado en
    el repo. En vez de volver a visitar el sitio web, vuelve a leer ese
    PDF local -mucho más rápido y sin riesgo de fallar por el sitio-.

    Modifica 'previous_entries' en el sitio (in-place).
    """
    hoy = datetime.now(timezone.utc).astimezone().strftime("%d-%m-%Y")
    corregidas = 0

    for entry in previous_entries:
        for causa in entry.get("Causas", []) or []:
            rol_debug = causa.get("ROL", "?")

            if not causa.get("Revisado"):
                print(f"Backfill: {rol_debug} -> se salta (Revisado no es True)")
                continue

            resoluciones = causa.get("Resoluciones") or []
            sentencia = next(
                (r for r in resoluciones if r.get("referencia", "").strip().lower() == "sentencia"),
                None,
            )
            if not sentencia:
                print(f"Backfill: {rol_debug} -> se salta (no tiene un trámite 'Sentencia' registrado)")
                continue

            archivo_relativo = sentencia.get("archivo_relativo")
            if not archivo_relativo:
                print(f"Backfill: {rol_debug} -> se salta (la Sentencia no tiene archivo_relativo registrado)")
                continue

            pdf_path = ROOT / "docs" / archivo_relativo
            if not pdf_path.exists():
                print(f"Backfill: {rol_debug} -> se salta (no se encontró el PDF en el checkout: {pdf_path})")
                continue

            # MATERIA se recalcula siempre (aunque ya tuviera un valor):
            # es una simple relectura del PDF ya descargado, así que es
            # barato, y garantiza que si se ajusta la plantilla, los datos
            # existentes se actualicen solos en la próxima corrida.
            if causa.get("Eleccion") and causa.get("Pronunciamiento"):
                texto_pdf = extraer_texto_pdf(pdf_path)
                if texto_pdf:
                    rol_causa = causa.get("ROL", "")
                    nueva_materia = generar_materia_con_ia(texto_pdf, rol_causa) or construir_materia(texto_pdf)
                    print(f"Backfill: {rol_debug} -> Materia recalculada: {nueva_materia[:80] if nueva_materia else '(vacía)'}...")
                    if nueva_materia and nueva_materia != causa.get("Materia"):
                        causa["Materia"] = nueva_materia
                        corregidas += 1
                else:
                    print(f"Backfill: {rol_debug} -> no se pudo leer texto del PDF {pdf_path}")
                continue

            texto_pdf = extraer_texto_pdf(pdf_path)
            if not texto_pdf:
                print(f"Backfill: {rol_debug} -> no se pudo leer texto del PDF {pdf_path}")
                continue

            eleccion = extraer_eleccion(texto_pdf)
            pronunciamiento = extraer_pronunciamiento(texto_pdf)
            materia = generar_materia_con_ia(texto_pdf, causa.get("ROL", "")) or construir_materia(texto_pdf)
            print(f"Backfill: {rol_debug} -> Eleccion={eleccion!r} Pronunciamiento={pronunciamiento!r} "
                  f"Materia={(materia[:80] + '...') if materia else '(vacía)'}")

            if eleccion and not causa.get("Eleccion"):
                causa["Eleccion"] = eleccion
                corregidas += 1
            if pronunciamiento and not causa.get("Pronunciamiento"):
                causa["Pronunciamiento"] = pronunciamiento
            if materia:
                causa["Materia"] = materia
            if not causa.get("Estado"):
                causa["Estado"] = "Con sentencia"
            if not causa.get("Solicitud_IA"):
                causa["Solicitud_IA"] = hoy

    if corregidas:
        print(f"Backfill: se completaron datos de {corregidas} causa(s) ya procesadas "
              f"leyendo su PDF ya descargado (sin volver a visitar el sitio).")


def extract_entries(page, previous_entries):
    """
    Extrae las filas de la tabla principal (una por fecha). Para cada
    fecha, abre el modal de "Detalle" para listar sus causas. Para cada
    causa NUEVA (ROL no visto en la corrida anterior), entra a la causa y
    descarga los documentos de sus trámites tipo "Resolución".

    Se recarga la página completa (page.goto) antes de procesar cada
    fecha Y antes de procesar cada causa nueva, para partir siempre de un
    estado limpio: tras muchas interacciones seguidas (abrir modales,
    entrar a causas, descargar documentos) la SPA puede quedar en un
    estado inesperado (p. ej. una ventana de "Cargando..." tapando los
    botones), lo que hacía fallar el clic de la fecha siguiente.
    """
    esperar_red_inactiva(page)
    page.wait_for_timeout(2000)
    aplicar_filtro_fecha(page)

    previous_rol_index = build_previous_rol_index(previous_entries)
    procesadas_esta_corrida = 0

    # Primero, solo recolectar la lista de fechas (sin entrar a ninguna),
    # para saber cuántas hay y sus textos exactos.
    main_rows = page.query_selector_all("table#selectable tbody tr")
    fechas = []
    for row in main_rows:
        cells = row.query_selector_all("td")
        if len(cells) >= 3:
            fechas.append(cells[0].inner_text().strip())

    if not fechas:
        body_text = page.inner_text("body")
        return [{"texto_bruto": body_text}]

    entries = []

    for fecha in fechas:
        # Estado limpio antes de cada fecha
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            esperar_red_inactiva(page)
            page.wait_for_timeout(1500)
            aplicar_filtro_fecha(page)
        except Exception as exc:
            print(f"Aviso: no se pudo recargar la página antes de procesar la fecha {fecha}: {exc}")
            continue

        rows = page.query_selector_all("table#selectable tbody tr")
        target_row = None
        numero_causas = numero_tramites = ""
        for r in rows:
            rcells = r.query_selector_all("td")
            if rcells and rcells[0].inner_text().strip() == fecha:
                target_row = r
                numero_causas = rcells[1].inner_text().strip() if len(rcells) > 1 else ""
                numero_tramites = rcells[2].inner_text().strip() if len(rcells) > 2 else ""
                break

        if target_row is None:
            print(f"Aviso: no se pudo re-ubicar la fecha {fecha} en la tabla principal")
            continue

        causas = []
        detalle_btn = target_row.query_selector("td:nth-child(4) span")
        if detalle_btn:
            try:
                detalle_btn.click(timeout=15000)
                page.wait_for_selector("#showDetalle", state="visible", timeout=15000)
                page.wait_for_timeout(1200)
                causas = extract_causas_from_modal(page)
            except Exception as exc:
                print(f"Aviso: no se pudo abrir el detalle de la fecha {fecha}: {exc}")

        # Marcar todas las causas de este resultado como "no revisadas" por
        # defecto; solo pasan a "Revisado": True si la descarga se
        # completó sin errores. Así, si algo falla, se reintenta al día
        # siguiente en vez de quedar saltado para siempre.
        for causa in causas:
            causa.setdefault("Revisado", False)

        # Para cada causa: si ya la conocíamos Y quedó marcada como
        # "Revisado" (la descarga se completó bien), reutilizamos sus
        # resoluciones previas sin volver a intentarlo. Si es nueva O si
        # antes falló (Revisado=False), la procesamos (o reintentamos).
        for causa in causas:
            rol = causa.get("ROL")
            previamente_vista = previous_rol_index.get(rol) if rol else None

            if previamente_vista is not None and previamente_vista.get("Revisado") is True:
                causa["Resoluciones"] = previamente_vista.get("Resoluciones", [])
                causa["Eleccion"] = previamente_vista.get("Eleccion", "")
                causa["Pronunciamiento"] = previamente_vista.get("Pronunciamiento", "")
                causa["Materia"] = previamente_vista.get("Materia", "")
                causa["Estado"] = previamente_vista.get("Estado", "")
                causa["Solicitud_IA"] = previamente_vista.get("Solicitud_IA", "")
                causa["Revisado"] = True
                continue

            if procesadas_esta_corrida >= MAX_CAUSAS_NUEVAS_POR_CORRIDA:
                print(f"Aviso: se alcanzó el límite de {MAX_CAUSAS_NUEVAS_POR_CORRIDA} "
                      f"causas nuevas/pendientes procesadas en esta corrida; el resto se "
                      f"reintentará en la siguiente ejecución diaria.")
                continue

            try:
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                esperar_red_inactiva(page)
                page.wait_for_timeout(1500)
                aplicar_filtro_fecha(page)

                fresh_rows = page.query_selector_all("table#selectable tbody tr")
                fresh_target_row = None
                for r in fresh_rows:
                    rcells = r.query_selector_all("td")
                    if rcells and rcells[0].inner_text().strip() == fecha:
                        fresh_target_row = r
                        break
                if fresh_target_row is None:
                    print(f"Aviso: no se pudo re-ubicar la fecha {fecha} para entrar a la causa {rol}")
                    continue

                target_detalle_btn = fresh_target_row.query_selector("td:nth-child(4) span")

                # Reintento: si el modal no abre a la primera (posible
                # lentitud del servidor bajo automatización intensa),
                # se espera un poco más y se prueba una segunda vez.
                modal_abierto = False
                for intento in range(2):
                    try:
                        target_detalle_btn.click(timeout=15000)
                        page.wait_for_selector("#showDetalle", state="visible", timeout=15000)
                        modal_abierto = True
                        break
                    except Exception:
                        if intento == 0:
                            print(f"Aviso: reintentando abrir el detalle para la causa {rol}...")
                            page.wait_for_timeout(3000)
                        else:
                            raise
                if not modal_abierto:
                    continue

                page.wait_for_timeout(1000)

                entrar_btn = None
                modal_rows = page.query_selector_all("#showDetalle tbody tr")
                for mrow in modal_rows:
                    mcells = mrow.query_selector_all("td")
                    if mcells and mcells[0].inner_text().strip() == rol:
                        entrar_btn = mrow.query_selector("span.glyphicon-new-window")
                        break

                if entrar_btn is None:
                    print(f"Aviso: no se encontró el botón 'Entrar' para la causa {rol}")
                    continue

                entrar_btn.click(timeout=15000)
                page.wait_for_timeout(2000)

                resoluciones, eleccion, pronunciamiento, materia = download_resoluciones_for_causa(page, rol, fecha)
                causa["Resoluciones"] = resoluciones
                causa["Eleccion"] = eleccion
                causa["Pronunciamiento"] = pronunciamiento
                causa["Materia"] = materia
                causa["Estado"] = "Con sentencia" if any(
                    r.get("referencia", "").strip().lower() == "sentencia" for r in resoluciones
                ) else "En trámite"
                causa["Solicitud_IA"] = datetime.now(timezone.utc).astimezone().strftime("%d-%m-%Y")
                causa["Revisado"] = True
                procesadas_esta_corrida += 1

            except Exception as exc:
                print(f"Aviso: error procesando la causa {rol} ({fecha}): {exc}")
                causa["Resoluciones"] = causa.get("Resoluciones", [])
                causa["Eleccion"] = causa.get("Eleccion", "")
                causa["Pronunciamiento"] = causa.get("Pronunciamiento", "")
                causa["Materia"] = causa.get("Materia", "")
                causa["Estado"] = causa.get("Estado", "")
                causa["Solicitud_IA"] = causa.get("Solicitud_IA", "")
                causa["Revisado"] = False

            # Pausa entre causa y causa para no saturar el sitio
            page.wait_for_timeout(2500)

        entries.append({
            "Fecha": fecha,
            "Numero_Causas": numero_causas,
            "Numero_Tramites": numero_tramites,
            "Causas": causas,
        })

    if not entries:
        body_text = page.inner_text("body")
        entries = [{"texto_bruto": body_text}]

    return entries


def format_entry(entry, max_len=300):
    """Convierte una entrada (dict) en una línea de texto legible para el correo."""
    if "texto_bruto" in entry:
        snippet = entry["texto_bruto"].strip().replace("\n", " ")[:max_len]
        return f"(texto sin estructurar) {snippet}..."

    if "Fecha" in entry:
        header = (f"{entry.get('Fecha', '?')} — "
                  f"{entry.get('Numero_Causas', '?')} causas, "
                  f"{entry.get('Numero_Tramites', '?')} trámites")
        causas = entry.get("Causas") or []
        if causas:
            lines = []
            for c in causas[:5]:
                line = f"{c.get('ROL', '')} {c.get('Caratula', '')}".strip()
                if c.get("Eleccion") or c.get("Pronunciamiento") or c.get("Estado"):
                    line += (f" — Elección: {c.get('Eleccion', '') or 's/i'} | "
                             f"Pronunciamiento: {c.get('Pronunciamiento', '') or 's/i'} | "
                             f"Estado: {c.get('Estado', '') or 's/i'}")
                resoluciones = c.get("Resoluciones") or []
                if resoluciones:
                    partes = []
                    for r in resoluciones:
                        ref = r.get("referencia") or "Resolución"
                        fecha_r = r.get("fecha_tramite") or ""
                        partes.append(f"{ref} ({fecha_r}): {r['url_publica']}")
                    line += " [" + " | ".join(partes) + "]"
                lines.append(line)
            sample = "; ".join(lines)
            if len(causas) > 5:
                sample += f" ... y {len(causas) - 5} causas más"
            return f"{header}\n      Causas: {sample}"
        return header + " (sin detalle de causas disponible)"

    parts = []
    for key, value in entry.items():
        if isinstance(value, list):
            value = " · ".join(str(v) for v in value)
        value = str(value).strip()
        if value:
            parts.append(f"{key}: {value}")
    line = " | ".join(parts)
    return line[:max_len] + ("..." if len(line) > max_len else "")


def build_summary(previous_entries, new_entries):
    """
    Resumen legible para el correo. Si las entradas tienen 'Fecha' (caso
    normal), compara por fecha: qué fechas son nuevas respecto de la
    última corrida. Si no (modo de respaldo con texto_bruto), compara por
    contenido completo.
    """
    total = len(new_entries)
    lines = [f"Total de fechas de Estado Diario encontradas: {total}", ""]

    has_fechas = any("Fecha" in e for e in new_entries)

    if has_fechas:
        previous_fechas = {e.get("Fecha") for e in previous_entries if "Fecha" in e}
        added = [e for e in new_entries if e.get("Fecha") not in previous_fechas]

        # Además de fechas nuevas, avisar de resoluciones nuevas en fechas
        # ya conocidas (causas nuevas agregadas a un día ya existente).
        previous_rol_index = build_previous_rol_index(previous_entries)
        nuevas_resoluciones = []
        for e in new_entries:
            for c in e.get("Causas", []) or []:
                rol = c.get("ROL")
                if rol and rol not in previous_rol_index and c.get("Resoluciones"):
                    nuevas_resoluciones.append((e.get("Fecha"), c))
    else:
        previous_serialized = {json.dumps(e, ensure_ascii=False, sort_keys=True) for e in previous_entries}
        added = [
            e for e in new_entries
            if json.dumps(e, ensure_ascii=False, sort_keys=True) not in previous_serialized
        ]
        nuevas_resoluciones = []

    if added:
        lines.append(f"Fechas nuevas ({len(added)}):")
        for e in added[:20]:
            lines.append(f"  - {format_entry(e)}")
        if len(added) > 20:
            lines.append(f"  ... y {len(added) - 20} más. Revisa la página completa para verlas todas.")
    else:
        lines.append("No se detectaron fechas nuevas respecto de la última revisión.")

    if nuevas_resoluciones:
        lines.append("")
        lines.append(f"Resoluciones nuevas disponibles para descarga ({len(nuevas_resoluciones)}):")
        for fecha, c in nuevas_resoluciones[:20]:
            for r in c.get("Resoluciones", []):
                ref = r.get("referencia") or "Resolución"
                fecha_r = r.get("fecha_tramite") or ""
                lines.append(f"  - {fecha} · {c.get('ROL')} {c.get('Caratula')} · {ref} ({fecha_r}): {r['url_publica']}")

    return "\n".join(lines)


def extraer_datos_para_registro(causa):
    """
    A partir de una causa ya procesada (con 'Materia' ya generada), arma
    la fila para agregar al Registro Cúmplase: ROL, Nombre, N° de
    Resolución del Servicio Electoral, Elección y Pronunciamiento.

    NOTA: el RUT del reclamante no aparece en ningún lugar del texto de
    las Sentencias (se verificó directamente), así que no se puede
    extraer automáticamente -queda vacío para completarse a mano si se
    necesita-.
    """
    materia = causa.get("Materia", "") or ""

    n_res = ""
    m = re.search(r"resoluci[oó]n N[°º]?\s*([A-Za-z]?\d+)", materia, re.IGNORECASE)
    if m:
        n_res = m.group(1).strip()

    nombre = ""
    m2 = re.search(r"de (?:don|do[ñn]a)\s+([^,]+?),\s*candidat[oa]", materia, re.IGNORECASE)
    if m2:
        nombre = m2.group(1).strip()
    else:
        m3 = re.search(r"de (?:don|do[ñn]a)\s+([^\.]+?)\.$", materia, re.IGNORECASE)
        if m3:
            nombre = m3.group(1).strip()

    return {
        "Índice": "",
        "RUT": "",
        "NOMBRE CANDIDATO": nombre.upper() if nombre else "",
        "N° RES": n_res,
        "ELECCIÓN": causa.get("Eleccion", "") or "",
        "PRONUNCIAMIENTO": causa.get("Pronunciamiento", "") or "",
        "CAUSA TRICEL - ROL": causa.get("ROL", ""),
        "Estado Contabilidad": "",
        "Responsable Contabilidad": "",
        "Comentario Conta": "",
        "Comentario Juridico": "",
        "Finalizado": False,
        "Fecha Agregado": datetime.now(timezone.utc).astimezone().strftime("%d-%m-%Y"),
    }


def actualizar_registro_cumplase(entries):
    """
    Revisa todas las causas ya procesadas (con Materia generada) cuya
    Carátula mencione 'Servicio Electoral', y las agrega automáticamente
    al Registro Cúmplase (docs/data/registro_cumplase.json) si todavía no
    estaban ahí -sin pisar registros existentes, para no perder el
    seguimiento manual (Estado Contabilidad, Responsable, comentarios,
    Finalizado) ya cargado a mano-.
    """
    registro_path = DATA_DIR / "registro_cumplase.json"
    registro = []
    if registro_path.exists():
        try:
            registro = json.loads(registro_path.read_text(encoding="utf-8"))
        except Exception:
            registro = []

    roles_existentes = {str(r.get("CAUSA TRICEL - ROL", "")).strip() for r in registro}
    agregados = 0

    for entry in entries:
        for causa in entry.get("Causas", []) or []:
            rol = str(causa.get("ROL", "")).strip()
            if not rol or rol in roles_existentes:
                continue
            caratula = (causa.get("Caratula") or "").lower()
            if "servicio electoral" not in caratula:
                continue
            if not causa.get("Materia"):
                continue  # todavía no se ha procesado su Sentencia

            registro.append(extraer_datos_para_registro(causa))
            roles_existentes.add(rol)
            agregados += 1

    if agregados:
        registro_path.write_text(
            json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Registro Cúmplase: se agregaron {agregados} causa(s) nueva(s) automáticamente.")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    RESOLUCIONES_DIR.mkdir(parents=True, exist_ok=True)
    HISTORICO_MANUAL_DIR.mkdir(parents=True, exist_ok=True)

    previous_entries = []
    if DATA_FILE.exists():
        try:
            previous = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            previous_entries = previous.get("entries", [])
        except Exception:
            previous_entries = []

    # Copia "antes del backfill" -se usa más abajo para detectar
    # correctamente si el backfill cambió algo (Materia, Elección, etc.),
    # ya que 'previous_entries' se modifica en el sitio dentro de
    # backfill_missing_fields() y luego 'entries' se reconstruye a partir
    # de esos mismos datos ya corregidos -comparar uno contra el otro
    # directamente siempre daría "sin cambios", aunque el backfill sí
    # haya corregido datos que nunca llegarían a guardarse en el repo.
    previous_entries_antes_del_backfill = copy.deepcopy(previous_entries)

    backfill_missing_fields(previous_entries)
    importar_pdfs_historicos(previous_entries)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        entries_del_sitio = extract_entries(page, previous_entries)
        raw_html = page.content()
        browser.close()

    # IMPORTANTE: el sitio solo muestra por defecto los últimos días del
    # Estado Diario -las fechas más antiguas van "desapareciendo" de esa
    # vista con el tiempo-. 'entries_del_sitio' solo trae lo que el sitio
    # muestra HOY. Si simplemente reemplazáramos todo con eso, las fechas
    # antiguas que ya no aparecen por defecto (o las importadas a mano
    # desde PDF) se perderían silenciosamente en cada corrida. Por eso se
    # combinan con lo que ya había en 'previous_entries', dando prioridad
    # a la versión más reciente de cada fecha cuando existen ambas.
    entries_por_fecha = {e.get("Fecha"): e for e in previous_entries if e.get("Fecha")}
    for entry in entries_del_sitio:
        fecha = entry.get("Fecha")
        if not fecha:
            # Modo de respaldo (texto_bruto): no tiene fecha real, no se
            # puede fusionar por fecha -se descarta del snapshot final
            # para no dejar una entrada fantasma sin fecha en la página-.
            continue
        entries_por_fecha[fecha] = entry
    entries = sorted(
        entries_por_fecha.values(),
        key=lambda e: e.get("Fecha", ""),
        reverse=True,
    )

    actualizar_registro_cumplase(entries)

    now_local = datetime.now(timezone.utc).astimezone()
    today = now_local.strftime("%Y-%m-%d")

    snapshot = {
        "source_url": URL,
        "fetched_at": now_local.isoformat(),
        "entries": entries,
    }

    RAW_HTML_FILE.write_text(raw_html, encoding="utf-8")

    changed = previous_entries_antes_del_backfill != entries

    DATA_FILE.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (HISTORY_DIR / f"{today}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = build_summary(previous_entries, entries)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write("summary<<EOF_SUMMARY\n")
            f.write(summary + "\n")
            f.write("EOF_SUMMARY\n")

    print(f"Entradas extraídas: {len(entries)} | Cambió respecto de ayer: {changed}")
    print(summary)


if __name__ == "__main__":
    main()
