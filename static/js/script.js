// ---------------------------------------------------------------------------
// FlaskGPT — interface du chat
// ---------------------------------------------------------------------------

const state = {
    convId: null,
    busy: false,
    selectedSources: new Set(), // ids des sources cochées
    sources: [],
    domains: {},
};

const $ = (selector, root = document) => root.querySelector(selector);

// Crée un élément avec classes et texte (le texte n'est jamais interprété comme du HTML).
function h(tag, className = "", text = "") {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text) el.textContent = text;
    return el;
}

class AppError extends Error {}

// --- Couleurs --------------------------------------------------------------
// Les classes sont écrites en entier pour que Tailwind les détecte.

const RATING_BORDER = {
    0: "border-l-slate-300",
    1: "border-l-red-500", 2: "border-l-red-400",
    3: "border-l-amber-400",
    4: "border-l-green-400", 5: "border-l-green-500",
};
const RATING_BUTTON = {
    1: "bg-red-100 text-red-800 ring-red-500", 2: "bg-red-50 text-red-700 ring-red-400",
    3: "bg-amber-100 text-amber-800 ring-amber-500",
    4: "bg-green-50 text-green-700 ring-green-400", 5: "bg-green-100 text-green-800 ring-green-500",
};
const RATING_LABEL = {
    1: "Insuffisante", 2: "Insuffisante", 3: "Moyenne", 4: "Satisfaisante", 5: "Satisfaisante",
};

function scoreStyle(score) {
    if (score >= 85) return { text: "text-green-700", bar: "bg-green-500", chip: "bg-green-100 text-green-800" };
    if (score >= 65) return { text: "text-amber-700", bar: "bg-amber-500", chip: "bg-amber-100 text-amber-800" };
    return { text: "text-red-700", bar: "bg-red-500", chip: "bg-red-100 text-red-800" };
}

// --- Réseau et erreurs -----------------------------------------------------

function showError(message) {
    $("#error-text").textContent = message;
    $("#error-banner").classList.replace("hidden", "flex");
}

function clearError() {
    $("#error-banner").classList.replace("flex", "hidden");
}

// fetch qui transforme tout échec (réseau ou HTTP) en AppError lisible.
async function api(path, options) {
    let response;
    try {
        response = await fetch(path, options);
    } catch {
        throw new AppError("Impossible de joindre le serveur. Vérifiez qu'il est démarré et que votre connexion fonctionne.");
    }
    if (!response.ok) {
        let message = `Erreur serveur (${response.status}).`;
        try {
            message = (await response.json()).error.message;
        } catch { /* réponse non JSON : on garde le message générique */ }
        throw new AppError(message);
    }
    return response;
}

const jsonOptions = (method, body) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
});

// Télécharge un fichier renvoyé par le serveur en affichant les erreurs proprement.
async function downloadFrom(url) {
    const response = await api(url);
    const disposition = response.headers.get("Content-Disposition") || "";
    const utf8Name = /filename\*=UTF-8''([^;]+)/.exec(disposition);
    const name = utf8Name ? decodeURIComponent(utf8Name[1]) : "export";
    const link = h("a");
    link.href = URL.createObjectURL(await response.blob());
    link.download = name;
    link.click();
    URL.revokeObjectURL(link.href);
}

// --- Markdown --------------------------------------------------------------

let converter;

function renderMarkdown(element, text, sources = [], highlight = false) {
    converter ??= new showdown.Converter({ tables: true, strikethrough: true, simplifiedAutoLink: true });
    element.innerHTML = DOMPurify.sanitize(converter.makeHtml(text));
    if (sources.length) linkCitations(element, new Set(sources.map(s => s.ref)));
    if (highlight) element.querySelectorAll("pre code").forEach(block => hljs.highlightElement(block));
}

