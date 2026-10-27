import json
import time
import unicodedata
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()  # doit précéder l'import de llm (lecture de OPENAI_MODEL)

import db  # noqa: E402
import llm  # noqa: E402
import quality  # noqa: E402
import reports  # noqa: E402
import sources as sources_lib  # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 Mo par envoi
db.init_db()

HISTORY_LIMIT = 20       # nombre de messages passés au modèle
MAX_PROMPT_CHARS = 8000
DEFAULT_TITLE = "Nouvelle conversation"


def error(message, status=400, code="bad_request"):
    return jsonify({"error": {"code": code, "message": message}}), status


@app.errorhandler(413)
def too_large(_):
    return error("Fichier trop volumineux (20 Mo maximum).", 413, "too_large")


@app.errorhandler(500)
def server_error(_):
    return error("Erreur interne du serveur.", 500, "server_error")


@app.route("/")
def home():
    domains = [{"key": k, "label": v["label"]} for k, v in llm.DOMAINS.items()]
    return render_template("index.html", domains=domains)


# --- Conversations ---------------------------------------------------------

@app.get("/api/conversations")
def list_conversations():
    return jsonify(db.list_conversations())


@app.post("/api/conversations")
def create_conversation():
    domain = (request.get_json(silent=True) or {}).get("domain", "general")
    if domain not in llm.DOMAINS:
        domain = "general"
    conv_id = db.create_conversation(domain)
    return jsonify(db.get_conversation(conv_id)), 201


@app.get("/api/conversations/<int:conv_id>")
def get_conversation(conv_id):
    conv = db.get_conversation(conv_id)
    if not conv:
        return error("Conversation introuvable.", 404, "not_found")
    return jsonify({"conversation": conv, "messages": db.get_messages(conv_id)})


@app.delete("/api/conversations/<int:conv_id>")
def delete_conversation(conv_id):
    db.delete_conversation(conv_id)
    return "", 204


@app.patch("/api/conversations/<int:conv_id>")
def update_conversation(conv_id):
    if not db.get_conversation(conv_id):
        return error("Conversation introuvable.", 404, "not_found")
    domain = (request.get_json(silent=True) or {}).get("domain")
    if domain not in llm.DOMAINS:
        return error("Domaine inconnu.")
    db.set_domain(conv_id, domain)
    return jsonify(db.get_conversation(conv_id))


# --- Chat ------------------------------------------------------------------

def event(**payload):
    """Une ligne JSON par événement (NDJSON) ; lue progressivement par le navigateur."""
    return json.dumps(payload, ensure_ascii=False) + "\n"


