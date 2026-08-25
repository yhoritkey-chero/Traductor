import asyncio
import io
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import fitz
import streamlit as st
from PIL import Image
import pytesseract
from playwright.async_api import async_playwright


st.set_page_config(
    page_title="Traductor PDF + OCR",
    page_icon="📚",
    layout="wide",
)

LANGS = {
    "Inglés": "en",
    "Español": "es",
    "Portugués": "pt",
    "Francés": "fr",
    "Alemán": "de",
    "Italiano": "it",
}

OCR_LANGS = {
    "es": "spa+eng",
    "en": "eng",
    "pt": "por+eng",
    "fr": "fra+eng",
    "de": "deu+eng",
    "it": "ita+eng",
}


def natural_key(value):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", str(value))]


def get_workspace():
    if "workspace" not in st.session_state:
        st.session_state.workspace = tempfile.mkdtemp(prefix="traductor_pdf_")
    work = Path(st.session_state.workspace)
    work.mkdir(parents=True, exist_ok=True)
    return work


def reset_workspace():
    old = st.session_state.get("workspace")
    if old:
        shutil.rmtree(old, ignore_errors=True)
    for key in [
        "workspace", "pdf_hash", "page_count", "dpi", "last_results",
        "visual_pdf", "ocr_pdf", "source_code", "target_code"
    ]:
        st.session_state.pop(key, None)


def pdf_to_pngs(pdf_bytes, outdir, dpi=180):
    outdir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    files = []
    for number, page in enumerate(doc, 1):
        out = outdir / f"{number:04d}.png"
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pix.save(str(out))
        files.append(out)
    doc.close()
    return files


def translated_path(src, translated_dir):
    return translated_dir / f"{src.stem}_traducida.png"


def page_files(work):
    return sorted((work / "paginas").glob("*.png"), key=natural_key)


def translated_files(work):
    pages = page_files(work)
    translated_dir = work / "traducidas"
    return [
        translated_path(src, translated_dir)
        for src in pages
        if translated_path(src, translated_dir).exists()
        and translated_path(src, translated_dir).stat().st_size > 1000
    ]


def pending_files(work):
    pages = page_files(work)
    translated_dir = work / "traducidas"
    return [
        src for src in pages
        if not translated_path(src, translated_dir).exists()
        or translated_path(src, translated_dir).stat().st_size <= 1000
    ]