// Transforme les « [2] » du texte en liens vers l'extrait correspondant (hors code).
function linkCitations(element, refs) {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
        if (!walker.currentNode.parentElement.closest("pre, code, a")) nodes.push(walker.currentNode);
    }
    for (const node of nodes) {
        if (!/\[\d+]/.test(node.nodeValue)) continue;
        const fragment = document.createDocumentFragment();
        for (const part of node.nodeValue.split(/(\[\d+])/)) {
            const ref = /^\[(\d+)]$/.exec(part)?.[1];
            if (ref && refs.has(Number(ref))) {
                const link = h("a", "cite cursor-pointer font-semibold text-indigo-600 hover:underline", part);
                link.dataset.ref = ref;
                fragment.append(link);
            } else {
                fragment.append(part);
            }
        }
        node.replaceWith(fragment);
    }
}

// --- Messages --------------------------------------------------------------

function addMessage(role, text) {
    const root = h("article", role === "user"
        ? "mb-4 rounded-2xl border border-slate-200 bg-slate-100 p-5"
        : `mb-4 rounded-2xl border border-slate-200 border-l-4 ${RATING_BORDER[0]} bg-white p-5 shadow-xs`);
    root.dataset.role = role;
    const body = h("div", "message leading-relaxed break-words");
    if (role === "user") body.textContent = text;
    else renderMarkdown(body, text);
    root.append(body);
    $("#gpt-output").append(root);
    root.scrollIntoView({ block: "end", behavior: "smooth" });
    return { root, body };
}

function setRatingColor(root, rating) {
    root.classList.remove(...Object.values(RATING_BORDER));
    root.classList.add(RATING_BORDER[rating || 0]);
}

// Ajoute métriques, notation et sources sous une réponse de l'assistant.
function decorateAssistant({ root, body }, data, text) {
    root.dataset.messageId = data.id;
    renderMarkdown(body, text, data.sources || [], true);

    const total = (data.prompt_tokens || 0) + (data.completion_tokens || 0);
    const speed = data.response_ms && data.completion_tokens
        ? ` · ${(data.completion_tokens / (data.response_ms / 1000)).toFixed(1)} tokens/s` : "";
    const metrics = h("p", "mt-3 text-xs text-slate-500",
        `⏱ ${(data.response_ms / 1000).toFixed(1)} s · ${total} tokens ` +
        `(entrée ${data.prompt_tokens}, sortie ${data.completion_tokens})${speed}`);
    root.append(metrics);

    root.append(buildRating(data.id, data.rating, root));
    root.append(buildSourcesBlock(data));
    setRatingColor(root, data.rating);
}

function buildRating(messageId, current, root) {
    const wrap = h("div", "mt-2 flex flex-wrap items-center gap-1 text-xs");
    wrap.append(h("span", "mr-1 text-slate-500", "Noter :"));
    const label = h("span", "ml-2 font-medium text-slate-600", current ? RATING_LABEL[current] : "");
    const buttons = [];

    const paint = rating => {
        buttons.forEach((button, i) => {
            const selected = i + 1 === rating;
            button.className = `h-7 w-7 rounded-full font-semibold ${RATING_BUTTON[i + 1]} ` +
                (selected ? "ring-2" : "opacity-60 hover:opacity-100");
            button.setAttribute("aria-pressed", selected);
        });
        label.textContent = rating ? RATING_LABEL[rating] : "";
    };

    for (let value = 1; value <= 5; value++) {
        const button = h("button", "", String(value));
        button.type = "button";
        button.title = RATING_LABEL[value];
        button.addEventListener("click", async () => {
            try {
                await api(`/api/messages/${messageId}/rating`, jsonOptions("POST", { rating: value }));
                paint(value);
                setRatingColor(root, value);
                refreshStats();
            } catch (error) {
                showError(error.message);
            }
        });
        buttons.push(button);
        wrap.append(button);
    }
    wrap.append(label);
    paint(current);
    return wrap;
}

// --- Sources sous une réponse ----------------------------------------------

function displaySettings() {
    return {
        unit: $("#src-unit").value,
        count: Math.max(1, Number($("#src-count").value) || 1),
    };
}

