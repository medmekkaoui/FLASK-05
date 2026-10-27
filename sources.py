"""Sources documentaires : extraction du texte, découpage en passages et recherche."""
import math
import re
import unicodedata
from collections import Counter

MAX_PARAGRAPH = 900  # au-delà, un paragraphe est coupé en morceaux

STOPWORDS = set(
    "le la les un une des de du et en au aux ce ces cet cette que qui quoi dont ou où "
    "est sont été etre être a ont avoir pour par sur sous dans avec sans plus moins "
    "pas ne se sa son ses leur leurs nous vous ils elles il elle on mais donc car "
    "the and for with that this from are was were you your can how what".split()
)


def extract_text(filename, data):
    """Retourne le texte d'un fichier téléversé (txt, md, csv, pdf)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(p for p in pages if p)
    if ext in ("txt", "md", "csv", "json", "log"):
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
    raise ValueError("Format non pris en charge. Utilisez .txt, .md, .csv, .json, .log ou .pdf.")


def _normalize(word):
    word = unicodedata.normalize("NFD", word.lower())
    return "".join(c for c in word if unicodedata.category(c) != "Mn")


def tokenize(text):
    words = (_normalize(w) for w in re.findall(r"\w+", text))
    return [w for w in words if len(w) > 2 and w not in STOPWORDS]


def split_passages(content):
    """Découpe en paragraphes (avec numéro de ligne de départ)."""
    passages = []
    for match in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", content):
        block, start = match.group(), match.start()
        if len(block) <= MAX_PARAGRAPH:
            pieces = [(start, block)]
        else:
            # Coupe aux fins de phrases pour rester lisible.
            pieces, piece_begin = [], 0
            for sentence in re.finditer(r"[^.!?]*[.!?]+\s*|[^.!?]+$", block):
                if sentence.end() - piece_begin > MAX_PARAGRAPH and sentence.start() > piece_begin:
                    pieces.append((start + piece_begin, block[piece_begin:sentence.start()]))
                    piece_begin = sentence.start()
            pieces.append((start + piece_begin, block[piece_begin:]))
        for piece_start, text in pieces:
            piece_start += len(text) - len(text.lstrip())  # ignore les espaces de tête
            text = text.strip()
            if text:
                passages.append({
                    "text": text,
                    "line_start": content.count("\n", 0, piece_start) + 1,
                })
    return passages


def search(question, sources, limit=6):
    """Classe les passages des sources par pertinence (TF-IDF) et garde les meilleurs."""
    query_terms = set(tokenize(question))
    if not query_terms:
        return []

    candidates = []
    for source in sources:
        for p in split_passages(source["content"]):
            candidates.append({
                "source_id": source["id"], "filename": source["filename"], **p,
                "tokens": Counter(tokenize(p["text"])),
            })
    if not candidates:
        return []

    doc_freq = Counter()
    for c in candidates:
        doc_freq.update(set(c["tokens"]) & query_terms)

    n = len(candidates)
    for c in candidates:
        length = sum(c["tokens"].values()) or 1
        c["score"] = sum(
            (c["tokens"][t] / length) * (math.log((n + 1) / (doc_freq[t] + 1)) + 1)
            for t in query_terms if c["tokens"].get(t)
        )

    ranked = sorted((c for c in candidates if c["score"] > 0), key=lambda c: -c["score"])[:limit]
    for ref, c in enumerate(ranked, start=1):
        c["ref"] = ref
        c["score"] = round(c["score"], 4)
        del c["tokens"]
    return ranked
