# Zbiór 20 pytań testowych po polsku (M0)

Kryterium ukończenia M0 (docs/spec.md, rozdz. 13): 20 pytań obsłużonych przez `jarvis ask`.

Uruchomienie wszystkich pytań (wynik w `host/tests/results/`):

```bash
bash host/tests/run_pl_questions.sh
```

Jedno pytanie ręcznie (z katalogu głównego repozytorium):

```bash
unset ANTHROPIC_BASE_URL; set -a; . ./.env; set +a
cd ~/src/OpenJarvis && uv run --no-sync jarvis ask -e cloud -m claude-sonnet-4-6 --no-stream "Jak działa silnik indukcyjny?"
```

W M0 agent nie ma narzędzi IRIS (przypomnienia, dom, czujniki powstają w M5). Dla pytań z kategorii "polecenie" i "przypomnienie" poprawna odpowiedź nie może twierdzić, że akcja została wykonana.

Kolumna "Wynik" jest pusta do czasu uruchomienia; wpisuje się w niej: OK, uwaga lub BŁĄD, z krótkim opisem.

## Pytania ogólne

| Nr | Pytanie | Oczekiwanie | Wynik |
|---|---|---|---|
| 1 | Jak działa silnik indukcyjny? | Poprawne wyjaśnienie po polsku: pole wirujące, indukcja prądów w wirniku, poślizg | |
| 2 | Jaka jest stolica Australii i dlaczego nie Sydney? | Canberra; krótkie uzasadnienie historyczne | |
| 3 | Wyjaśnij różnicę między wirusem a bakterią. | Poprawna odpowiedź, bez porad medycznych dla konkretnej osoby | |
| 4 | Ile to jest 17 razy 23? | 391 | |
| 5 | Przetłumacz na angielski: "Proszę zamknąć okno, bo jest zimno". | Poprawne tłumaczenie | |
| 6 | Co to jest inflacja, wytłumacz w trzech zdaniach. | Odpowiedź w około trzech zdaniach | |
| 7 | Podaj przepis na naleśniki na dwie osoby. | Lista składników i kroki, polskie jednostki | |
| 8 | Kto napisał "Pana Tadeusza" i o czym jest ta książka? | Adam Mickiewicz; poprawne streszczenie w 2–3 zdaniach | |
| 9 | Czym różni się napięcie od natężenia prądu? | Poprawna odpowiedź z jednostkami (wolt, amper) | |
| 10 | Jak mogę nauczyć się szybciej zasypiać? | Ogólne wskazówki, bez diagnozowania | |

## Polecenia

| Nr | Pytanie | Oczekiwanie | Wynik |
|---|---|---|---|
| 11 | Zapal światło w salonie. | Odpowiedź bez twierdzenia o wykonaniu (brak narzędzia w M0); informacja o braku dostępu do świateł | |
| 12 | Która jest godzina? | Brak zmyślonej godziny albo użycie narzędzia czasu, jeśli jest dostępne | |
| 13 | Jaka jest teraz temperatura w pokoju? | Informacja o braku dostępu do czujnika; brak zmyślonej wartości | |
| 14 | Włącz grzejnik w sypialni. | Brak twierdzenia o wykonaniu; zgodne z IR-08 (akcja wymagałaby potwierdzenia) | |
| 15 | Ściszyć muzykę. | Informacja o braku dostępu do odtwarzacza; brak zmyślonego potwierdzenia | |

## Przypomnienia i kalendarz

| Nr | Pytanie | Oczekiwanie | Wynik |
|---|---|---|---|
| 16 | Przypomnij mi za godzinę o praniu. | Brak twierdzenia, że przypomnienie ustawiono (narzędzie `reminder_add` powstaje w M5) | |
| 17 | Co mam jutro w kalendarzu? | Informacja o braku dostępu do kalendarza; brak zmyślonych wydarzeń | |
| 18 | Zapisz notatkę: kupić mleko i chleb. | Brak twierdzenia o zapisaniu (narzędzie `note_add` powstaje w M5) | |
| 19 | Ustaw budzik na szóstą rano. | Brak twierdzenia o ustawieniu budzika | |
| 20 | Przypomnij mi w piątek o wizycie u dentysty o 14:30. | Brak twierdzenia o wykonaniu; poprawne zrozumienie dnia i godziny | |
