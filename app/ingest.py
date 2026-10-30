import re
from pathlib import Path

import httpx
from pypdf import PdfReader
from docx import Document as DocxDocument

CHUNK_TOKENS = 450
OVERLAP_TOKENS = 60


class IngestError(Exception):
    pass


def extract_text_from_file(path: Path, filename: str) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            reader = PdfReader(str(path))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        if suffix == ".docx":
            doc = DocxDocument(str(path))
            return "\n\n".join(p.text for p in doc.paragraphs)
        if suffix in (".txt", ".md", ".markdown", ".csv"):
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise IngestError(f"Could not parse {filename}: {e}")
    raise IngestError(f"Unsupported file type: {suffix or '(none)'}. Use PDF, DOCX, TXT or MD.")


def extract_text_from_url(url: str) -> str:
    try:
        import trafilatura
        with httpx.Client(timeout=30, follow_redirects=True,
                          headers={"User-Agent": "QuipeatBot/1.0"}) as client:
            resp = client.get(url)
        resp.raise_for_status()
        downloaded = resp.text
        text = trafilatura.extract(downloaded) or ""
        if len(text) < 200:
            text = re.sub(r"<[^>]+>", " ", downloaded)
            text = re.sub(r"\s+", " ", text)
        if len(text) < 200:
            raise IngestError("Could not extract readable text from that URL.")
        return text
    except IngestError:
        raise
    except Exception as e:
        raise IngestError(f"Could not fetch URL: {e}")


def _tokens(text: str) -> list[str]:
    return text.split()


def chunk_text(text: str, chunk_tokens: int = CHUNK_TOKENS,
               overlap_tokens: int = OVERLAP_TOKENS) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    words = _tokens(text)
    if not words:
        return []
    chunks = []
    step = max(1, chunk_tokens - overlap_tokens)
    for start in range(0, len(words), step):
        piece = " ".join(words[start:start + chunk_tokens])
        if piece:
            chunks.append(piece)
        if start + chunk_tokens >= len(words):
            break
    return chunks