function buildSourcesBlock(data) {
    const details = h("details", "mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs");
    details.dataset.sourcesBlock = "1";
    details.dataset.messageId = data.id;
    details._passages = data.sources || [];
    details._hidden = new Set(); // fichiers décochés dans ce bloc
    details._forced = null;      // extrait demandé via une citation
    const count = details._passages.length;
    details.append(h("summary", "cursor-pointer font-medium text-slate-600",
        count ? `Sources utilisées (${count} passage${count > 1 ? "s" : ""})` : "Sources utilisées"));
    details.append(h("div", "sources-content mt-2 space-y-3"));
    renderSources(details);
    return details;
}

// Sélectionne ce qui doit être montré pour une source selon l'unité choisie.
function visibleItems(passages, { unit, count }, forcedRef) {
    if (unit === "lignes") {
        const lines = passages.flatMap(p => p.passage.split("\n")
            .map((text, i) => ({ ref: p.ref, line: p.line_start + i, text })));
        return lines.filter(l => l.text.trim()).slice(0, count);
    }
    let chosen = passages.slice(0, count);
    const forced = passages.find(p => p.ref === forcedRef);
    if (forced && !chosen.includes(forced)) chosen = [...chosen, forced];
    return chosen.map(p => ({
        ref: p.ref, line: p.line_start, id: p.ref,
        text: unit === "extraits" && p.passage.length > 220 ? p.passage.slice(0, 220).trimEnd() + "…" : p.passage,
    }));
}

function renderSources(details) {
    const content = $(".sources-content", details);
    content.replaceChildren();
    const passages = details._passages;
    if (!passages.length) {
        content.append(h("p", "text-slate-500",
            "Aucune source documentaire n'a été utilisée : cette réponse repose sur les connaissances générales du modèle. " +
            "Importez et cochez des documents pour obtenir des réponses vérifiables."));
        return;
    }
    const settings = displaySettings();
    const byFile = Map.groupBy(passages, p => p.filename);
    for (const [filename, items] of byFile) {
        const block = h("div");
        const header = h("label", "flex items-center gap-2 font-semibold text-slate-700");
        const checkbox = h("input");
        checkbox.type = "checkbox";
        checkbox.checked = !details._hidden.has(filename);
        checkbox.addEventListener("change", () => {
            if (checkbox.checked) details._hidden.delete(filename);
            else details._hidden.add(filename);
            renderSources(details);
        });
        header.append(checkbox, h("span", "", filename));
        block.append(header);

        if (checkbox.checked) {
            for (const item of visibleItems(items, settings, details._forced)) {
                const row = h("p", "mt-1 whitespace-pre-wrap rounded border-l-2 border-indigo-300 bg-white px-2 py-1");
                row.id = `src-${details.dataset.messageId}-${item.ref}${settings.unit === "lignes" ? "-" + item.line : ""}`;
                row.dataset.ref = item.ref;
                row.append(h("span", "mr-1 font-semibold text-indigo-600", `[${item.ref}]`),
                    h("span", "mr-2 text-slate-400", `l.${item.line}`), item.text);
                block.append(row);
            }
        }
        content.append(block);
    }
}

function refreshAllSources() {
    document.querySelectorAll("[data-sources-block]").forEach(renderSources);
}

// Clic sur une citation [n] : ouvre les sources et met le passage en évidence.
function jumpToPassage(link) {
    const details = $("[data-sources-block]", link.closest("article"));
    const ref = Number(link.dataset.ref);
    const passage = details._passages.find(p => p.ref === ref);
    details._hidden.delete(passage.filename);
    details._forced = ref;
    details.open = true;
    renderSources(details);
    const target = details.querySelector(`[data-ref="${ref}"]`);
    if (!target) return;
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    target.classList.add("ring-2", "ring-indigo-400");
    setTimeout(() => target.classList.remove("ring-2", "ring-indigo-400"), 1800);
}

// --- Chargement d'une conversation -----------------------------------------

