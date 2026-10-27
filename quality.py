"""Analyse de la qualité et de la gouvernance d'un jeu de données importé."""
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from io import BytesIO, StringIO

import pandas as pd

MAX_ROWS = 200_000
NULL_TOKENS = {"", "null", "none", "nan", "n/a", "na", "-", "--", "#n/a", "nc", "?"}
DATE_SHAPES = {
    "AAAA-MM-JJ": re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([ T].*)?$"),
    "JJ/MM/AAAA": re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"),
    "JJ-MM-AAAA": re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$"),
    "JJ.MM.AAAA": re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$"),
    "AAAA/MM/JJ": re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$"),
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
NON_NEGATIVE_HINTS = ("prix", "price", "montant", "amount", "quantite", "quantité", "qte",
                      "qty", "salaire", "salary", "stock", "total")
WEIGHTS = {"completeness": 0.30, "uniqueness": 0.20, "consistency": 0.25, "validity": 0.25}


# --- Chargement ------------------------------------------------------------

def load_tables(filename, data):
    """Retourne {nom_de_table: DataFrame de chaînes} pour CSV, Excel, JSON ou SQLite."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("csv", "tsv", "txt"):
        text = None
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        frames = {"données": pd.read_csv(
            StringIO(text), sep=None, engine="python", dtype=str,
            keep_default_na=False, nrows=MAX_ROWS)}
    elif ext in ("xlsx", "xlsm"):
        frames = pd.read_excel(BytesIO(data), sheet_name=None, dtype=str, keep_default_na=False)
    elif ext == "json":
        content = json.loads(data.decode("utf-8-sig"))
        if isinstance(content, dict):
            content = next((v for v in content.values() if isinstance(v, list)), [content])
        frames = {"données": pd.json_normalize(content)}
    elif ext in ("db", "sqlite", "sqlite3"):
        frames = _load_sqlite(data)
    else:
        raise ValueError("Format non pris en charge. Utilisez .csv, .xlsx, .json ou .sqlite/.db.")

    tables = {}
    for name, df in frames.items():
        df = df.head(MAX_ROWS)
        df.columns = [str(c) for c in df.columns]
        tables[str(name)] = df.astype("string")
    if not tables:
        raise ValueError("Le fichier ne contient aucune table exploitable.")
    return tables


def _load_sqlite(data):
    # sqlite3 ne lit que depuis un fichier : copie temporaire, ouverte en lecture seule.
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {
                n: pd.read_sql_query(f'SELECT * FROM "{n.replace(chr(34), "")}" LIMIT {MAX_ROWS}', conn)
                for n in names
            }
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"Fichier SQLite illisible : {exc}") from exc
        finally:
            conn.close()
    finally:
        os.remove(path)


# --- Analyse ---------------------------------------------------------------

def _fold(value):
    value = unicodedata.normalize("NFD", value.strip().lower())
    return "".join(c for c in value if unicodedata.category(c) != "Mn")


def _to_number(series):
    cleaned = series.str.strip().str.replace(r"\s", "", regex=True)
    # "12,5" -> "12.5" uniquement quand c'est une décimale à virgule.
    cleaned = cleaned.str.replace(r"^(-?\d+),(\d+)$", r"\1.\2", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def _issue(severity, column, kind, message, count, examples=()):
    return {"severity": severity, "column": column, "type": kind, "message": message,
            "count": int(count), "examples": [str(e) for e in list(examples)[:3]]}


def _analyze_column(name, raw, n_rows):
    """Retourne (détail colonne, anomalies, nb cellules incohérentes, nb cellules invalides)."""
    issues = []
    missing_mask = raw.isna() | raw.str.strip().str.lower().isin(NULL_TOKENS)
    values = raw[~missing_mask].str.strip()
    missing = int(missing_mask.sum())
    inconsistent = invalid = 0
    kind = "texte"

    if missing:
        pct = missing / n_rows * 100
        issues.append(_issue("high" if pct > 20 else "medium" if pct > 5 else "low",
                             name, "valeurs manquantes",
                             f"{missing} valeur(s) manquante(s) ({pct:.1f} %)", missing))

    if len(values):
        numbers = _to_number(values)
        numeric_share = numbers.notna().mean()
        date_hits = {label: values.str.match(rx) for label, rx in DATE_SHAPES.items()}
        date_share = pd.concat(date_hits.values(), axis=1).any(axis=1).mean()

        if numeric_share >= 0.5 and date_share < 0.5:
            kind = "numérique"
            bad = values[numbers.isna()]
            if len(bad):
                inconsistent += len(bad)
                issues.append(_issue("high", name, "types mélangés",
                                     f"{len(bad)} valeur(s) non numérique(s) dans une colonne numérique",
                                     len(bad), bad.unique()))
            clean = numbers.dropna()
            if len(clean) >= 8:
                q1, q3 = clean.quantile(0.25), clean.quantile(0.75)
                iqr = q3 - q1
                outliers = clean[(clean < q1 - 1.5 * iqr) | (clean > q3 + 1.5 * iqr)] if iqr else clean[:0]
                if len(outliers):
                    issues.append(_issue("medium", name, "anomalies",
                                         f"{len(outliers)} valeur(s) aberrante(s) (méthode IQR, "
                                         f"plage normale {q1 - 1.5 * iqr:.4g} à {q3 + 1.5 * iqr:.4g})",
                                         len(outliers), outliers.unique()))
            low = name.lower()
            if any(h in low for h in NON_NEGATIVE_HINTS) and (clean < 0).any():
                n_neg = int((clean < 0).sum())
                invalid += n_neg
                issues.append(_issue("high", name, "non-conformité",
                                     f"{n_neg} valeur(s) négative(s) alors que « {name} » doit être ≥ 0",
                                     n_neg, clean[clean < 0].unique()))
            if "age" in low.split("_") or low in ("age", "âge"):
                out = clean[(clean < 0) | (clean > 120)]
                if len(out):
                    invalid += len(out)
                    issues.append(_issue("high", name, "non-conformité",
                                         f"{len(out)} âge(s) hors de la plage 0–120", len(out), out.unique()))
        elif date_share >= 0.5:
            kind = "date"
            used = [label for label, hit in date_hits.items() if hit.any()]
            if len(used) > 1:
                minority = int(len(values) - max(h.sum() for h in date_hits.values()))
                inconsistent += minority
                issues.append(_issue("medium", name, "formats mélangés",
                                     f"Formats de date multiples : {', '.join(used)}", minority))
            unique = values.unique()[:5000]
            parsed = pd.to_datetime(pd.Series(unique), errors="coerce", dayfirst=True, format="mixed")
            bad = pd.Series(unique)[parsed.isna()]
            if len(bad):
                n_bad = int(values.isin(bad).sum())
                invalid += n_bad
                issues.append(_issue("high", name, "non-conformité",
                                     f"{n_bad} date(s) invalide(s)", n_bad, bad))
        else:
            if "mail" in name.lower():
                kind = "email"
                bad = values[~values.str.match(EMAIL_RE)]
                if len(bad):
                    invalid += len(bad)
                    issues.append(_issue("high", name, "non-conformité",
                                         f"{len(bad)} adresse(s) e-mail invalide(s)", len(bad), bad.unique()))
            # Variantes de casse / accents / espaces d'une même valeur ("Paris", "paris ", "PARIS").
            if values.nunique() <= max(50, len(values) * 0.5):
                folded = values.map(_fold)
                variants = values.groupby(folded).nunique()
                variants = variants[variants > 1]
                if len(variants):
                    cells = int(values[folded.isin(variants.index)].shape[0])
                    inconsistent += cells
                    sample = [", ".join(sorted(values[folded == k].unique())[:3]) for k in variants.index[:3]]
                    issues.append(_issue("medium", name, "incohérence",
                                         f"{len(variants)} valeur(s) écrite(s) de plusieurs façons "
                                         f"(casse, accents, espaces)", cells, sample))
        padded = raw[~missing_mask]
        n_pad = int((padded != padded.str.strip()).sum())
        if n_pad:
            inconsistent += n_pad
            issues.append(_issue("low", name, "espaces superflus",
                                 f"{n_pad} valeur(s) avec espaces en début/fin", n_pad))

    if name.lower() in ("id", "uuid", "identifiant"):
        dup = int(values.duplicated().sum())
        if dup:
            issues.append(_issue("high", name, "clé dupliquée",
                                 f"{dup} identifiant(s) en doublon dans « {name} »", dup,
                                 values[values.duplicated()].unique()))

    detail = {
        "name": name, "type": kind, "missing": missing,
        "missing_pct": round(missing / n_rows * 100, 1) if n_rows else 0,
        "unique": int(values.nunique()),
    }
    return detail, issues, inconsistent, invalid


def _grade(score):
    return "bon" if score >= 85 else "moyen" if score >= 65 else "insuffisant"


def analyze_table(name, df):
    n_rows, n_cols = df.shape
    if n_rows == 0:
        return {"name": name, "rows": 0, "columns": n_cols, "scores": {"overall": 0},
                "grade": "insuffisant", "columns_detail": [],
                "issues": [_issue("high", None, "table vide", "La table ne contient aucune ligne", 0)]}

    columns, issues = [], []
    missing_cells = inconsistent_cells = invalid_cells = 0
    for col in df.columns:
        detail, col_issues, inc, inv = _analyze_column(col, df[col], n_rows)
        columns.append(detail)
        issues += col_issues
        missing_cells += detail["missing"]
        inconsistent_cells += inc
        invalid_cells += inv

    dup_rows = int(df.duplicated().sum())
    if dup_rows:
        issues.append(_issue("high" if dup_rows / n_rows > 0.05 else "medium", None, "doublons",
                             f"{dup_rows} ligne(s) entièrement dupliquée(s)", dup_rows))

    total_cells = n_rows * n_cols
    scores = {
        "completeness": 100 * (1 - missing_cells / total_cells),
        "uniqueness": 100 * (1 - dup_rows / n_rows),
        "consistency": 100 * (1 - min(1, inconsistent_cells / total_cells)),
        "validity": 100 * (1 - min(1, invalid_cells / total_cells)),
    }
    scores["overall"] = sum(scores[k] * w for k, w in WEIGHTS.items())
    scores = {k: round(v, 1) for k, v in scores.items()}

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: (order[i["severity"]], -i["count"]))
    return {
        "name": name, "rows": n_rows, "columns": n_cols, "scores": scores,
        "grade": _grade(scores["overall"]), "duplicate_rows": dup_rows,
        "missing_cells": missing_cells, "columns_detail": columns, "issues": issues,
    }


def analyze_file(filename, data):
    tables = [analyze_table(n, df) for n, df in load_tables(filename, data).items()]
    overall = round(sum(t["scores"]["overall"] for t in tables) / len(tables), 1)
    return {"filename": filename, "overall": overall, "grade": _grade(overall), "tables": tables}