def find_chromium():
    env_path = os.getenv("CHROMIUM_PATH", "").strip()
    candidates = [
        env_path,
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return ""


def available_ocr_languages():
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception:
        return set()


def resolve_ocr_language(target):
    requested = OCR_LANGS.get(target, "eng")
    installed = available_ocr_languages()
    wanted = requested.split("+")
    usable = [x for x in wanted if x in installed]
    missing = [x for x in wanted if x not in installed]
    if not usable:
        if "eng" in installed:
            usable = ["eng"]
        elif installed:
            usable = [sorted(installed)[0]]
        else:
            usable = wanted
            missing = []
    return "+".join(usable), missing


def make_visual_pdf(images, out, dpi=180):
    images = sorted(images, key=natural_key)
    if not images:
        raise ValueError("No hay imágenes traducidas.")

    doc = fitz.open()
    for path in images:
        with Image.open(path) as im:
            width_px, height_px = im.size
        width_pt = width_px * 72 / dpi
        height_pt = height_px * 72 / dpi
        page = doc.new_page(width=width_pt, height=height_pt)
        page.insert_image(page.rect, filename=str(path))
    doc.save(str(out), deflate=True, garbage=4)
    doc.close()


def make_ocr_pdf(images, out, target="es", dpi=180, callback=None):
    lang, missing = resolve_ocr_language(target)
    result = fitz.open()
    total = len(images)
    try:
        for index, path in enumerate(sorted(images, key=natural_key), 1):
            pdf_bytes = pytesseract.image_to_pdf_or_hocr(
                str(path),
                extension="pdf",
                lang=lang,
                config=f"--dpi {int(dpi)}",
            )
            one = fitz.open(stream=pdf_bytes, filetype="pdf")
            result.insert_pdf(one)
            one.close()
            if callback:
                callback(index, total, path.name)
        result.save(str(out), deflate=True, garbage=4)
    finally:
        result.close()
    return lang, missing


async def automate_google_images(files, translated_dir, source="en", target="es", pause=2):
    """Automatiza Google Translate / Images en Chromium headless.

    No intenta resolver CAPTCHA ni verificaciones de Google. Si Google exige una
    intervención humana, la app se detiene y muestra el error para evitar un bucle.
    """
    chromium = find_chromium()
    if not chromium:
        raise RuntimeError(
            "Chromium no está instalado en el servidor. Revisa packages.txt y reinicia la app."
        )

    translated_dir.mkdir(parents=True, exist_ok=True)
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=chromium,
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 1000},
            locale="es-PE",
        )
        page = await context.new_page()

        try:
            url = f"https://translate.google.com/?sl={source}&tl={target}&op=images"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)

            body_text = (await page.locator("body").inner_text()).lower()
            if "captcha" in body_text or "unusual traffic" in body_text or "tráfico inusual" in body_text:
                raise RuntimeError(
                    "Google mostró una verificación/CAPTCHA. Streamlit Cloud no puede completarla de forma interactiva."
                )

            # La URL ya abre el modo Imágenes; este click es solo un fallback.
            try:
                await page.get_by_text(re.compile(r"Imágenes|Images", re.I)).first.click(timeout=5000)
            except Exception:
                pass

            for src in sorted(files, key=natural_key):
                dest = translated_path(src, translated_dir)
                if dest.exists() and dest.stat().st_size > 1000:
                    results.append((src.name, "ya estaba hecha"))
                    continue

                try:
                    file_input = page.locator('input[type="file"]').last
                    await file_input.wait_for(state="attached", timeout=20000)
                    await file_input.set_input_files(str(src))
                    await page.wait_for_timeout(2000)

                    body_text = (await page.locator("body").inner_text()).lower()
                    if "captcha" in body_text or "unusual traffic" in body_text or "tráfico inusual" in body_text:
                        raise RuntimeError("Google solicitó una verificación/CAPTCHA.")

                    candidates = [
                        page.get_by_role(
                            "button",
                            name=re.compile(r"Descargar traducción|Download translation", re.I),
                        ).last,
                        page.locator(
                            '[aria-label*="Descargar traducción"],'
                            '[aria-label*="Download translation"],'
                            '[title*="Descargar traducción"],'
                            '[title*="Download translation"]'
                        ).last,
                        page.locator(
                            '[aria-label*="Descargar"],'
                            '[aria-label*="Download"],'
                            '[title*="Descargar"],'
                            '[title*="Download"]'
                        ).last,
                    ]

                    downloaded = False
                    last_error = None
                    for button in candidates:
                        try:
                            async with page.expect_download(timeout=90000) as download_info:
                                await button.click(timeout=15000)
                            download = await download_info.value
                            await download.save_as(str(dest))
                            downloaded = True
                            break
                        except Exception as exc:
                            last_error = exc

                    if not downloaded:
                        raise last_error or RuntimeError("No se encontró el botón de descarga.")

                    if not dest.exists() or dest.stat().st_size <= 1000:
                        raise RuntimeError("Google no devolvió una imagen válida.")

                    results.append((src.name, "traducida"))
                    await page.wait_for_timeout(int(pause * 1000))

                    clear_candidates = [
                        page.get_by_role(
                            "button", name=re.compile(r"Borrar imagen|Clear image", re.I)
                        ).last,
                        page.locator(
                            '[aria-label*="Borrar"],'
                            '[aria-label*="Clear"],'
                            '[title*="Borrar"],'
                            '[title*="Clear"]'
                        ).last,
                    ]
                    for clear in clear_candidates:
                        try:
                            await clear.click(timeout=4000)
                            await page.wait_for_timeout(600)
                            break
                        except Exception:
                            pass

                except Exception as exc:
                    results.append((src.name, f"ERROR: {type(exc).__name__}: {str(exc)[:180]}"))
                    break
        finally:
            await context.close()
            await browser.close()

    return results


