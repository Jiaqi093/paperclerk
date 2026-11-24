"""F2 step 1: what the model is shown for one document.

Pages with a text layer are sent as text. Pages flagged `needs_vision` (scans, photos) are sent
as images, together with whatever little text they have. The whole document is sent: nothing is
cut for length. Only what one request can carry (the model's context window, the API's image
limits) can stop part of it from going, and then `unread` says what was left out, so the
document goes to review instead of being silently truncated.

The document is untrusted data. It is wrapped in <document> tags, and anything inside it that
looks like one of our tags is defused, so text in a document can't close the block and pose
as instructions. The file name is sent separately as a hint. Values must come from the
document itself: the prompt says so, and the source check looks only at the document's text
(for a scanned page, which has no text, only the prompt stands between the name and a value).
"""

from __future__ import annotations

import contextlib
import io
import math
import re
import unicodedata
from dataclasses import dataclass

from PIL import Image, ImageOps

from paperclerk.ingest.parsers.base import ParsedDocument

# Long edge of every image sent. Small print on receipts stays readable, and it is under the
# API's 3000 px limit for requests with more than 20 images.
IMAGE_LONG_EDGE = 2000
JPEG_QUALITY = 85
# Never render a PDF page above this resolution: a stamp-sized page doesn't need to become
# 2000 px of blur.
MAX_RENDER_DPI = 300
# API limits for one request: 600 images on 1M-context models, 128 MB in total after base64
# (which adds a third). Kept below so the text and the rest of the request fit.
MAX_IMAGES = 600
MAX_IMAGE_BYTES = 90 * 1024 * 1024
# Tokens the document may use, leaving room for the instructions and the answer in a 1M-token
# context window. Pass a smaller budget for a model with a smaller window.
DEFAULT_TOKEN_BUDGET = 900_000
# Rough, on the high side: text runs 3-4 characters per token; an image costs one token per
# 28x28 pixel patch (2000x1414 → about 3,700).
CHARS_PER_TOKEN = 3
IMAGE_PATCH = 28

# Photos up to 300 megapixels (200 MP phone cameras) can be opened: JPEGs are decoded at a
# fraction of their size (draft mode). Other formats are decoded whole, so they are limited.
# Pillow only has a process-wide setting for this, so importing this module raises it for any
# Pillow use in the process; keep MAX_NON_JPEG_PIXELS in front of decoding other formats.
Image.MAX_IMAGE_PIXELS = 300_000_000
MAX_NON_JPEG_PIXELS = 100_000_000

# Bump when the markup below changes: it is part of the prompt version.
MARKUP_VERSION = 1

_TAGS = ("document", "page", "file_name")
_TAG_LOOKALIKE = re.compile(r"<(\s*/*\s*(?:" + "|".join(_TAGS) + r")\b)", re.IGNORECASE)
_W_DEL = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}del"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass(frozen=True)
class ImageInput:
    page_no: int  # 1-based page it shows; DOCX and image files are one page
    data: bytes  # JPEG

    media_type = "image/jpeg"


@dataclass(frozen=True)
class ModelInput:
    text: str  # file name and document, marked up as data
    images: tuple[ImageInput, ...]  # in page order; the text says which page each one is
    # Parts of the document that couldn't be sent, and why. Non-empty → the result goes to
    # review whatever its confidence.
    unread: tuple[str, ...] = ()


def build_input(
    doc: ParsedDocument, data: bytes, file_name: str, token_budget: int = DEFAULT_TOKEN_BUDGET
) -> ModelInput:
    """`data` is the file's bytes (the same ones that were parsed), `file_name` its name."""
    unread: list[str] = []

    # Text first: it is the cheaper and more reliable way to read a page.
    page_texts = [_defuse(p.text) for p in doc.pages]
    tokens = 0
    text_fits = len(page_texts)
    for i, text in enumerate(page_texts):
        tokens += _text_tokens(text)
        if tokens > token_budget:
            text_fits = i
            break
    if text_fits < len(page_texts):
        dropped = list(range(text_fits + 1, len(page_texts) + 1))
        unread.append(f"{_pages(dropped)}: too long for one request, not sent")
        tokens = sum(_text_tokens(t) for t in page_texts[:text_fits])

    vision_pages = [i + 1 for i, p in enumerate(doc.pages[:text_fits]) if p.needs_vision]
    images: list[ImageInput] = []
    if vision_pages:
        images, image_unread = _images_for(doc.mime_type, data, vision_pages, token_budget - tokens)
        unread.extend(image_unread)

    image_index: dict[int, list[int]] = {}
    for n, img in enumerate(images, start=1):
        image_index.setdefault(img.page_no, []).append(n)

    parts = [f"<file_name>{_defuse(_base_name(file_name))}</file_name>", "<document>"]
    for page_no, (page, text) in enumerate(zip(doc.pages, page_texts, strict=True), start=1):
        if page_no > text_fits:
            parts.append(f'<page number="{page_no}" omitted="too long"></page>')
            continue
        shown = image_index.get(page_no, [])
        if shown:
            label = ", ".join(str(n) for n in shown)
            attrs = f' number="{page_no}" scanned="true" image="{label}"'
        elif page.needs_vision:
            attrs = f' number="{page_no}" scanned="true" image="none"'
        else:
            attrs = f' number="{page_no}"'
        parts.append(f"<page{attrs}>\n{text}\n</page>")
    parts.append("</document>")
    return ModelInput(text="\n".join(parts), images=tuple(images), unread=tuple(unread))


