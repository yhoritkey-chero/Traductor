# Traductor PDF + OCR — Streamlit Cloud

Aplicación web para convertir un PDF en imágenes, traducir visualmente cada página mediante Google Traductor / Imágenes y crear un PDF final con OCR buscable.

## Flujo

PDF → PNG → Google Translate / Images → imágenes traducidas → PDF visual → PDF con OCR.

## Archivos del repositorio

- `app.py`: aplicación Streamlit.
- `requirements.txt`: dependencias Python.
- `packages.txt`: Chromium y Tesseract para Debian/Streamlit Cloud.
- `.streamlit/config.toml`: permite PDFs de hasta 300 MB.

## Subir a GitHub

1. Crea un repositorio nuevo en GitHub.
2. Sube **el contenido de esta carpeta**, no el ZIP completo.
3. Verifica que `app.py`, `requirements.txt` y `packages.txt` estén en la raíz del repositorio.
4. Conserva también la carpeta `.streamlit` con `config.toml`.

## Desplegar en Streamlit Community Cloud

1. Entra a Streamlit Community Cloud.
2. Elige **Create app / Deploy an app**.
3. Selecciona tu repositorio de GitHub.
4. Branch: normalmente `main`.
5. Main file path: `app.py`.
6. Pulsa **Deploy**.

Durante la construcción, Streamlit instalará las dependencias Python de `requirements.txt` y los paquetes Linux de `packages.txt`.

## Uso

1. Sube un PDF.
2. Selecciona idioma original y destino.
3. Prueba primero un lote de 3 páginas.
4. Si funciona, continúa por lotes.
5. Cuando no queden páginas pendientes, crea el PDF visual y el PDF + OCR.
6. Descarga el resultado.

## OCR

Tesseract se instala en el servidor. Se incluyen modelos para inglés, español, portugués, francés, alemán e italiano. El PDF OCR conserva la imagen traducida y agrega una capa de texto buscable/seleccionable.

## Almacenamiento temporal

Streamlit Community Cloud no debe considerarse almacenamiento permanente. La aplicación guarda los archivos de trabajo en un directorio temporal del servidor mientras dura la sesión/instancia. Por eso existe el botón **Descargar respaldo del avance (ZIP)**.

## Limitación importante de Google Traductor

La traducción por imágenes se automatiza controlando Chromium en modo headless. Google puede cambiar su interfaz, limitar automatizaciones o mostrar CAPTCHA/verificaciones. La aplicación no intenta evadir esas verificaciones. Si Google exige interacción humana, el lote se detendrá y mostrará el error.

Por esta razón conviene probar primero con 3 páginas antes de procesar un documento completo.

## Privacidad

El PDF se procesa temporalmente en el servidor de Streamlit. Para traducir visualmente las páginas, cada imagen se envía a Google Traductor. No uses esta versión cloud con documentos cuya política institucional prohíba enviarlos a servicios externos.
