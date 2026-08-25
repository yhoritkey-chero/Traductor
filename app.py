import hashlib
import io
import os
import re
import shutil
import statistics
import time
from pathlib import Path

import fitz
import pytesseract
import streamlit as st
from deep_translator import GoogleTranslator
from PIL import Image, ImageDraw, ImageFont
from pytesseract import Output


APP_VERSION = "v4.0 ESTABLE · OCR + TRADUCCIÓN"
ROOT = Path("/tmp/traductor_pdf_ocr")
ROOT.mkdir(parents=True, exist_ok=True)

LANGS = {
    "Inglés": ("en", "eng"),
    "Español": ("es", "spa"),
    "Francés": ("fr", "fra"),
    "Alemán": ("de", "deu"),
    "Italiano": ("it", "ita"),
    "Portugués": ("pt", "por"),
}

st.set_page_config(
    page_title="Traductor PDF OCR",
    page_icon="📚",
    layout="wide",
)


def natural_key(value):
    return [int(x) if x.isdigit() else x.lower()
            for x in re.split(r"(\d+)", str(value))]


def project_dir(pdf_bytes: bytes) -> Path:
    digest = hashlib.sha256(pdf_bytes).hexdigest()[:16]
    path = ROOT / digest
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_font():
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return None


FONT_PATH = find_font()


def get_font(size: int):
    size = max(7, int(size))
    if FONT_PATH:
        return ImageFont.truetype(FONT_PATH, size=size)
    return ImageFont.load_default()


def pdf_to_images(pdf_bytes: bytes, pages_dir: Path, dpi: int = 180):
    pages_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)

    outputs = []
    for i, page in enumerate(doc, start=1):
        out = pages_dir / f"{i:04d}.png"
        if not out.exists():
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pix.save(str(out))
        outputs.append(out)

    doc.close()
    return outputs


def safe_conf(value):
    try:
        return float(value)
    except Exception:
        return -1.0


def looks_translatable(text: str) -> bool:
    text = " ".join(text.split()).strip()
    if len(text) < 3:
        return False

    letters = sum(ch.isalpha() for ch in text)
    digits = sum(ch.isdigit() for ch in text)
    visible = sum(not ch.isspace() for ch in text)

    if visible == 0:
        return False

    # Evita fórmulas, ejes numéricos y fragmentos casi puramente simbólicos.
    if letters / visible < 0.45:
        return False

    if digits > letters * 1.2:
        return False

    return True


def ocr_blocks(image: Image.Image, ocr_lang: str):
    data = pytesseract.image_to_data(
        image,
        lang=ocr_lang,
        config="--psm 3",
        output_type=Output.DICT,
    )

    groups = {}
    n = len(data["text"])

    for i in range(n):
        word = (data["text"][i] or "").strip()
        conf = safe_conf(data["conf"][i])

        if not word or conf < 35:
            continue

        key = (
            int(data["block_num"][i]),
            int(data["par_num"][i]),
        )

        groups.setdefault(key, []).append({
            "text": word,
            "left": int(data["left"][i]),
            "top": int(data["top"][i]),
            "width": int(data["width"][i]),
            "height": int(data["height"][i]),
            "line": int(data["line_num"][i]),
            "conf": conf,
        })

    blocks = []

    for words in groups.values():
        if not words:
            continue

        text = " ".join(w["text"] for w in words)
        text = re.sub(r"\s+([,.;:!?%)\]])", r"\1", text)
        text = re.sub(r"([(\[])\s+", r"\1", text)

        if not looks_translatable(text):
            continue

        left = min(w["left"] for w in words)
        top = min(w["top"] for w in words)
        right = max(w["left"] + w["width"] for w in words)
        bottom = max(w["top"] + w["height"] for w in words)

        heights = [w["height"] for w in words if w["height"] > 0]
        median_height = statistics.median(heights) if heights else 14

        line_count = len(set(w["line"] for w in words))
        block_width = right - left
        block_height = bottom - top

        # Descarta microbloques que suelen ser ruido OCR.
        if block_width < 40 or block_height < 12:
            continue

        blocks.append({
            "text": text,
            "bbox": (left, top, right, bottom),
            "font_px": max(8, int(median_height * 0.92)),
            "line_count": max(1, line_count),
        })

    return sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))


def chunk_for_translation(text: str, max_chars: int = 4300):
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    current = ""

    for sentence in sentences:
        if not sentence:
            continue
        candidate = (current + " " + sentence).strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(sentence) <= max_chars:
                current = sentence
            else:
                for i in range(0, len(sentence), max_chars):
                    chunks.append(sentence[i:i + max_chars])
                current = ""

    if current:
        chunks.append(current)

    return chunks