def _text_tokens(text: str) -> int:
    # Latin text runs several characters per token; Chinese, Japanese and the like about one
    # token per character or more, so those count one each.
    wide = sum(1 for ch in text if ord(ch) > 0x2FF)
    return math.ceil((len(text) - wide) / CHARS_PER_TOKEN) + wide + 10  # + the page markup


def _image_tokens(jpeg: bytes) -> int:
    with Image.open(io.BytesIO(jpeg)) as img:
        w, h = img.size
    return math.ceil(w / IMAGE_PATCH) * math.ceil(h / IMAGE_PATCH)


def _strip_invisible(text: str) -> str:
    # Format characters (zero-width space, soft hyphen, BOM, bidi controls) can hide a tag
    # from the pattern while a model still reads it. They carry nothing a clerk needs.
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _base_name(file_name: str) -> str:
    # Only the name: folder names can carry more personal detail than the user meant to send.
    name = re.split(r"[\\/]", file_name)[-1]
    name = "".join(" " if unicodedata.category(ch)[0] in "CZ" and ch != " " else ch for ch in name)
    return " ".join(name.split())


def _defuse(text: str) -> str:
    """Make anything shaped like our tags harmless: "</document>" → "‹/document>"."""
    return _TAG_LOOKALIKE.sub(r"‹\1", _strip_invisible(text))


def _pages(page_nos: list[int]) -> str:
    """[3, 4, 5, 9] → "pages 3-5, 9"."""
    runs: list[list[int]] = []
    for n in page_nos:
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    parts = [str(r[0]) if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in runs]
    return ("page " if len(page_nos) == 1 else "pages ") + ", ".join(parts)


# --- images ------------------------------------------------------------------------------


@dataclass
class _Budget:
    tokens: int
    images: int = 0
    bytes: int = 0

    def take(self, jpeg: bytes) -> bool:
        cost = _image_tokens(jpeg)
        if (
            self.images >= MAX_IMAGES
            or self.bytes + len(jpeg) > MAX_IMAGE_BYTES
            or cost > self.tokens
        ):
            return False
        self.images += 1
        self.bytes += len(jpeg)
        self.tokens -= cost
        return True


def _images_for(
    mime_type: str, data: bytes, pages: list[int], token_budget: int
) -> tuple[list[ImageInput], list[str]]:
    budget = _Budget(token_budget)
    images: list[ImageInput] = []
    unread: list[str] = []

    def add(page_no: int, jpeg: bytes | None, problem: str | None = None) -> bool:
        """Returns False once the request is full."""
        if problem:
            unread.append(f"page {page_no}: {problem}")
        if jpeg is None:
            if not problem:
                unread.append(f"page {page_no}: scanned, but no image could be made from it")
            return True
        if not budget.take(jpeg):
            return False
        images.append(ImageInput(page_no, jpeg))
        return True

    def over_limit(page_nos: list[int]) -> None:
        if page_nos:
            unread.append(f"{_pages(page_nos)}: over what one request can carry, not sent")

    if mime_type == "application/pdf":
        with contextlib.closing(_render_pdf_pages(data, pages)) as rendered:
            for i, (page_no, jpeg) in enumerate(rendered):
                if not add(page_no, jpeg):
                    over_limit(pages[i:])
                    break
    elif mime_type.startswith("image/"):
        jpeg, problem = _normalize(data)
        if not add(1, jpeg, problem):
            over_limit([1])
    elif mime_type.endswith("wordprocessingml.document"):
        found = _docx_images(data)
        if not found:
            add(1, None)
        for jpeg, problem in found:
            if not add(1, jpeg, problem):
                over_limit([1])
                break
    else:
        for page_no in pages:
            add(page_no, None)
    return images, unread


_JPEG_FORMATS = ("JPEG", "MPO")  # MPO: a camera JPEG with a preview or gain map attached
_ANIMATED_FORMATS = ("GIF", "WEBP", "PNG")