function renderConversation(messages) {
    $("#gpt-output").replaceChildren();
    for (const m of messages) {
        const parts = addMessage(m.role, m.content);
        if (m.role === "assistant") decorateAssistant(parts, m, m.content);
    }
}

async function openConversation(id) {
    const { conversation, messages } = await (await api(`/api/conversations/${id}`)).json();
    state.convId = conversation.id;
    $("#domain").value = conversation.domain;
    try { localStorage.setItem("convId", conversation.id); } catch { /* stockage indisponible */ }
    renderConversation(messages);
    refreshHistory();
    refreshStats();
}

async function startNewConversation() {
    const domain = $("#domain").value;
    const conv = await (await api("/api/conversations", jsonOptions("POST", { domain }))).json();
    await openConversation(conv.id);
}

// --- Envoi d'une question --------------------------------------------------

// Lit le flux NDJSON ligne par ligne et appelle onEvent pour chaque événement.
async function readEvents(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop(); // dernière ligne possiblement incomplète
            for (const line of lines) if (line.trim()) onEvent(JSON.parse(line));
        }
        if (buffer.trim()) onEvent(JSON.parse(buffer));
    } finally {
        reader.releaseLock();
    }
}

async function sendPrompt(prompt) {
    const userMsg = addMessage("user", prompt);
    const assistant = addMessage("assistant", "");
    assistant.body.textContent = "…";
    let text = "";
    let finished = false;

    try {
        const response = await api(`/api/conversations/${state.convId}/chat`, jsonOptions("POST", {
            prompt, source_ids: [...state.selectedSources],
        }));
        await readEvents(response, event => {
            if (event.type === "delta") {
                text += event.text;
                renderMarkdown(assistant.body, text);
            } else if (event.type === "error") {
                throw new AppError(event.message);
            } else if (event.type === "done") {
                finished = true;
                decorateAssistant(assistant, { ...event, id: event.message_id }, text);
            }
        });
        if (!finished) throw new AppError("La réponse a été interrompue avant la fin. Réessayez.");
        refreshHistory();
        refreshStats();
    } catch (error) {
        // Rien n'a été enregistré côté serveur : on retire les bulles et on rend la question.
        userMsg.root.remove();
        assistant.root.remove();
        $("#prompt").value = prompt;
        showError(error instanceof AppError ? error.message : "Une erreur inattendue est survenue.");
        if (!(error instanceof AppError)) console.error(error);
    }
}

// --- Historique ------------------------------------------------------------

async function refreshHistory() {
    const list = $("#history-list");
    try {
        const conversations = await (await api("/api/conversations")).json();
        list.replaceChildren();
        for (const conv of conversations) {
            const item = h("li", "flex items-center gap-1");
            const open = h("button", "min-w-0 flex-1 truncate rounded px-2 py-1 text-left hover:bg-slate-100", conv.title);
            open.type = "button";
            open.title = `${state.domains[conv.domain] || conv.domain} · ${conv.message_count} message(s)`;
            if (conv.id === state.convId) open.classList.add("bg-indigo-50", "font-medium");
            open.addEventListener("click", () => openConversation(conv.id).catch(e => showError(e.message)));
            const remove = h("button", "px-2 text-slate-400 hover:text-red-600", "✕");
            remove.type = "button";
            remove.setAttribute("aria-label", `Supprimer « ${conv.title} »`);
            remove.addEventListener("click", () => deleteConversation(conv.id));
            item.append(open, remove);
            list.append(item);
        }
    } catch (error) {
        showError(error.message);
    }
}

async function deleteConversation(id) {
    if (!confirm("Supprimer définitivement cette conversation ?")) return;
    try {
        await api(`/api/conversations/${id}`, { method: "DELETE" });
        if (id === state.convId) await startNewConversation();
        else refreshHistory();
    } catch (error) {
        showError(error.message);
    }
}

// --- Performances ----------------------------------------------------------