@app.post("/api/conversations/<int:conv_id>/chat")
def chat(conv_id):
    conv = db.get_conversation(conv_id)
    if not conv:
        return error("Conversation introuvable.", 404, "not_found")

    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return error("Le message est vide.")
    if len(prompt) > MAX_PROMPT_CHARS:
        return error(f"Message trop long ({MAX_PROMPT_CHARS} caractères maximum).")

    source_ids = [int(i) for i in body.get("source_ids", []) if str(i).isdigit()]
    history = [{"role": m["role"], "content": m["content"]}
               for m in db.get_messages(conv_id)][-HISTORY_LIMIT:]
    passages = sources_lib.search(prompt, db.get_sources(source_ids))
    messages = ([{"role": "system", "content": llm.build_system_prompt(conv["domain"], passages)}]
                + history + [{"role": "user", "content": prompt}])

    def generate():
        started = time.perf_counter()
        answer = []
        stream = llm.stream_chat(messages)
        try:
            for piece in stream:
                answer.append(piece)
                yield event(type="delta", text=piece)
        except llm.UserFacingError as exc:
            yield event(type="error", code=exc.code, message=exc.message)
            return
        except Exception:  # filet de sécurité : ne jamais couper le flux sans message
            app.logger.exception("Erreur pendant le streaming")
            yield event(type="error", code="unexpected", message="Erreur inattendue côté serveur.")
            return

        text = "".join(answer)
        if not text.strip():
            yield event(type="error", code="empty_answer",
                        message="Le modèle n'a renvoyé aucune réponse. Reformulez votre question.")
            return

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        # Tokens exacts renvoyés par l'API ; estimation seulement en secours.
        prompt_tokens, completion_tokens = stream.usage or (
            llm.estimate_tokens(" ".join(m["content"] for m in messages)), llm.estimate_tokens(text))

        # Persistance seulement en cas de succès : pas de question orpheline sans réponse.
        db.add_message(conv_id, "user", prompt)
        message_id = db.add_message(
            conv_id, "assistant", text, response_ms=elapsed_ms, model=llm.MODEL,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        if passages:
            db.add_message_sources(message_id, passages)
        title = prompt[:60] if conv["title"] == DEFAULT_TITLE else None
        db.touch_conversation(conv_id, title)

        yield event(
            type="done", message_id=message_id, response_ms=elapsed_ms,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            title=title or conv["title"],
            sources=[{"ref": p["ref"], "source_id": p["source_id"], "filename": p["filename"],
                      "line_start": p["line_start"], "passage": p["text"], "score": p["score"]}
                     for p in passages])

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/messages/<int:message_id>/rating")
def rate_message(message_id):
    rating = (request.get_json(silent=True) or {}).get("rating")
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        return error("La note doit être un entier entre 1 et 5.")
    db.set_rating(message_id, rating)
    return jsonify({"message_id": message_id, "rating": rating})


@app.get("/api/conversations/<int:conv_id>/stats")
def conversation_stats(conv_id):
    if not db.get_conversation(conv_id):
        return error("Conversation introuvable.", 404, "not_found")
    return jsonify(db.conversation_stats(conv_id))


# --- Export et résumé ------------------------------------------------------

def download(data, mimetype, filename):
    # En-têtes HTTP en latin-1 : version ASCII + version UTF-8 (RFC 5987) pour les accents.
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    return Response(data, mimetype=mimetype, headers={
        "Content-Disposition":
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"})


@app.get("/api/conversations/<int:conv_id>/export")
def export_conversation(conv_id):
    conv = db.get_conversation(conv_id)
    if not conv:
        return error("Conversation introuvable.", 404, "not_found")
    messages = db.get_messages(conv_id)
    if not messages:
        return error("La conversation est vide : rien à exporter.")
    fmt = request.args.get("format", "txt")
    label = llm.DOMAINS[conv["domain"]]["label"]

    if fmt == "txt":
        return download(reports.to_txt(conv, messages, label).encode("utf-8"),
                        "text/plain; charset=utf-8", reports.export_filename(conv, "txt"))
    if fmt == "json":
        return download(reports.to_json(conv, messages).encode("utf-8"),
                        "application/json", reports.export_filename(conv, "json"))
    if fmt == "pdf":
        pdf = reports.conversation_pdf(conv, messages, db.conversation_stats(conv_id), label)
        return download(pdf, "application/pdf", reports.export_filename(conv, "pdf"))
    return error("Format d'export inconnu (txt, json ou pdf).")


@app.get("/api/conversations/<int:conv_id>/summary.pdf")
def summary_pdf(conv_id):
    conv = db.get_conversation(conv_id)
    if not conv:
        return error("Conversation introuvable.", 404, "not_found")
    messages = db.get_messages(conv_id)
    if not messages:
        return error("La conversation est vide : rien à résumer.")

    transcript = "\n".join(f"{reports.ROLE_LABEL[m['role']]} : {m['content']}" for m in messages)
    try:
        summary = llm.complete([
            {"role": "system", "content": "Résume cette conversation en français en 5 à 8 puces "
                                          "Markdown : sujets abordés, conclusions, points ouverts."},
            {"role": "user", "content": transcript[-12000:]},
        ])
        is_ai = True
    except llm.UserFacingError:
        # Le résumé reste disponible même si l'API est en panne.
        summary, is_ai = reports.fallback_summary(messages), False

    pdf = reports.summary_pdf(conv, messages, db.conversation_stats(conv_id),
                              llm.DOMAINS[conv["domain"]]["label"], summary, is_ai)
    return download(pdf, "application/pdf", reports.export_filename(conv, "pdf").replace(".pdf", "_resume.pdf"))


# --- Sources documentaires -------------------------------------------------

@app.get("/api/sources")
def list_sources():
    return jsonify(db.list_sources())


@app.post("/api/sources")
def upload_source():
    file = request.files.get("file")
    if not file or not file.filename:
        return error("Aucun fichier reçu.")
    try:
        text = sources_lib.extract_text(file.filename, file.read())
    except ValueError as exc:
        return error(str(exc))
    except Exception:
        return error("Impossible de lire ce fichier (corrompu ou protégé ?).")
    if not text.strip():
        return error("Le fichier ne contient aucun texte exploitable.")
    db.add_source(file.filename, text)
    return jsonify(db.list_sources()), 201


@app.delete("/api/sources/<int:source_id>")
def delete_source(source_id):
    db.delete_source(source_id)
    return "", 204


# --- Analyse de données ----------------------------------------------------

@app.post("/api/datasets/analyze")
def analyze_dataset():
    file = request.files.get("file")
    if not file or not file.filename:
        return error("Aucun fichier reçu.")
    try:
        report = quality.analyze_file(file.filename, file.read())
    except ValueError as exc:
        return error(str(exc))
    except Exception:
        app.logger.exception("Analyse impossible")
        return error("Impossible d'analyser ce fichier : format ou contenu invalide.")
    report["id"] = db.save_dataset_report(file.filename, report)
    return jsonify(report)


if __name__ == "__main__":
    app.run(debug=True)