def _normalize(data: bytes) -> tuple[bytes | None, str | None]:
    """Any image file → upright RGB JPEG, long edge at most IMAGE_LONG_EDGE.

    Returns the JPEG, or None, and a note when something about the image wasn't sent (later
    frames of an animation). A broken file gives (None, None)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            is_jpeg = img.format in _JPEG_FORMATS
            if not is_jpeg and img.width * img.height > MAX_NON_JPEG_PIXELS:
                return None, "image too large to decode, not sent"
            note = None
            if img.format in _ANIMATED_FORMATS and getattr(img, "is_animated", False):
                note = "animated image: only its first frame was sent"
            try:
                orientation = img.getexif().get(0x0112, 1)
            except Exception:  # broken EXIF from editors and messengers: send it as stored
                orientation = 1
            img.draft("RGB", (IMAGE_LONG_EDGE, IMAGE_LONG_EDGE))  # fast shrink for big JPEGs
            img.load()
            small = _shrink(img)
            if orientation not in (None, 1):
                try:
                    small = ImageOps.exif_transpose(small)  # phone photos: apply the rotation
                except Exception:
                    pass
            return _to_jpeg(small), note
    except Exception:  # Pillow raises many types for damaged files
        return None, None


def _shrink(img: Image.Image) -> Image.Image:
    """At most IMAGE_LONG_EDGE, in one step and without extra full-size copies."""
    if img.mode in ("I;16", "I;16L", "I;16B", "I;16N", "I", "F"):
        img = _to_8bit(img)
    if max(img.size) > IMAGE_LONG_EDGE:
        img.thumbnail((IMAGE_LONG_EDGE, IMAGE_LONG_EDGE), Image.Resampling.LANCZOS)
    return img


def _to_jpeg(img: Image.Image) -> bytes:
    """The image is the caller's own and may be changed in place."""
    img = _shrink(img)
    if img.mode in ("RGBA", "LA", "PA", "P") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, "white")
        img.paste(rgba, mask=rgba.getchannel("A"))  # transparent → white paper
    elif img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def _to_8bit(img: Image.Image) -> Image.Image:
    """16-bit and float greyscale (some scanners) → 8-bit, stretched to the values used.
    A plain convert would clip everything above 255 to white."""
    if img.mode.startswith("I;16"):
        img = img.convert("I")
    img = img.convert("F")
    low, high = img.getextrema()
    if high <= low:
        return Image.new("L", img.size, 255 if high > 0 else 0)
    scale = 255 / (high - low)
    return img.point(lambda v: (v - low) * scale).convert("L")


def _render_pdf_pages(data: bytes, pages: list[int]):
    """Yields (page number, JPEG or None) one page at a time, so a long scan isn't held in
    memory and the caller can stop when the request is full. PDFium calls hold the lock;
    encoding doesn't."""
    import pypdfium2 as pdfium

    from paperclerk.ingest.parsers.pdf import PDFIUM_LOCK

    with PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError:
            pdf = None
    if pdf is None:
        for page_no in pages:
            yield page_no, None
        return
    try:
        for page_no in pages:
            pil = None
            with PDFIUM_LOCK:
                try:
                    page = pdf[page_no - 1]
                    width, height = page.get_size()
                    scale = min(IMAGE_LONG_EDGE / max(width, height, 1), MAX_RENDER_DPI / 72)
                    bitmap = page.render(scale=scale)
                    try:
                        pil = bitmap.to_pil()  # a copy: safe to use after close
                    finally:
                        bitmap.close()
                except (pdfium.PdfiumError, IndexError):
                    pil = None
            yield page_no, (_to_jpeg(pil) if pil is not None else None)
    finally:
        with PDFIUM_LOCK:
            pdf.close()


def _docx_images(data: bytes) -> list[tuple[bytes | None, str | None]]:
    """Pictures in the body, in reading order, skipping tracked deletions."""
    import docx

    from paperclerk.ingest.parsers.docx import IMAGE_TAGS

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception:  # python-docx raises many types for a damaged file
        return [(None, None)]
    found: list[tuple[bytes | None, str | None]] = []
    seen: set[str] = set()
    for el in document.element.body.iter(*IMAGE_TAGS):
        if any(a.tag == _W_DEL for a in el.iterancestors()):
            continue
        rid = el.get(f"{_R}embed") or el.get(f"{_R}id")
        if not rid:
            if el.get(f"{_R}link"):
                found.append((None, "a picture linked from outside the file, not in it"))
            continue
        if rid in seen:
            continue
        seen.add(rid)
        part = document.part.related_parts.get(rid)
        if part is None or getattr(part, "is_external", False):
            found.append((None, "a picture linked from outside the file, not in it"))
            continue
        found.append(_normalize(part.blob))
    return found