def translate_text(text: str, source_code: str, target_code: str, cache: dict):
    key = (source_code, target_code, text)
    if key in cache:
        return cache[key]

    if source_code == target_code:
        cache[key] = text
        return text

    translator = GoogleTranslator(source=source_code, target=target_code)
    translated_parts = []

    for chunk in chunk_for_translation(text):
        last_error = None
        for attempt in range(3):
            try:
                result = translator.translate(chunk)
                if result and result.strip():
                    translated_parts.append(result.strip())
                    last_error = None
                    break
            except Exception as exc:
                last_error = exc
                time.sleep(1.2 * (attempt + 1))

        if last_error is not None:
            raise RuntimeError(f"No se pudo traducir un bloque: {last_error}")

        time.sleep(0.15)

    translated = " ".join(translated_parts).strip()
    cache[key] = translated
    return translated


def wrap_pixels(draw, text, font, max_width):
    words = text.split()
    if not words:
        return []

    lines = []
    current = words[0]

    for word in words[1:]:
        candidate = current + " " + word
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines


def fit_text(draw, text, box, preferred_size):
    left, top, right, bottom = box
    max_width = max(20, right - left)
    max_height = max(12, bottom - top)

    size = max(7, preferred_size)

    while size >= 7:
        font = get_font(size)
        lines = wrap_pixels(draw, text, font, max_width)
        spacing = max(1, int(size * 0.18))
        line_height = size + spacing
        needed = len(lines) * line_height

        if needed <= max_height * 1.20:
            return font, lines, line_height

        size -= 1

    font = get_font(7)
    lines = wrap_pixels(draw, text, font, max_width)
    return font, lines, 8


def render_translated_page(
    src_path: Path,
    out_path: Path,
    source_ocr_lang: str,
    source_code: str,
    target_code: str,
    translation_cache: dict,
):
    image = Image.open(src_path).convert("RGB")
    blocks = ocr_blocks(image, source_ocr_lang)

    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)

    translated_count = 0

    for block in blocks:
        original = block["text"]

        try:
            translated = translate_text(
                original,
                source_code=source_code,
                target_code=target_code,
                cache=translation_cache,
            )
        except Exception:
            # Si un bloque puntual falla, preserva el original y sigue con los demás.
            continue

        if not translated or translated.strip() == original.strip():
            continue

        left, top, right, bottom = block["bbox"]

        # Pequeño margen, sin invadir demasiado gráficos/columnas vecinas.
        pad_x = 2
        pad_y = 1
        box = (
            max(0, left - pad_x),
            max(0, top - pad_y),
            min(canvas.width, right + pad_x),
            min(canvas.height, bottom + pad_y),
        )

        draw.rectangle(box, fill="white")

        font, lines, line_height = fit_text(
            draw,
            translated,
            box,
            preferred_size=block["font_px"],
        )

        x = box[0]
        y = box[1]

        for line in lines:
            if y + line_height > box[3] + max(4, int(line_height * 0.35)):
                break
            draw.text((x, y), line, font=font, fill="black")
            y += line_height

        translated_count += 1

    canvas.save(out_path, format="PNG", optimize=True)
    image.close()
    canvas.close()

    return translated_count


def make_image_pdf(image_paths, output_path: Path):
    doc = fitz.open()

    for image_path in sorted(image_paths, key=natural_key):
        img = Image.open(image_path)
        width, height = img.size
        img.close()

        # Tamaño PDF proporcional a la imagen.
        page = doc.new_page(width=width, height=height)
        page.insert_image(page.rect, filename=str(image_path))

    doc.save(str(output_path), deflate=True, garbage=3)
    doc.close()


def make_searchable_pdf(image_paths, output_path: Path, ocr_lang: str):
    merged = fitz.open()

    for image_path in sorted(image_paths, key=natural_key):
        pdf_bytes = pytesseract.image_to_pdf_or_hocr(
            str(image_path),
            extension="pdf",
            lang=ocr_lang,
            config="--psm 3",
        )
        page_pdf = fitz.open(stream=pdf_bytes, filetype="pdf")
        merged.insert_pdf(page_pdf)
        page_pdf.close()

    merged.save(str(output_path), deflate=True, garbage=3)
    merged.close()


def installed_tesseract_languages():
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception:
        return set()


def clear_project(path: Path):
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


# ---------- UI ----------

st.title("📚 Traductor PDF → Español → PDF OCR")
st.success(f"✅ {APP_VERSION}")
st.caption(
    "Versión para Streamlit Cloud. No usa Chromium ni automatiza Google Traductor de Imágenes. "
    "Reconoce el texto del PDF, lo traduce y genera una copia visual con OCR."
)

with st.sidebar:
    st.header("⚙️ Configuración")

    source_label = st.selectbox(
        "Idioma original",
        list(LANGS.keys()),
        index=0,
    )
    target_label = st.selectbox(
        "Traducir a",
        list(LANGS.keys()),
        index=1,
    )

    dpi = st.slider("Calidad (DPI)", 140, 220, 180, 10)

    source_code, source_ocr = LANGS[source_label]
    target_code, target_ocr = LANGS[target_label]

    st.markdown("---")
    st.caption(
        "💡 Para artículos científicos, 170–190 DPI suele dar buen equilibrio entre "
        "calidad, velocidad y memoria."
    )


