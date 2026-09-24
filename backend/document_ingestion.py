"""
Document Ingestion Module
--------------------------
Handles three input types:
  1. Plain images (jpg, png, etc.)          -> OCR via pytesseract
  2. Text-based PDFs (has an embedded text layer) -> direct extraction via pdfplumber
  3. Scanned/image-only PDFs (no text layer) -> rasterize pages (PyMuPDF) + OCR

Usage:
    from document_ingestion import extract_text
    result = extract_text("path/to/file.pdf")
    print(result["text"])
    print(result["method_used"])
"""

import os
import io
from PIL import Image
import pytesseract
import pdfplumber
import fitz  # PyMuPDF
import docx  # python-docx

# Windows: point pytesseract to the Tesseract executable
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


SUPPORTED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
SUPPORTED_PDF_EXT = {".pdf"}
SUPPORTED_DOCX_EXT = {".docx"}


def _extract_text_from_image(image_path_or_pil):
    """OCR a single image (file path or PIL Image object) and return text."""
    if isinstance(image_path_or_pil, str):
        img = Image.open(image_path_or_pil)
    else:
        img = image_path_or_pil
    return pytesseract.image_to_string(img)


def _pdf_has_text_layer(pdf_path, min_chars=20):
    """
    Quick check: does this PDF have a usable embedded text layer,
    or is it just scanned images with no fonts?
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            sample_text = ""
            # Check first few pages only, for speed
            for page in pdf.pages[:3]:
                page_text = page.extract_text() or ""
                sample_text += page_text
            return len(sample_text.strip()) >= min_chars
    except Exception:
        return False


def _extract_text_from_text_pdf(pdf_path):
    """Extract text directly from a PDF that already has a text layer."""
    full_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            full_text.append(f"\n--- Page {i + 1} ---\n{page_text}")
    return "\n".join(full_text)


def _extract_text_from_scanned_pdf(pdf_path, dpi=200):
    """
    Rasterize each page of a scanned/image-only PDF and run OCR on it.
    Uses PyMuPDF (fitz) to render pages as images, then pytesseract for OCR.
    """
    full_text = []
    doc = fitz.open(pdf_path)
    zoom = dpi / 72  # PDF default is 72 dpi
    matrix = fitz.Matrix(zoom, zoom)

    for page_num, page in enumerate(doc):
        pix = page.get_pixmap(matrix=matrix)
        img_bytes = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_bytes))
        page_text = pytesseract.image_to_string(img)
        full_text.append(f"\n--- Page {page_num + 1} (OCR) ---\n{page_text}")

    doc.close()
    return "\n".join(full_text)


def _extract_text_from_docx(docx_path):
    """Extract text from a .docx file, preserving paragraph structure."""
    doc = docx.Document(docx_path)
    paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
    return "\n".join(paragraphs)


def extract_text(file_path):
    """
    Main entry point. Detects file type and routes to the correct
    extraction method. Returns a dict with the extracted text and
    metadata about how it was extracted (useful for logging/debugging
    and for the report).
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    if ext in SUPPORTED_IMAGE_EXT:
        text = _extract_text_from_image(file_path)
        return {
            "file": file_path,
            "type": "image",
            "method_used": "OCR (pytesseract)",
            "text": text.strip(),
        }

    elif ext in SUPPORTED_PDF_EXT:
        if _pdf_has_text_layer(file_path):
            text = _extract_text_from_text_pdf(file_path)
            return {
                "file": file_path,
                "type": "text_pdf",
                "method_used": "Direct extraction (pdfplumber)",
                "text": text.strip(),
            }
        else:
            text = _extract_text_from_scanned_pdf(file_path)
            return {
                "file": file_path,
                "type": "scanned_pdf",
                "method_used": "Rasterize (PyMuPDF) + OCR (pytesseract)",
                "text": text.strip(),
            }

    elif ext in SUPPORTED_DOCX_EXT:
        text = _extract_text_from_docx(file_path)
        return {
            "file": file_path,
            "type": "docx",
            "method_used": "Direct extraction (python-docx)",
            "text": text.strip(),
        }

    else:
        raise ValueError(f"Unsupported file type: {ext}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python document_ingestion.py <file_path>")
        sys.exit(1)

    result = extract_text(sys.argv[1])
    print(f"File: {result['file']}")
    print(f"Type detected: {result['type']}")
    print(f"Method used: {result['method_used']}")
    print("\n--- Extracted Text (first 1000 chars) ---")
    print(result["text"][:1000])
