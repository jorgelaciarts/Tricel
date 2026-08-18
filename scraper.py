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

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://tricel.lexsoft.cl/tce/estadoDiario"
PAGES_BASE_URL = "https://jorgelaciarts.github.io/Tricel/"

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "docs" / "data"
DATA_FILE = DATA_DIR / "estado_diario.json"
RAW_HTML_FILE = DATA_DIR / "estado_diario_raw.html"
HISTORY_DIR = DATA_DIR / "history"
RESOLUCIONES_DIR = DATA_DIR / "resoluciones"

# Límite de causas nuevas a procesar por ejecución (protección ante
# ejecuciones inesperadamente largas, p. ej. la primera corrida con
# muchas causas históricas).
MAX_CAUSAS_NUEVAS_POR_CORRIDA = 80


def sanitize_filename(text):
    text = re.sub(r"[^\w\-.]", "_", text.strip())
    return text[:100]


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
    'Resolución' y descarga su documento. Devuelve una lista de dicts
    con la ruta relativa del PDF guardado y algunos metadatos de la fila.
    """
    resoluciones = []
    table = find_tramites_table(page)
    if table is None:
        print(f"Aviso: no se encontró la tabla de trámites para la causa {rol}")
        return resoluciones

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
        except Exception as exc:
            print(f"Aviso: no se pudo descargar una resolución de la causa {rol}: {exc}")

    return resoluciones


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
    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(2000)

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
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(1500)
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

        # Para cada causa: si ya la conocíamos, reutilizamos sus resoluciones
        # previas (no volvemos a descargar). Si es nueva, entramos y
        # buscamos resoluciones para descargar.
        for causa in causas:
            rol = causa.get("ROL")
            previamente_vista = previous_rol_index.get(rol) if rol else None

            if previamente_vista is not None:
                causa["Resoluciones"] = previamente_vista.get("Resoluciones", [])
                continue

            if procesadas_esta_corrida >= MAX_CAUSAS_NUEVAS_POR_CORRIDA:
                print(f"Aviso: se alcanzó el límite de {MAX_CAUSAS_NUEVAS_POR_CORRIDA} "
                      f"causas nuevas procesadas en esta corrida; el resto se procesará "
                      f"en la siguiente ejecución diaria.")
                continue

            try:
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_load_state("networkidle", timeout=30000)
                page.wait_for_timeout(1500)

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
                target_detalle_btn.click(timeout=15000)
                page.wait_for_selector("#showDetalle", state="visible", timeout=15000)
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

                resoluciones = download_resoluciones_for_causa(page, rol, fecha)
                causa["Resoluciones"] = resoluciones
                procesadas_esta_corrida += 1

            except Exception as exc:
                print(f"Aviso: error procesando la causa {rol} ({fecha}): {exc}")
                causa["Resoluciones"] = []

            # Pequeña pausa entre causa y causa para no saturar el sitio
            page.wait_for_timeout(800)

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


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    RESOLUCIONES_DIR.mkdir(parents=True, exist_ok=True)

    previous_entries = []
    if DATA_FILE.exists():
        try:
            previous = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            previous_entries = previous.get("entries", [])
        except Exception:
            previous_entries = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        entries = extract_entries(page, previous_entries)
        raw_html = page.content()
        browser.close()

    now_local = datetime.now(timezone.utc).astimezone()
    today = now_local.strftime("%Y-%m-%d")

    snapshot = {
        "source_url": URL,
        "fetched_at": now_local.isoformat(),
        "entries": entries,
    }

    RAW_HTML_FILE.write_text(raw_html, encoding="utf-8")

    changed = previous_entries != entries

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



if __name__ == "__main__":
    main()