def make_progress_zip(work):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in ["paginas", "traducidas"]:
            directory = work / folder
            if directory.exists():
                for path in sorted(directory.glob("*.png"), key=natural_key):
                    zf.write(path, arcname=f"{folder}/{path.name}")
    buffer.seek(0)
    return buffer.getvalue()


# -------------------------- INTERFAZ --------------------------
st.title("📚 Traductor PDF → Google Imágenes → PDF OCR")
st.caption(
    "Versión para GitHub + Streamlit Cloud. El PDF se procesa temporalmente en el servidor; "
    "las páginas se envían a Google Traductor para realizar la traducción visual."
)

with st.sidebar:
    st.header("⚙️ Configuración")
    source_name = st.selectbox("Idioma original", list(LANGS), index=0)
    target_name = st.selectbox("Traducir a", list(LANGS), index=1)
    source = LANGS[source_name]
    target = LANGS[target_name]
    dpi = st.slider("Calidad (DPI)", 140, 240, 180, 10)
    pause = st.slider("Pausa entre páginas", 1, 8, 2)

    chromium = find_chromium()
    ocr_installed = available_ocr_languages()
    st.caption("Chromium: " + ("✅ detectado" if chromium else "❌ no detectado"))
    st.caption("Tesseract: " + ("✅ detectado" if shutil.which("tesseract") else "❌ no detectado"))
    if ocr_installed:
        st.caption("OCR: " + ", ".join(sorted(ocr_installed)))

    if st.button("🧹 Reiniciar proyecto"):
        reset_workspace()
        st.rerun()

work = get_workspace()

uploaded = st.file_uploader("1. Sube el PDF que deseas traducir", type=["pdf"])

if uploaded is not None:
    pdf_bytes = uploaded.getvalue()
    current_hash = f"{uploaded.name}:{len(pdf_bytes)}:{hash(pdf_bytes[:4096])}"

    if st.session_state.get("pdf_hash") != current_hash:
        # Nuevo documento: limpia el trabajo anterior de esta sesión.
        reset_workspace()
        work = get_workspace()
        st.session_state.pdf_hash = current_hash
        st.session_state.source_code = source
        st.session_state.target_code = target
        st.session_state.dpi = dpi

        original = work / "original.pdf"
        original.write_bytes(pdf_bytes)
        with st.spinner("Convirtiendo el PDF en páginas..."):
            pages = pdf_to_pngs(pdf_bytes, work / "paginas", dpi=dpi)
        st.session_state.page_count = len(pages)
        st.success(f"PDF preparado: {len(pages)} páginas.")

