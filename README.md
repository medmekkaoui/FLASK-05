# FlaskGPT

Assistant IA (Flask + Claude) avec conversations SQLite, domaines, exports TXT/JSON/PDF,
résumé PDF, sources citées, notation des réponses, indicateurs de performance et audit
de qualité des données.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\pip install flask python-dotenv anthropic reportlab pandas openpyxl pypdf
npm install
npm run build:css
```

Copiez `.env.example` en `.env` et renseignez `ANTHROPIC_API_KEY` (l'API est payante à l'usage).

## Lancement

```powershell
.\.venv\Scripts\python.exe app.py
```

Puis ouvrez http://127.0.0.1:5000
