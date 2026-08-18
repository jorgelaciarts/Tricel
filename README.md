# Estado Diario · Tribunal Calificador de Elecciones (TCE)

Extrae automáticamente, una vez al día, el contenido de
[`https://tricel.lexsoft.cl/tce/estadoDiario`](https://tricel.lexsoft.cl/tce/estadoDiario),
lo publica en una página de GitHub Pages y te avisa por correo cuando hay
novedades.

## Cómo funciona

1. Un **GitHub Action** programado (`.github/workflows/scrape.yml`) corre
   `scraper.py` una vez al día.
2. `scraper.py` usa **Playwright** (navegador headless) para renderizar la
   página — es necesario porque el sitio carga el contenido con
   JavaScript, no sirve una descarga simple de HTML.
3. El resultado se guarda en `docs/data/estado_diario.json` (snapshot
   actual) y en `docs/data/history/AAAA-MM-DD.json` (histórico).
4. Si el contenido cambió respecto del día anterior, el workflow:
   - hace commit de los nuevos datos al repositorio, y
   - te envía un correo automático.
5. `docs/index.html` (servido por GitHub Pages) lee ese JSON y muestra la
   tabla más reciente.

## Configuración inicial (una sola vez)

1. **Crea el repositorio en GitHub** y sube estos archivos:
   ```bash
   cd tricel-estado-diario
   git init
   git add .
   git commit -m "Setup inicial"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
   git push -u origin main
   ```

2. **Activa GitHub Pages**:
   - Ve a *Settings → Pages* en tu repositorio.
   - En "Source" elige la rama `main` y la carpeta `/docs`.
   - Guarda. Tu sitio quedará en `https://TU_USUARIO.github.io/TU_REPO/`.

3. **Configura el envío de correo** (Settings → Secrets and variables →
   Actions → *New repository secret*). Si usas Gmail:
   - `MAIL_SERVER` → `smtp.gmail.com`
   - `MAIL_PORT` → `465`
   - `MAIL_USERNAME` → tu correo de Gmail
   - `MAIL_PASSWORD` → una [contraseña de aplicación](https://myaccount.google.com/apppasswords)
     (no tu contraseña normal; necesitas verificación en 2 pasos activada)
   - `MAIL_TO` → el correo donde quieres recibir el aviso

   Si usas otro proveedor de correo, ajusta `MAIL_SERVER`/`MAIL_PORT` según
   sus datos SMTP.

4. **Ejecuta el workflow manualmente la primera vez** para probar que todo
   funciona, sin esperar al cron diario:
   - Ve a la pestaña *Actions* del repositorio.
   - Elige el workflow "Scrape Estado Diario TCE".
   - Haz clic en *Run workflow*.

## Ajuste importante después de la primera ejecución

No fue posible ver de antemano cómo se ve exactamente la tabla renderizada
del Estado Diario (el sitio requiere ejecutar JavaScript, algo que no pude
hacer al preparar este proyecto). Por eso `scraper.py` busca de forma
genérica cualquier `<table>` en la página.

Después de la primera corrida:

1. Abre `docs/data/estado_diario_raw.html` en el repo (se genera
   automáticamente) para ver el HTML real ya renderizado.
2. Abre `docs/data/estado_diario.json` para ver qué se extrajo.
   - Si ves entradas con columnas con sentido (fechas, causas, tipo de
     resolución, etc.) → ¡perfecto, ya funciona!
   - Si en cambio ves un campo `"texto_bruto"` con todo el texto de la
     página → significa que no encontró una tabla `<table>` real, y hay
     que ajustar la función `extract_entries()` en `scraper.py` con los
     selectores CSS correctos (clases o ids que veas en el HTML crudo).

## Frecuencia

Por defecto corre **una vez al día** a las 13:00 UTC (10:00 u 11:00 hora de
Chile, según horario de verano). Puedes cambiar el horario editando el
`cron` en `.github/workflows/scrape.yml`, o correrlo manualmente cuando
quieras desde la pestaña *Actions*.

## Aviso

Este proyecto no es oficial ni está afiliado al Tribunal Calificador de
Elecciones. Simplemente reorganiza información pública que el propio sitio
publica.