pages = page_files(work)
if pages:
    translated = translated_files(work)
    pending = pending_files(work)

    st.markdown("### 2. Traducir páginas con Google")
    c1, c2, c3 = st.columns(3)
    c1.metric("Páginas", len(pages))
    c2.metric("Traducidas", len(translated))
    c3.metric("Pendientes", len(pending))

    if pending:
        batch_default = min(3, len(pending))
        batch_max = min(20, len(pending))
        batch = st.number_input(
            "Páginas pendientes a procesar en este lote",
            min_value=1,
            max_value=batch_max,
            value=batch_default,
            help="En Streamlit Cloud conviene procesar por lotes pequeños (3–10 páginas).",
        )

        if st.button("🚀 Traducir lote pendiente", type="primary"):
            st.session_state.source_code = source
            st.session_state.target_code = target
            if not chromium:
                st.error("Chromium no está disponible. Revisa packages.txt y reinicia el despliegue.")
            elif source == target:
                st.error("El idioma original y el idioma de destino deben ser diferentes.")
            else:
                status = st.empty()
                status.info("Abriendo Google Traductor en el servidor...")
                try:
                    results = asyncio.run(
                        automate_google_images(
                            pending[: int(batch)],
                            work / "traducidas",
                            source=source,
                            target=target,
                            pause=pause,
                        )
                    )
                    st.session_state.last_results = results
                    status.empty()
                    if any("ERROR" in result for _, result in results):
                        st.warning("El lote se detuvo por un error. Revisa el registro de abajo.")
                    else:
                        st.success("Lote terminado. Puedes continuar con el siguiente.")
                    st.rerun()
                except Exception as exc:
                    status.empty()
                    st.error(str(exc))
    else:
        st.success("✅ Todas las páginas están traducidas.")

    if st.session_state.get("last_results"):
        with st.expander("Registro del último lote"):
            for name, result in st.session_state.last_results:
                icon = "✅" if result in {"traducida", "ya estaba hecha"} else "❌"
                st.write(f"{icon} {name} — {result}")

    translated = translated_files(work)
    pending = pending_files(work)

    if translated:
        st.download_button(
            "💾 Descargar respaldo del avance (ZIP)",
            make_progress_zip(work),
            file_name="avance_traduccion.zip",
            mime="application/zip",
            help="Útil porque Streamlit Cloud usa almacenamiento temporal.",
        )

        st.markdown("### 3. Crear PDF final")
        if pending:
            st.warning(
                f"Todavía faltan {len(pending)} páginas. Puedes crear un PDF parcial, "
                "pero el documento final debería generarse cuando el contador llegue a 0."
            )

        col_a, col_b = st.columns(2)
        project_dpi = int(st.session_state.get("dpi", dpi))
        target_for_ocr = target

        with col_a:
            if st.button("📑 Crear PDF visual"):
                out = work / "PDF_TRADUCIDO.pdf"
                with st.spinner("Construyendo PDF visual..."):
                    make_visual_pdf(translated, out, dpi=project_dpi)
                st.session_state.visual_pdf = str(out)
                st.success("PDF visual creado.")

        with col_b:
            if st.button("🔎 Crear PDF + OCR", type="primary"):
                out = work / "PDF_TRADUCIDO_OCR.pdf"
                bar = st.progress(0.0)
                text = st.empty()

                def update(i, total, name):
                    bar.progress(i / total)
                    text.caption(f"OCR {i}/{total}: {name}")

                try:
                    lang, missing = make_ocr_pdf(
                        translated,
                        out,
                        target=target_for_ocr,
                        dpi=project_dpi,
                        callback=update,
                    )
                    st.session_state.ocr_pdf = str(out)
                    bar.empty()
                    text.empty()
                    st.success(f"PDF OCR creado con: {lang}")
                    if missing:
                        st.warning("Modelos OCR no disponibles: " + ", ".join(missing))
                except Exception as exc:
                    st.error(f"No se pudo crear el OCR: {exc}")

        visual = Path(st.session_state.visual_pdf) if st.session_state.get("visual_pdf") else None
        ocr = Path(st.session_state.ocr_pdf) if st.session_state.get("ocr_pdf") else None

        if visual and visual.exists():
            st.download_button(
                "⬇️ Descargar PDF traducido",
                visual.read_bytes(),
                file_name="PDF_TRADUCIDO.pdf",
                mime="application/pdf",
            )

        if ocr and ocr.exists():
            st.download_button(
                "⬇️ Descargar PDF traducido + OCR",
                ocr.read_bytes(),
                file_name="PDF_TRADUCIDO_OCR.pdf",
                mime="application/pdf",
                type="primary",
            )

st.divider()
st.info(
    "Importante: esta versión depende de la interfaz web de Google Traductor. "
    "Si Google muestra un CAPTCHA o bloquea el navegador headless, la traducción automática "
    "puede detenerse. La app no intenta eludir esas verificaciones."
)