async function refreshStats() {
    const panel = $("#perf-panel");
    try {
        const stats = await (await api(`/api/conversations/${state.convId}/stats`)).json();
        panel.replaceChildren();
        if (!stats.responses.length) {
            panel.append(h("p", "text-xs text-slate-500", "Aucune réponse pour l'instant."));
            return;
        }
        const total = stats.total_prompt_tokens + stats.total_completion_tokens;
        const cards = h("div", "grid grid-cols-3 gap-2 text-center");
        for (const [label, value] of [
            ["Temps moyen", stats.avg_response_ms ? `${(stats.avg_response_ms / 1000).toFixed(1)} s` : "–"],
            ["Tokens", total],
            ["Note moy.", stats.avg_rating ?? "–"],
        ]) {
            const card = h("div", "rounded-lg bg-slate-50 p-2");
            card.append(h("div", "text-base font-semibold", String(value)), h("div", "text-xs text-slate-500", label));
            cards.append(card);
        }
        panel.append(cards);

        const maxMs = Math.max(...stats.responses.map(r => r.response_ms || 0), 1);
        const table = h("table", "mt-3 w-full text-xs");
        const head = h("tr", "text-left text-slate-500");
        ["#", "Temps", "Tokens (E/S)", "Note"].forEach(t => head.append(h("th", "pb-1 font-medium", t)));
        table.append(head);
        stats.responses.forEach((r, i) => {
            const row = h("tr", "border-t border-slate-100");
            const time = h("td", "py-1 pr-2");
            time.append(h("div", "", `${((r.response_ms || 0) / 1000).toFixed(1)} s`));
            const bar = h("div", "h-1 rounded bg-indigo-400");
            bar.style.width = `${Math.round(((r.response_ms || 0) / maxMs) * 100)}%`;
            time.append(bar);
            const rating = h("td", "py-1");
            rating.append(r.rating
                ? h("span", `rounded-full px-2 py-0.5 ${RATING_BUTTON[r.rating].split(" ").slice(0, 2).join(" ")}`, String(r.rating))
                : h("span", "text-slate-400", "–"));
            row.append(h("td", "py-1 pr-2 text-slate-500", String(i + 1)), time,
                h("td", "py-1 pr-2", `${r.prompt_tokens ?? 0} / ${r.completion_tokens ?? 0}`), rating);
            table.append(row);
        });
        panel.append(table);
    } catch (error) {
        showError(error.message);
    }
}

// --- Sources (panneau) -----------------------------------------------------

function renderSourceList() {
    const list = $("#sources-list");
    list.replaceChildren();
    if (!state.sources.length) {
        list.append(h("li", "text-xs text-slate-400", "Aucune source importée."));
        return;
    }
    for (const source of state.sources) {
        const item = h("li", "flex items-center gap-2");
        const label = h("label", "flex min-w-0 flex-1 items-center gap-2");
        const checkbox = h("input");
        checkbox.type = "checkbox";
        checkbox.checked = state.selectedSources.has(source.id);
        checkbox.addEventListener("change", () => {
            if (checkbox.checked) state.selectedSources.add(source.id);
            else state.selectedSources.delete(source.id);
        });
        label.append(checkbox, h("span", "truncate", source.filename));
        const remove = h("button", "px-2 text-slate-400 hover:text-red-600", "✕");
        remove.type = "button";
        remove.setAttribute("aria-label", `Retirer ${source.filename}`);
        remove.addEventListener("click", async () => {
            try {
                await api(`/api/sources/${source.id}`, { method: "DELETE" });
                state.selectedSources.delete(source.id);
                await loadSources();
            } catch (error) {
                showError(error.message);
            }
        });
        item.append(label, remove);
        list.append(item);
    }
}

async function loadSources(selectId) {
    state.sources = await (await api("/api/sources")).json();
    if (selectId) state.selectedSources.add(selectId);
    renderSourceList();
}

async function uploadSource(input) {
    const file = input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
        const before = new Set(state.sources.map(s => s.id));
        const sources = await (await api("/api/sources", { method: "POST", body: form })).json();
        state.sources = sources;
        sources.filter(s => !before.has(s.id)).forEach(s => state.selectedSources.add(s.id));
        renderSourceList();
    } catch (error) {
        showError(error.message);
    } finally {
        input.value = "";
    }
}

