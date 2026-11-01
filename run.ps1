$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath "data\processed\question_bank.db")) {
    python scripts\build_question_bank.py
}

python -m uvicorn backend.kaogong_api.main:app --host 127.0.0.1 --port 8000
