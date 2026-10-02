#!/usr/bin/env bash
# Uruchamia pytania z pl_questions.md przez `jarvis ask` i zapisuje odpowiedzi do host/tests/results/.
# Użycie:  bash host/tests/run_pl_questions.sh
# Zmienne: OPENJARVIS_DIR (domyślnie ~/src/OpenJarvis), JARVIS_MODEL (domyślnie claude-sonnet-4-6),
#          DRY_RUN=1 wypisuje tylko wczytane pytania.
# Klucz ANTHROPIC_API_KEY jest wczytywany z .env w katalogu głównym repozytorium.
# ANTHROPIC_BASE_URL z otoczenia jest usuwany (w sesjach Claude Code wskazuje proxy sesji, a SDK anthropic
# wysłałby tam klucz); własny adres można ustawić w .env.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OPENJARVIS_DIR="${OPENJARVIS_DIR:-$HOME/src/OpenJarvis}"
JARVIS_MODEL="${JARVIS_MODEL:-claude-sonnet-4-6}"
OUT="$HERE/results/pl_$(date +%Y%m%d_%H%M%S).md"

unset ANTHROPIC_BASE_URL
if [ -f "$ROOT/.env" ]; then
  set -a; . "$ROOT/.env"; set +a
fi

mapfile -t QUESTIONS < <(sed -n 's/^| \([0-9]\+\) | \([^|]*\) |.*/\1\t\2/p' "$HERE/pl_questions.md" | sed 's/[[:space:]]*$//')
echo "Wczytano pytań: ${#QUESTIONS[@]}"
if [ "${DRY_RUN:-0}" = "1" ]; then
  printf '%s\n' "${QUESTIONS[@]}"
  exit 0
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Brak ANTHROPIC_API_KEY (ustaw w .env w katalogu głównym repozytorium)." >&2
  exit 2
fi

mkdir -p "$HERE/results"
{
  echo "# Wyniki uruchomienia pytań testowych"
  echo
  echo "Data: $(date -Iseconds); silnik: cloud; model: $JARVIS_MODEL; commit OpenJarvis: $(git -C "$OPENJARVIS_DIR" rev-parse --short HEAD)"
} > "$OUT"

FAILED=0
for line in "${QUESTIONS[@]}"; do
  nr="${line%%$'\t'*}"; q="${line#*$'\t'}"
  echo "[$nr] $q"
  set +e
  ans="$(cd "$OPENJARVIS_DIR" && uv run --no-sync jarvis ask -e cloud -m "$JARVIS_MODEL" --no-stream "$q" 2>&1)"
  rc=$?
  set -e
  [ "$rc" -eq 0 ] || FAILED=$((FAILED + 1))
  { printf '\n## %s. %s\n\nKod wyjścia: %s\n\n```\n%s\n```\n' "$nr" "$q" "$rc" "$ans"; } >> "$OUT"
done
echo "Zapisano: $OUT"
echo "Pytań z kodem wyjścia różnym od 0: $FAILED z ${#QUESTIONS[@]}"
[ "$FAILED" -eq 0 ]