// --- Qualité des données ---------------------------------------------------

const SEVERITY_CHIP = {
    high: "bg-red-100 text-red-800", medium: "bg-amber-100 text-amber-800", low: "bg-slate-100 text-slate-700",
};
const SEVERITY_LABEL = { high: "Élevée", medium: "Moyenne", low: "Faible" };
const INDICATORS = {
    completeness: "Complétude", uniqueness: "Unicité", consistency: "Cohérence", validity: "Conformité",
};

function scoreBar(label, score) {
    const style = scoreStyle(score);
    const row = h("div", "mt-1");
    const top = h("div", "flex justify-between text-xs");
    top.append(h("span", "", label), h("span", `font-semibold ${style.text}`, `${score} %`));
    const track = h("div", "h-1.5 rounded bg-slate-100");
    const fill = h("div", `h-1.5 rounded ${style.bar}`);
    fill.style.width = `${Math.max(0, Math.min(100, score))}%`;
    track.append(fill);
    row.append(top, track);
    return row;
}

function renderDatasetReport(report) {
    const panel = $("#dataset-report");
    panel.replaceChildren();

    const head = h("div", "flex items-center justify-between");
    head.append(h("span", "font-medium", report.filename),
        h("span", `rounded-full px-2 py-0.5 text-xs font-semibold ${scoreStyle(report.overall).chip}`,
            `${report.overall} % · ${report.grade}`));
    panel.append(head);

    for (const table of report.tables) {
        const box = h("div", "mt-3 rounded-lg border border-slate-200 p-3");
        box.append(h("div", "font-semibold", `Table « ${table.name} »`),
            h("div", "text-xs text-slate-500",
                `${table.rows} ligne(s) · ${table.columns} colonne(s) · ${table.duplicate_rows ?? 0} doublon(s) · ${table.missing_cells ?? 0} cellule(s) vide(s)`));
        box.append(scoreBar("Qualité globale", table.scores.overall));
        for (const [key, label] of Object.entries(INDICATORS)) {
            if (key in table.scores) box.append(scoreBar(label, table.scores[key]));
        }

        if (table.columns_detail.length) {
            const cols = h("details", "mt-2 text-xs");
            cols.append(h("summary", "cursor-pointer text-slate-600", "Détail par colonne"));
            const grid = h("table", "mt-1 w-full");
            const header = h("tr", "text-left text-slate-500");
            ["Colonne", "Type", "Vides", "Uniques"].forEach(t => header.append(h("th", "font-medium", t)));
            grid.append(header);
            for (const c of table.columns_detail) {
                const row = h("tr", "border-t border-slate-100");
                row.append(h("td", "py-0.5 pr-2 font-medium", c.name), h("td", "pr-2", c.type),
                    h("td", c.missing ? "pr-2 text-red-700" : "pr-2", `${c.missing} (${c.missing_pct} %)`),
                    h("td", "", String(c.unique)));
                grid.append(row);
            }
            cols.append(grid);
            box.append(cols);
        }

        const issues = h("div", "mt-3 space-y-2");
        if (!table.issues.length) issues.append(h("p", "text-xs text-green-700", "Aucun problème détecté."));
        for (const issue of table.issues) {
            const row = h("div", "rounded bg-slate-50 p-2 text-xs");
            const title = h("div", "flex items-center gap-2");
            title.append(h("span", `rounded px-1.5 py-0.5 font-semibold ${SEVERITY_CHIP[issue.severity]}`, SEVERITY_LABEL[issue.severity]),
                h("span", "font-medium", `${issue.type}${issue.column ? " · " + issue.column : ""}`));
            row.append(title, h("div", "mt-1", issue.message));
            if (issue.examples.length) row.append(h("div", "mt-0.5 text-slate-500", `Ex. : ${issue.examples.join(" ; ")}`));
            issues.append(row);
        }
        box.append(issues);
        panel.append(box);
    }

    const save = h("button", "mt-3 rounded-lg border border-slate-300 px-3 py-1.5 text-xs hover:bg-slate-100", "Télécharger le rapport (JSON)");
    save.type = "button";
    save.addEventListener("click", () => {
        const link = h("a");
        link.href = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
        link.download = `qualite_${report.filename}.json`;
        link.click();
        URL.revokeObjectURL(link.href);
    });
    panel.append(save);
}

