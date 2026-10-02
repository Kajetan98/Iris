#!/usr/bin/env bash
# Uruchamia pytania z pl_questions.md przez `jarvis ask` (silnik Ollama, D-010) i zapisuje odpowiedzi
# do host/tests/results/.
# Użycie:  ./host/tests/run_pl_questions.sh
# Zmienne: OPENJARVIS_DIR (domyślnie ~/src/OpenJarvis), JARVIS_MODEL (domyślnie qwen3:8b),
#          OLLAMA_HOST (domyślnie http://localhost:11434), DRY_RUN=1 wypisuje tylko wczytane pytania.
# Opcjonalny plik .env w katalogu głównym repozytorium jest wczytywany przed startem.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

if [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env"; set +a
fi
OPENJARVIS_DIR="${OPENJARVIS_DIR:-$HOME/src/OpenJarvis}"
JARVIS_MODEL="${JARVIS_MODEL:-qwen3:8b}"
export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
OUT="$HERE/results/pl_$(date +%Y%m%d_%H%M%S)_${JARVIS_MODEL//[^A-Za-z0-9._-]/_}.md"

mapfile -t QUESTIONS < <(sed -n 's/^| \([0-9]\+\) | \([^|]*\) |.*/\1\t\2/p' "$HERE/pl_questions.md" | sed 's/[[:space:]]*$//')
echo "Wczytano pytań: ${#QUESTIONS[@]}"
if [ "${DRY_RUN:-0}" = "1" ]; then
  printf '%s\n' "${QUESTIONS[@]}"
  exit 0
fi

if ! curl -sf -m 5 "$OLLAMA_HOST/api/tags" >/dev/null; then
  echo "Ollama nie odpowiada pod $OLLAMA_HOST (uruchom: ollama serve)." >&2
  exit 2
fi
if ! curl -sf -m 5 "$OLLAMA_HOST/api/tags" \
    | python3 -c 'import json,sys; sys.exit(sys.argv[1] not in {m["name"] for m in json.load(sys.stdin)["models"]})' "$JARVIS_MODEL"; then
  echo "Brak modelu $JARVIS_MODEL w Ollama (pobierz: ollama pull $JARVIS_MODEL)." >&2
  exit 3
fi

mkdir -p "$HERE/results"
{
  echo "# Wyniki uruchomienia pytań testowych"
  echo
  echo "Data: $(date -Iseconds); silnik: ollama; model: $JARVIS_MODEL; commit OpenJarvis: $(git -C "$OPENJARVIS_DIR" rev-parse --short HEAD)"
} > "$OUT"

FAILED=0
for line in "${QUESTIONS[@]}"; do
  nr="${line%%$'\t'*}"; q="${line#*$'\t'}"
  echo "[$nr] $q"
  set +e
  ans="$(cd "$OPENJARVIS_DIR" && uv run --no-sync jarvis --quiet ask -e ollama -m "$JARVIS_MODEL" --no-stream "$q" 2>&1)"
  rc=$?
  set -e
  [ "$rc" -eq 0 ] || FAILED=$((FAILED + 1))
  { printf '\n## %s. %s\n\nKod wyjścia: %s\n\n```\n%s\n```\n' "$nr" "$q" "$rc" "$ans"; } >> "$OUT"
done
echo "Zapisano: $OUT"
echo "Pytań z kodem wyjścia różnym od 0: $FAILED z ${#QUESTIONS[@]}"
[ "$FAILED" -eq 0 ]