uploaded = st.file_uploader(
    "1. Sube el PDF que deseas traducir",
    type=["pdf"],
)

if uploaded is not None:
    pdf_bytes = uploaded.getvalue()
    work = project_dir(pdf_bytes)
    pages_dir = work / "paginas"
    translated_dir = work / "traducidas"
    translated_dir.mkdir(parents=True, exist_ok=True)

    original_pdf = work / "original.pdf"
    if not original_pdf.exists():
        original_pdf.write_bytes(pdf_bytes)

    with st.spinner("Preparando páginas del PDF..."):
        page_paths = pdf_to_images(pdf_bytes, pages_dir, dpi=dpi)

    translated_paths = [
        translated_dir / p.name
        for p in page_paths
        if (translated_dir / p.name).exists()
    ]
    pending = [
        p for p in page_paths
        if not (translated_dir / p.name).exists()
    ]

    c1, c2, c3 = st.columns(3)
    c1.metric("Páginas", len(page_paths))
    c2.metric("Traducidas", len(translated_paths))
    c3.metric("Pendientes", len(pending))

    available_langs = installed_tesseract_languages()

    if source_ocr not in available_langs:
        st.error(
            f"Tesseract no tiene instalado el idioma OCR '{source_ocr}'. "
            "Revisa packages.txt y reinicia la app."
        )
        st.stop()

    if st.button(
        "🚀 Traducir documento completo",
        type="primary",
        disabled=(len(pending) == 0),
    ):
        translation_cache = {}
        progress = st.progress(0)
        status = st.empty()
        log_box = st.empty()
        logs = []

        total = len(pending)

        for idx, src in enumerate(pending, start=1):
            out = translated_dir / src.name
            status.info(
                f"Página {idx}/{total} · OCR + traducción + reconstrucción · {src.name}"
            )

            try:
                count = render_translated_page(
                    src_path=src,
                    out_path=out,
                    source_ocr_lang=source_ocr,
                    source_code=source_code,
                    target_code=target_code,
                    translation_cache=translation_cache,
                )
                logs.append(f"✅ {src.name}: {count} bloques traducidos")
            except Exception as exc:
                logs.append(f"❌ {src.name}: {type(exc).__name__}: {exc}")
                log_box.text("\n".join(logs[-12:]))
                st.error(
                    "La traducción se detuvo. Las páginas terminadas quedaron guardadas; "
                    "puedes volver a pulsar el botón para continuar."
                )
                break

            progress.progress(idx / total)
            log_box.text("\n".join(logs[-12:]))

        status.success("Proceso de traducción finalizado.")
        st.rerun()

    translated_paths = sorted(
        translated_dir.glob("*.png"),
        key=natural_key,
    )

    if translated_paths:
        st.subheader("2. Vista previa")

        preview_cols = st.columns(min(3, len(translated_paths)))
        for i, img_path in enumerate(translated_paths[:3]):
            with preview_cols[i % len(preview_cols)]:
                st.image(
                    str(img_path),
                    caption=f"Página {int(img_path.stem)}",
                    use_container_width=True,
                )

        if len(translated_paths) < len(page_paths):
            st.warning(
                f"Aún faltan {len(page_paths) - len(translated_paths)} páginas. "
                "Puedes continuar la traducción antes de generar el PDF."
            )

        if len(translated_paths) == len(page_paths):
            st.subheader("3. Crear PDF final")

            visual_pdf = work / "PDF_TRADUCIDO.pdf"
            searchable_pdf = work / "PDF_TRADUCIDO_OCR.pdf"

            col_a, col_b = st.columns(2)

            with col_a:
                if st.button("📄 Crear PDF traducido", use_container_width=True):
                    with st.spinner("Creando PDF visual traducido..."):
                        make_image_pdf(translated_paths, visual_pdf)
                    st.success("PDF traducido creado.")

            with col_b:
                if st.button("🔎 Crear PDF traducido + OCR", use_container_width=True):
                    if target_ocr not in available_langs:
                        st.error(
                            f"Tesseract no tiene instalado el idioma OCR '{target_ocr}'. "
                            "Revisa packages.txt."
                        )
                    else:
                        with st.spinner("Creando capa OCR del PDF final..."):
                            make_searchable_pdf(
                                translated_paths,
                                searchable_pdf,
                                ocr_lang=target_ocr,
                            )
                        st.success("PDF traducido con OCR creado.")

            if visual_pdf.exists():
                st.download_button(
                    "⬇️ Descargar PDF traducido",
                    data=visual_pdf.read_bytes(),
                    file_name=f"{Path(uploaded.name).stem}_TRADUCIDO.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )

            if searchable_pdf.exists():
                st.download_button(
                    "⬇️ Descargar PDF traducido + OCR",
                    data=searchable_pdf.read_bytes(),
                    file_name=f"{Path(uploaded.name).stem}_TRADUCIDO_OCR.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )

    st.markdown("---")
    if st.button("🧹 Reiniciar este proyecto"):
        clear_project(work)
        st.rerun()