async function analyzeDataset(input) {
    const file = input.files[0];
    if (!file) return;
    const panel = $("#dataset-report");
    panel.replaceChildren(h("p", "text-xs text-slate-500", "Analyse en cours…"));
    const form = new FormData();
    form.append("file", file);
    try {
        renderDatasetReport(await (await api("/api/datasets/analyze", { method: "POST", body: form })).json());
    } catch (error) {
        panel.replaceChildren();
        showError(error.message);
    } finally {
        input.value = "";
    }
}

// --- Initialisation --------------------------------------------------------

document.addEventListener("DOMContentLoaded", async () => {
    const form = $("#prompt-form");
    const sendButton = $("button", form);
    for (const option of $("#domain").options) state.domains[option.value] = option.textContent;

    $("#error-close").addEventListener("click", clearError);

    form.addEventListener("submit", async event => {
        event.preventDefault();
        const prompt = form.elements.prompt.value.trim();
        if (!prompt || state.busy) return;
        clearError();
        form.reset();
        state.busy = sendButton.disabled = true;
        try {
            await sendPrompt(prompt);
        } finally {
            state.busy = sendButton.disabled = false;
            $("#prompt").focus();
        }
    });

    $("#gpt-output").addEventListener("click", event => {
        const link = event.target.closest(".cite");
        if (link) jumpToPassage(link);
    });

    $("#new-chat").addEventListener("click", async () => {
        const hasMessages = $("#gpt-output").children.length > 0;
        if (hasMessages && !confirm("Effacer la conversation en cours et en démarrer une nouvelle ?")) return;
        try {
            const previous = state.convId;
            await startNewConversation();
            if (hasMessages) await api(`/api/conversations/${previous}`, { method: "DELETE" });
            clearError();
            refreshHistory();
        } catch (error) {
            showError(error.message);
        }
    });

    $("#domain").addEventListener("change", async event => {
        try {
            await api(`/api/conversations/${state.convId}`, jsonOptions("PATCH", { domain: event.target.value }));
            refreshHistory();
        } catch (error) {
            showError(error.message);
        }
    });

    $("#export-btn").addEventListener("click", async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            await downloadFrom(`/api/conversations/${state.convId}/export?format=${$("#export-format").value}`);
        } catch (error) {
            showError(error.message);
        } finally {
            button.disabled = false;
        }
    });

    $("#summary-btn").addEventListener("click", async event => {
        const button = event.currentTarget;
        const label = button.textContent;
        button.disabled = true;
        button.textContent = "Génération…";
        try {
            await downloadFrom(`/api/conversations/${state.convId}/summary.pdf`);
        } catch (error) {
            showError(error.message);
        } finally {
            button.disabled = false;
            button.textContent = label;
        }
    });

    $("#source-file").addEventListener("change", event => uploadSource(event.target));
    $("#dataset-file").addEventListener("change", event => analyzeDataset(event.target));
    $("#src-unit").addEventListener("change", refreshAllSources);
    $("#src-count").addEventListener("input", refreshAllSources);

    try {
        await loadSources();
        state.sources.forEach(s => state.selectedSources.add(s.id));
        renderSourceList();

        let saved = null;
        try { saved = localStorage.getItem("convId"); } catch { /* stockage indisponible */ }
        if (saved) {
            try { await openConversation(saved); return; } catch { /* conversation supprimée : on en crée une */ }
        }
        await startNewConversation();
    } catch (error) {
        showError(error.message);
    }
});
