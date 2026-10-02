#!/usr/bin/env bash
# Tworzy repozytorium IRIS na GitHubie, kamienie milowe M0–M5 i zadania.
# Uruchomić raz, z katalogu głównego iris/. Wymaga GitHub CLI (gh) zalogowanego: gh auth login
# Użycie:  ./scripts/github_setup.sh [nazwa] [private|public]
# Tablica GitHub Projects (opcjonalnie): CREATE_PROJECT=1 ./scripts/github_setup.sh
#   wymaga zakresu: gh auth refresh -s project
set -euo pipefail

REPO_NAME="${1:-iris}"
VISIBILITY="${2:-private}"
CREATE_PROJECT="${CREATE_PROJECT:-0}"

gh auth status >/dev/null

# 1. Repozytorium istnieje (remote origin); tworzenie repozytorium pominięte.

# 2. Etykiety
gh label create firmware   --color 0E8A16 --description "STM32L476"            --force
gh label create host       --color 1D76DB --description "IRIS Core, Python"    --force
gh label create weryfikacja --color FBCA04 --description "Pomiar lub test decyzji" --force
gh label create decyzja    --color D93F0B --description "Kwestia otwarta"      --force

# 3. Kamienie milowe
milestone() {
  gh api "repos/{owner}/{repo}/milestones" -f title="$1" -f description="$2" >/dev/null
}
milestone "M0 Czat tekstowy"     "OpenJarvis lokalnie, silnik Claude, czat po polsku"
milestone "M1 Głos na hoście"     "Mikrofon laptopa, VAD, STT, agent, TTS; pomiar IR-02 i IR-04"
milestone "M2 Bramka i protokół"  "iris-gateway, protokół v1, symulator HERMES"
milestone "M3 Firmware audio"     "SAI + DMA, ADPCM, ESP8266 TCP, odtwarzanie TTS"
milestone "M4 Wybudzanie i tryb lokalny" "DF2301Q, komendy lokalne, heartbeat, mDNS"
milestone "M5 Narzędzia"          "Czujniki, dom, przypomnienia, kalendarz"

# 4. Zadania
ISSUE_URLS=()
issue() {  # tytuł, kamień milowy, etykieta, treść
  local url
  url=$(gh issue create --title "$1" --milestone "$2" --label "$3" --body "$4")
  ISSUE_URLS+=("$url")
  echo "$url"
}

issue "Uruchomić OpenJarvis lokalnie" "M0 Czat tekstowy" host \
  "Klon repozytorium, quickstart, \`jarvis doctor\`. Spec: rozdz. 13."
issue "Skonfigurować silnik Claude w OpenJarvis" "M0 Czat tekstowy" host \
  "engine/cloud.py lub engine/litellm.py; klucz w .env. Ollama jako zapas."
issue "Zbiór 20 pytań testowych po polsku" "M0 Czat tekstowy" weryfikacja \
  "Kryterium ukończenia M0. Pytania ogólne, polecenia, przypomnienia."

issue "Integracja webrtcvad i faster-whisper" "M1 Głos na hoście" host \
  "Mikrofon laptopa jako źródło. Spec: rozdz. 8."
issue "Test odsłuchowy TTS: Piper, Kokoro-PL, usługa chmurowa" "M1 Głos na hoście" weryfikacja \
  "Decyzja D-004. Te same 10 zdań dla każdego silnika."
issue "Zbiór 100 wypowiedzi i skrypt WER" "M1 Głos na hoście" weryfikacja \
  "Wymaganie IR-04 (WER ≤ 15%)."
issue "Pomiar budżetu opóźnienia na hoście" "M1 Głos na hoście" weryfikacja \
  "Wymaganie IR-02, tabela 6.1. Pomiar na laptopie i RPi 5."

issue "iris-gateway: serwer TCP i dekoder ramek" "M2 Bramka i protokół" host \
  "Na bazie host/iris_protocol.py. docs/protocol.md."
issue "Symulator HERMES (WAV -> ramki AUDIO_CHUNK)" "M2 Bramka i protokół" host \
  "Rozmowa przez symulator bez sprzętu."
issue "Uwierzytelnienie HELLO / HELLO_ACK (HMAC)" "M2 Bramka i protokół" host \
  "docs/protocol.md, sekcja Uwierzytelnienie."
issue "Testy: błędne CRC, utrata połączenia, zgubione ramki" "M2 Bramka i protokół" weryfikacja \
  "Kryterium ukończenia M2."

issue "Potwierdzić obsługę audio przez SAI w RM0351" "M3 Firmware audio" weryfikacja \
  "SPI w STM32L476 bez trybu I2S; INMP441 i MAX98357A przez SAI1 (bloki A/B) lub SAI1 + SAI2."
issue "Przydział UART i I2C dla wszystkich modułów" "M3 Firmware audio" firmware \
  "6 UART w L476; DF2301Q przez I2C, debug przez SWO. Spec: rozdz. 7."
issue "Nagrywanie INMP441: SAI + DMA, 16 kHz" "M3 Firmware audio" firmware ""
issue "Koder IMA-ADPCM i ramkowanie na STM32" "M3 Firmware audio" firmware \
  "Decyzja D-005. Niezależne bloki po 20 ms."
issue "ESP8266: tryb transparentny TCP, 921600 bit/s" "M3 Firmware audio" firmware ""
issue "Odtwarzanie TTS przez MAX98357A z buforem 400 ms" "M3 Firmware audio" firmware ""

issue "DF2301Q: słowo budzące i test IR-01" "M4 Wybudzanie i tryb lokalny" weryfikacja \
  "Decyzja D-007. 100 wywołań z 1 m, nagranie 8 h."
issue "Komendy lokalne i dźwięki DFPlayer" "M4 Wybudzanie i tryb lokalny" firmware \
  "Lista ze spec, rozdz. 5.2. IR-03, IR-06."
issue "Heartbeat i przejście w tryb lokalny" "M4 Wybudzanie i tryb lokalny" firmware \
  "IR-05. docs/protocol.md, Liczniki czasu."
issue "Wykrywanie hosta przez mDNS (_iris._tcp.local)" "M4 Wybudzanie i tryb lokalny" host \
  "IR-07."

issue "Narzędzie hermes_device_sensors" "M5 Narzędzia" host \
  "Decyzja D-006, wymaganie IR-09."
issue "Backend domu: Home Assistant czy MQTT" "M5 Narzędzia" decyzja \
  "Kwestia otwarta nr 1."
issue "home_set z potwierdzeniem dla urządzeń o skutkach fizycznych" "M5 Narzędzia" host \
  "IR-08."
issue "Przypomnienia i kalendarz (scheduler, gcalendar)" "M5 Narzędzia" host ""

# 5. Tablica GitHub Projects (opcjonalnie)
if [ "$CREATE_PROJECT" = "1" ]; then
  NUM=$(gh project create --owner "@me" --title "IRIS" --format json --jq .number)
  gh project link "$NUM" --owner "@me" --repo "$REPO_NAME"
  for url in "${ISSUE_URLS[@]}"; do
    gh project item-add "$NUM" --owner "@me" --url "$url" >/dev/null
  done
  echo "Tablica IRIS: projekt nr $NUM"
fi
