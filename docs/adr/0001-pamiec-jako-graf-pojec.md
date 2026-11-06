# ADR-0001: Pamięć jako graf pojęć, narzędzia LLM, IRIS jako osobny byt

**Status:** zaakceptowana (decyzje Kajetana z 6.11.2026), wdrażana krokami
**Data:** 2026-11-06
**Decydujący:** Kajetan

## Kontekst

Do wersji 3 schematu IRIS pamiętał fakty o użytkowniku w tabeli `facts`:
- zdania podane wprost (`stated`) i przypuszczenia z rozmów (`inferred`) z pewnością;
- wzmacnianie i zanikanie;
- pytania "Czy dobrze rozumiem: ...?" oraz reakcje na pochwałę i krytykę.

Te same reguły ma aplikacja Android w Kotlinie, zgodność pilnują wspólne wektory testowe.

Paczka `iris-memory` (6.11.2026) proponuje inny model: graf pojęć z wykładniczym zanikiem, połączeniami między pojęciami z tej samej wypowiedzi, wizualizacją jako kula i dwoma narzędziami agenta (`iris_recall`, `iris_remember`). Była napisana pod serwer OpenJarvis, którego to repozytorium nie używa.

Przegląd paczki wykazał trzy rzeczy:
- **Liczby i dane wrażliwe.** Heurystyka zapisuje liczby ("tętno 120" daje pojęcie `120`) i dane wrażliwe (choroby, adres). Omija tym filtr danych wrażliwych z obecnych reguł.
- **Brak uwierzytelniania.** Router nie ma uwierzytelniania, a trasy `/clear`, `/forget` i `/learn` są otwarte.
- **Narzędzia.** Host nie ma pętli wywołań narzędzi LLM.

Jednocześnie zmienia się założenie projektu. IRIS ma być osobnym bytem, a HERMES jednym z urządzeń, które mogą się z nim łączyć, a nie ciałem IRIS.

Ograniczenia z `CLAUDE.md`:
- spec przed kodem;
- warstwa stanu działa bez LLM;
- dane o zdrowiu i położeniu nie trafiają do chmury poza potrzebnym wycinkiem;
- treść pamięci nie trafia do zdarzeń ani logów;
- bez nowych zależności bez pytania.

## Decyzja

1. Graf pojęć zastępuje fakty na hoście i w aplikacji Android.
   - Model z paczki ma takie wartości: start 0,35, wzmocnienie 0,4, połączenie 0,3, przypomnienie 0,15, próg zapomnienia 0,03. Okres połowicznego zaniku wynosi 14 dni zamiast 3.
   - Graf trzyma pojęcia, nie liczby. Filtr danych wrażliwych z wersji 3 obejmuje graf: wypowiedź z danymi wrażliwymi nie uczy grafu automatycznie.
   - Fakty z wersji 3 są migrowane do grafu, a tabela `facts` zostaje jako `facts_v3` do usunięcia przez "zapomnij wszystko".
2. Host dostaje pętlę narzędzi LLM w adapterach Claude i Ollama, z narzędziami `iris_recall` i `iris_remember`.
   - Narzędzia ze wspomnieniami są oferowane według tej samej polityki co dawne fakty (`send_memory`: `never`, `local_only`, `always`).
   - Model bez obsługi narzędzi dostaje przypomnienie w bloku kontekstu.
3. Paczka nie jest wgrywana jako osobny pakiet. Jej logika trafia do warstw repozytorium:
   - reguły do `host/core`;
   - tabele do `host/store`;
   - trasy z tokenem do `host/api`;
   - wizualizacja do klienta przeglądarkowego.

   Adapter OpenJarvis nie jest potrzebny.
4. IRIS jest osobnym bytem (spec, sekcja 0).
   - Nazwy protokołu (`body`, "ciało") zostają dla zgodności z firmware, a znaczą "aktywne urządzenie".
   - Zdarzenia z czujników nie zasilają pamięci.

## Rozważane opcje

### A. Graf zastępuje fakty (wybrana)

