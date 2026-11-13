# ADR-0002: Własny model IRIS przez dostrojenie modelu otwartego

**Status:** zaakceptowana (decyzja Kajetana z 10.11.2026), potok gotowy, trening jeszcze niewykonany
**Data:** 2026-11-13
**Decydujący:** Kajetan

## Kontekst

IRIS do rozmowy używa cudzego modelu przez adapter (`host/brain`): Ollama lokalnie albo Claude API. Zachowanie IRIS (odpowiedzi do czytania na głos, brak twierdzeń o odczuwaniu, stan opisany jako stan urządzenia, narzędzia pamięci `iris_recall` i `iris_remember`, pojęcia w bloku kontekstu) wynika wyłącznie z promptu systemowego. Małe modele lokalne trzymają się go słabo, a duże wymagają chmury albo mocnego sprzętu.

Kajetan chce, żeby IRIS była własnym modelem językowym, a nie tylko nakładką na cudzy.

## Decyzja

Model IRIS powstaje przez dostrojenie (LoRA) otwartego modelu bazowego na rozmowach w formacie hosta. Trening od zera nie wchodzi w grę (dane i sprzęt). Host się nie zmienia: model IRIS jest zwykłym modelem w Ollama (`engine = "ollama"`, `model = "iris"`), a reguły stanu, potrzeb i pamięci zostają deterministyczne w Pythonie.

1. Dane powstają prawdziwą ścieżką hosta. Scenariusz przechodzi przez `Assistant` i pamięć, a silnik pośredni zapisuje każde zapytanie w postaci, w jakiej model dostanie je po wdrożeniu (prompt systemowy, blok kontekstu, definicje i wyniki narzędzi). Zbiór nie może więc rozjechać się z hostem.
2. Odpowiedzi pochodzą z dwóch źródeł: przykłady wzorcowe napisane ręcznie i odpowiedzi modelu-nauczyciela na scenariusze losowane, odsiane kontrolami.
3. Kontrole są deterministyczne i wspólne dla filtra danych i oceny.
4. Model dostrojony zastępuje bazowy tylko wtedy, gdy na turach spoza zbioru treningowego ma wyraźnie lepszy wynik kontroli.
5. Dane są wyłącznie syntetyczne. Rozmowy i pamięć użytkownika nie trafiają do zbioru.

## Rozważane opcje

### A: dostrojenie LoRA modelu otwartego (wybrana)

| Wymiar | Ocena |
|---|---|
| Złożoność | średnia: potok danych, trening, eksport do GGUF |
| Koszt | karta graficzna na kilka godzin; bez opłat za API przy lokalnym nauczycielu |
| Zmiany w hoście | brak |

Za: model lokalny, który zna format hosta bez długiego promptu; działa bez chmury. Przeciw: jakość zależy od modelu bazowego i od danych; każda zmiana promptu albo narzędzi wymaga nowego zbioru i treningu.

### B: trening własnego modelu od zera

Odrzucona. Model mówiący po polsku wymaga miliardów słów i sprzętu spoza zasięgu projektu.

### C: tylko prompt systemowy (stan obecny)

Zostaje jako punkt odniesienia w ocenie. Jeśli dostrojenie nie da wyraźnej poprawy, model bazowy z promptem jest lepszym wyborem.

## Model bazowy

Parametr treningu, nie stała. Kandydaci to polskie modele Bielik (SpeakLeash) w wersji 3.0 Instruct: 4.5B (karta modelu podaje licencję Apache 2.0) i 1.5B dla słabszego sprzętu (licencję trzeba sprawdzić na karcie przed dystrybucją). Nie potwierdziłem, że szablony rozmowy 1.5B i 4.5B obsługują narzędzia. Dlatego zbiór ma każdą turę w dwóch trybach (narzędzia pamięci albo pojęcia w bloku kontekstu), a trening sam pomija tryb narzędzi, gdy szablon ich nie wstawia. Host obsługuje oba tryby od ADR-0001.

## Nauczyciel

Dowolny silnik z `[brain.llm]`. Domyślnie większy model lokalny w Ollama. Warunki dostawców API w chmurze mogą zabraniać trenowania innych modeli na ich odpowiedziach: przed użyciem takiego nauczyciela trzeba przeczytać warunki. Odpowiedzi nauczyciela są w zbiorze tylko wtedy, gdy przeszły kontrole.

## Konsekwencje

- Łatwiej: IRIS działa lokalnie z modelem, który zna jej zasady; ocena daje liczbę do porównania modeli i promptów.
- Trudniej: prompt systemowy, narzędzia i blok kontekstu stają się częścią danych. Po ich zmianie zbiór trzeba zbudować od nowa, a model dostroić ponownie.
- Ograniczenie: kontrole wykrywają naruszenia zasad, nie oceniają poprawności merytorycznej ani jakości polszczyzny. Tę część trzeba sprawdzić słuchając modelu.
- Ograniczenie: szablon rozmowy w Ollama musi być ten sam co w treningu (`train/Modelfile`).
- Aplikacja Android: model IRIS w formacie `.litertlm` to osobny krok (konwersja z modelu scalonego), poza tym ADR.

## Kroki

1. [x] Spec, sekcja 15.
2. [x] `train/`: scenariusze, nagrywanie, kontrole, zbiór wzorcowy, ocena, skrypt dostrajania, Modelfile, testy.
3. [ ] Ocena modelu bazowego (`python -m train.evaluate`) na komputerze z Ollama.
4. [ ] Zbiór z nauczycielem, trening na karcie graficznej, eksport do GGUF.
5. [ ] Ocena modelu dostrojonego i decyzja o wdrożeniu.
