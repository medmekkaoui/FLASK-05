"""Appels à l'API Anthropic (Claude) : domaines, streaming, tokens et erreurs lisibles."""
import os

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
MAX_TOKENS = 16000

DOMAINS = {
    "general": {
        "label": "Général",
        "prompt": "Tu es un assistant polyvalent, clair et précis.",
    },
    "informatique": {
        "label": "Informatique",
        "prompt": (
            "Tu es un ingénieur logiciel senior. Réponds avec rigueur technique, "
            "donne des exemples de code commentés dans des blocs Markdown, "
            "mentionne les bonnes pratiques, la sécurité et les cas limites."
        ),
    },
    "finance": {
        "label": "Finance",
        "prompt": (
            "Tu es un analyste financier. Utilise le vocabulaire financier exact "
            "(ratios, flux, risque, rendement), explique tes calculs étape par étape "
            "et rappelle que tes réponses ne constituent pas un conseil en investissement."
        ),
    },
    "data": {
        "label": "Analyse de données",
        "prompt": (
            "Tu es un data analyst / data steward. Raisonne en termes de qualité des "
            "données, statistiques, nettoyage, visualisation et gouvernance. "
            "Propose des requêtes SQL ou du code pandas quand c'est utile."
        ),
    },
    "juridique": {
        "label": "Juridique",
        "prompt": (
            "Tu es un juriste. Structure tes réponses, cite les notions de droit "
            "pertinentes et précise que tes réponses ne remplacent pas l'avis d'un avocat."
        ),
    },
    "education": {
        "label": "Éducation",
        "prompt": (
            "Tu es un enseignant pédagogue. Explique simplement, progressivement, "
            "avec des exemples concrets, puis propose une courte question de vérification."
        ),
    },
}

BASE_RULES = (
    "Réponds en français, sauf si l'utilisateur écrit dans une autre langue. "
    "Utilise le Markdown pour la mise en forme."
)

SOURCES_RULES = (
    "Des extraits de documents sources numérotés sont fournis ci-dessous. "
    "Appuie-toi dessus en priorité et cite le numéro de l'extrait utilisé entre crochets, "
    "par exemple [1]. Si les extraits ne contiennent pas la réponse, dis-le explicitement "
    "avant de répondre avec tes connaissances générales."
)


class UserFacingError(Exception):
    """Erreur dont le message peut être affiché tel quel à l'utilisateur."""

    def __init__(self, code, message, status=500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def translate_error(exc):
    """Convertit une exception Anthropic/réseau en UserFacingError compréhensible."""
    if isinstance(exc, UserFacingError):
        return exc
    if isinstance(exc, anthropic.AuthenticationError):
        return UserFacingError(
            "invalid_api_key",
            "Clé API invalide ou révoquée. Vérifiez ANTHROPIC_API_KEY dans le fichier .env, "
            "puis redémarrez l'application.", 401)
    if isinstance(exc, anthropic.PermissionDeniedError):
        return UserFacingError(
            "permission_denied",
            "Cette clé API n'a pas accès au modèle demandé. Changez ANTHROPIC_MODEL ou de clé.", 403)
    if isinstance(exc, anthropic.NotFoundError):
        return UserFacingError(
            "model_not_found",
            f"Le modèle « {MODEL} » est introuvable. Corrigez ANTHROPIC_MODEL dans .env "
            "(par exemple claude-opus-5-5, claude-sonnet-5-5 ou claude-haiku-5-5).", 404)
    if isinstance(exc, anthropic.RateLimitError):
        return UserFacingError(
            "rate_limited",
            "Trop de requêtes en peu de temps (quota de débit atteint). "
            "Patientez quelques secondes puis réessayez.", 429)
    if isinstance(exc, anthropic.APIConnectionError):  # inclut APITimeoutError
        return UserFacingError(
            "connection_error",
            "Impossible de joindre le service Anthropic. Vérifiez votre connexion Internet "
            "(ou proxy/pare-feu) puis réessayez.", 503)
    if isinstance(exc, anthropic.BadRequestError):
        text = str(exc).lower()
        if "credit balance" in text or "billing" in text:
            return UserFacingError(
                "quota_exceeded",
                "Crédit Anthropic épuisé : ajoutez du crédit dans Console > Plans & Billing, "
                "puis réessayez.", 402)
        if "prompt is too long" in text or ("context" in text and "exceed" in text):
            return UserFacingError(
                "context_too_long",
                "La conversation (ou les sources) est trop longue pour le modèle. "
                "Démarrez une nouvelle conversation ou réduisez les sources cochées.", 400)
        return UserFacingError("invalid_request", f"Requête refusée par l'API : {exc.message}", 400)
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in (500, 502, 503, 504, 529):
            return UserFacingError(
                "service_unavailable",
                "Le service Anthropic est surchargé ou momentanément indisponible. "
                "Réessayez dans quelques instants.", 503)
        return UserFacingError(
            "api_error", f"Erreur de l'API Anthropic ({exc.status_code}) : {exc.message}", 502)
    return UserFacingError("unexpected", "Une erreur inattendue est survenue côté serveur.", 500)


# --- Client ----------------------------------------------------------------

_client = None


def get_client():
    global _client
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise UserFacingError(
            "missing_api_key",
            "Aucune clé API configurée. Créez un fichier .env (voir .env.example) "
            "contenant ANTHROPIC_API_KEY=votre_clé, puis redémarrez l'application.", 401)
    if _client is None:
        _client = anthropic.Anthropic(api_key=key, timeout=120.0)
    return _client


def estimate_tokens(text):
    """Estimation de secours (~4 caractères/token) si l'API ne renvoie pas l'usage."""
    return max(1, len(text or "") // 4)


def split_system(messages):
    """L'API Anthropic prend le prompt système à part, hors de la liste des messages."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    chat = [m for m in messages if m["role"] != "system"]
    while chat and chat[0]["role"] != "user":  # l'API exige un premier message « user »
        chat.pop(0)
    return system, chat


# --- Prompt et appels ------------------------------------------------------

def build_system_prompt(domain, passages):
    parts = [DOMAINS.get(domain, DOMAINS["general"])["prompt"], BASE_RULES]
    if passages:
        parts.append(SOURCES_RULES)
        parts.append("\n\n".join(
            f"[{p['ref']}] (fichier : {p['filename']})\n{p['text']}" for p in passages))
    return "\n\n".join(parts)


class ChatStream:
    """Itérable de fragments de texte ; `usage` = (tokens entrée, tokens sortie) après la fin."""

    def __init__(self, messages):
        self.system, self.messages = split_system(messages)
        self.usage = None

    def __iter__(self):
        client = get_client()
        try:
            with client.messages.stream(
                model=MODEL, max_tokens=MAX_TOKENS, system=self.system, messages=self.messages,
            ) as stream:
                yield from stream.text_stream
                usage = stream.get_final_message().usage
            prompt = (usage.input_tokens + (usage.cache_read_input_tokens or 0)
                      + (usage.cache_creation_input_tokens or 0))
            self.usage = (prompt, usage.output_tokens)
        except Exception as exc:
            raise translate_error(exc) from exc


def stream_chat(messages):
    return ChatStream(messages)


def complete(messages):
    """Appel non-streamé (résumés). Lève UserFacingError."""
    system, chat = split_system(messages)
    client = get_client()
    try:
        with client.messages.stream(
            model=MODEL, max_tokens=MAX_TOKENS, system=system, messages=chat,
        ) as stream:
            message = stream.get_final_message()
        return "".join(b.text for b in message.content if b.type == "text")
    except Exception as exc:
        raise translate_error(exc) from exc
