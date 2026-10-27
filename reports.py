"""Exports d'une conversation (TXT, JSON, PDF) et résumé PDF."""
import json
import re
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle

ROLE_LABEL = {"user": "Vous", "assistant": "Assistant"}


def _latin(text):
    """Les polices PDF standard ne gèrent que cp1252 : on remplace le reste par « ? »."""
    return (text or "").encode("cp1252", "replace").decode("cp1252")


def _safe_filename(title):
    return re.sub(r"[^\w\-]+", "_", title, flags=re.UNICODE).strip("_")[:50] or "conversation"


def export_filename(conv, ext):
    return f"{_safe_filename(conv['title'])}_{conv['id']}.{ext}"


def to_json(conv, messages):
    payload = {
        "id": conv["id"], "title": conv["title"], "domain": conv["domain"],
        "created_at": conv["created_at"],
        "messages": [
            {k: m[k] for k in ("role", "content", "created_at", "response_ms",
                               "prompt_tokens", "completion_tokens", "rating", "model")}
            | {"sources": [{"ref": s["ref"], "file": s["filename"], "line": s["line_start"],
                            "passage": s["passage"]} for s in m["sources"]]}
            for m in messages
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_txt(conv, messages, domain_label):
    lines = [conv["title"], f"Domaine : {domain_label}", f"Créée le : {conv['created_at']}", "=" * 60, ""]
    for m in messages:
        lines.append(f"[{m['created_at']}] {ROLE_LABEL[m['role']]} :")
        lines.append(m["content"])
        if m["role"] == "assistant":
            meta = []
            if m["response_ms"] is not None:
                meta.append(f"{m['response_ms'] / 1000:.1f} s")
            if m["completion_tokens"] is not None:
                meta.append(f"{(m['prompt_tokens'] or 0) + m['completion_tokens']} tokens")
            if m["rating"]:
                meta.append(f"note {m['rating']}/5")
            if meta:
                lines.append("(" + ", ".join(meta) + ")")
            for s in m["sources"]:
                lines.append(f"  [{s['ref']}] {s['filename']} (ligne {s['line_start']}) : {s['passage'][:200]}")
        lines.append("")
    return "\n".join(lines)


# --- PDF -------------------------------------------------------------------

def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontSize=20, textColor=colors.HexColor("#3730a3")),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], textColor=colors.HexColor("#1e293b")),
        "body": ParagraphStyle("b", parent=base["BodyText"], leading=14),
        "small": ParagraphStyle("s", parent=base["BodyText"], fontSize=8, textColor=colors.HexColor("#64748b")),
        "user": ParagraphStyle("u", parent=base["BodyText"], leading=14, textColor=colors.HexColor("#3730a3")),
        "code": ParagraphStyle("c", parent=base["Code"], fontSize=8, backColor=colors.HexColor("#f1f5f9"),
                               leftIndent=6, rightIndent=6),
    }


def _text_flowables(text, style, code_style):
    """Transforme un texte Markdown simple en paragraphes, blocs de code inclus."""
    flow = []
    for i, part in enumerate(re.split(r"```[^\n]*\n?", text or "")):
        if i % 2:  # contenu situé entre deux ```
            flow.append(Preformatted(_latin(part.strip("\n")), code_style))
        else:
            for para in re.split(r"\n\s*\n", part.strip()):
                if para.strip():
                    safe = escape(_latin(para)).replace("\n", "<br/>")
                    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
                    flow.append(Paragraph(safe, style))
        flow.append(Spacer(1, 4))
    return flow


def _build(story):
    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                      topMargin=2 * cm, bottomMargin=2 * cm).build(story)
    return buf.getvalue()


def _stats_table(stats, messages):
    total = stats["total_prompt_tokens"] + stats["total_completion_tokens"]
    avg = f"{stats['avg_response_ms'] / 1000:.1f} s" if stats["avg_response_ms"] else "n/a"
    rating = f"{stats['avg_rating']}/5" if stats["avg_rating"] else "non noté"
    rows = [
        ["Messages", str(len(messages)), "Temps de réponse moyen", avg],
        ["Tokens (entrée)", str(stats["total_prompt_tokens"]), "Tokens (sortie)", str(stats["total_completion_tokens"])],
        ["Tokens (total)", str(total), "Note moyenne", rating],
    ]
    table = Table([[_latin(c) for c in r] for r in rows], colWidths=[3.5 * cm, 3 * cm, 5 * cm, 3 * cm])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f1f5f9")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    return table


def conversation_pdf(conv, messages, stats, domain_label):
    """Transcription complète."""
    st = _styles()
    story = [Paragraph(escape(_latin(conv["title"])), st["title"]),
             Paragraph(escape(_latin(f"Domaine : {domain_label} — Créée le {conv['created_at']}")), st["small"]),
             Spacer(1, 10), _stats_table(stats, messages), Spacer(1, 14)]
    for m in messages:
        story.append(Paragraph(ROLE_LABEL[m["role"]], st["h2"]))
        story += _text_flowables(m["content"], st["user"] if m["role"] == "user" else st["body"], st["code"])
        for s in m["sources"]:
            story.append(Paragraph(
                escape(_latin(f"[{s['ref']}] {s['filename']} (ligne {s['line_start']}) : {s['passage'][:250]}")),
                st["small"]))
        story.append(Spacer(1, 6))
    return _build(story)


def summary_pdf(conv, messages, stats, domain_label, summary_text, summary_is_ai):
    """Résumé : synthèse, indicateurs, questions posées, sources citées."""
    st = _styles()
    story = [Paragraph(escape(_latin("Résumé — " + conv["title"])), st["title"]),
             Paragraph(escape(_latin(f"Domaine : {domain_label} — Créée le {conv['created_at']}")), st["small"]),
             Spacer(1, 10), Paragraph("Synthèse", st["h2"])]
    story += _text_flowables(summary_text, st["body"], st["code"])
    if not summary_is_ai:
        story.append(Paragraph(
            "Synthèse automatique simplifiée (l'IA n'était pas disponible au moment de l'export).", st["small"]))
    story += [Spacer(1, 8), Paragraph("Indicateurs", st["h2"]), _stats_table(stats, messages),
              Spacer(1, 8), Paragraph("Questions posées", st["h2"])]
    for i, q in enumerate((m for m in messages if m["role"] == "user"), start=1):
        story.append(Paragraph(escape(_latin(f"{i}. {q['content'][:300]}")), st["body"]))
    files = sorted({s["filename"] for m in messages for s in m["sources"]})
    if files:
        story += [Spacer(1, 8), Paragraph("Sources citées", st["h2"])]
        story += [Paragraph(escape(_latin(f"• {f}")), st["body"]) for f in files]
    return _build(story)


def fallback_summary(messages):
    """Résumé extractif sans IA : premières phrases de chaque échange."""
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append(f"Question : {m['content'][:200]}")
        else:
            first = re.split(r"(?<=[.!?])\s", m["content"].strip(), maxsplit=1)[0]
            out.append(f"Réponse : {first[:300]}")
    return "\n\n".join(out) or "Conversation vide."