| Wymiar | Ocena |
|---|---|
| Złożoność | wysoka: spec, rdzeń, baza z migracją, API, przeglądarka, Android z nowymi wektorami |
| Koszt | kilka modułów pracy, jedna pamięć do utrzymania |
| Skala | graf rośnie z liczbą pojęć; zanik i próg zapomnienia go ograniczają, a przy setkach pojęć migawka i przycinanie są tanie |
| Znajomość | model prosty (jedna formuła zaniku), reguły deterministyczne |

**Zalety:**
- jedna pamięć zamiast dwóch;
- skojarzenia między pojęciami;
- wizualizacja;
- nauka z każdej rozmowy bez pytań do użytkownika.

**Wady:**
- graf pamięta pojęcia, nie zdania: "mam psa o imieniu Burek" daje pojęcia `psa`, `imieniu`, `Burek` bez relacji między nimi;
- tracimy pewność i pytania potwierdzające;
- migracja jest stratna (zdanie zamienia się w zbiór pojęć), dlatego stara tabela zostaje.

### B. Graf obok faktów

| Wymiar | Ocena |
|---|---|
| Złożoność | średnia: nowe moduły bez ruszania istniejących reguł i Androida |
| Koszt | dwie pamięci, dwa zestawy poleceń i promptów |
| Skala | jak A |
| Znajomość | istniejące reguły zostają |

**Zalety:**
- zdania i skojarzenia naraz;
- mniejsze ryzyko.

**Wady:**
- dwa źródła prawdy;
- pytanie, które polecenie trafia do której pamięci;
- dłuższy prompt.

### C. Paczka bez zmian, przez OpenJarvis

Odrzucona: repozytorium nie używa OpenJarvis, a paczka nie ma uwierzytelniania ani filtra danych wrażliwych.

## Analiza

Kajetan wybrał A, rezygnując ze zdań na rzecz jednej pamięci i skojarzeń. Ryzyka A ograniczają trzy reguły:
- filtr (liczby, dane wrażliwe);
- migracja nieniszcząca (`facts_v3`);
- polecenie "zapamiętaj", które dalej działa bez LLM.

Narzędzia LLM zamiast stałego wstrzykiwania pamięci do promptu skracają kontekst. Kosztują jednak dodatkowe wywołanie modelu, gdy model użyje narzędzia, co wydłuża odpowiedź głosową. Dlatego pętla ma limit rund, a nauka z rozmowy działa w tle.

## Konsekwencje

Łatwiejsze:
- wizualizacja pamięci;
- nauka bez pytań;
- jedna polityka prywatności dla pamięci.

Trudniejsze:
- odpowiedź na pytanie o konkretny fakt ("jak ma na imię mój pies?"). Graf zwraca pojęcia powiązane, a LLM musi z nich wnioskować;
- port reguł grafu do Kotlina z nowymi wektorami.

Do sprawdzenia później:
- jakość heurystyki (formy odmienione jako etykiety);
- czas odpowiedzi głosowej z narzędziami;
- usunięcie `facts_v3` w kolejnej wersji schematu;
- domeny (`dom`, `czas`, `ty`, `rozmowa`) po pierwszych tygodniach użycia.

## Kroki

1. [ ] Spec (sekcje 0, 1, 4, 7, 9, 10, 12, 14) i `CLAUDE.md`.
2. [ ] `host/core`: reguły grafu, wyciąganie pojęć, filtr, polecenia, konfiguracja `[memory]`.
3. [ ] `host/store`: schemat 4, migracja faktów.
4. [ ] `host/brain`: pętla narzędzi (Claude, Ollama), prompt wyciągania, prompt systemowy.
5. [ ] `host/api`: trasy `/memory`, strumień WebSocket z tokenem, nauka w tle, polecenia, reakcje.
6. [ ] Klient przeglądarkowy: kula pamięci.
7. [ ] Android: port reguł i wektory, migracja w telefonie, ekran pamięci, eksport.
8. [ ] Pomiar czasu od końca wypowiedzi do początku odtwarzania przed zmianą i po niej.
