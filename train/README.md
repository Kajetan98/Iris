# Model IRIS

Dostrajanie otwartego modelu tak, żeby zachowywał się jak IRIS bez pomocy długiego promptu. Zasady: `docs/spec.md`, sekcja 15. Decyzja: `docs/adr/0002-wlasny-model-iris.md`.

Stan: potok działa i jest testowany (`pytest host/tests/test_train.py`). Sam trening nie został jeszcze wykonany, bo wymaga karty graficznej.

## Kroki

1. Punkt odniesienia. Na komputerze z Ollama i modelem bazowym utwórz `baza.toml`:

   ```toml
   [brain.llm]
   engine = "ollama"
   model = "<model bazowy w Ollama>"
   ```

   i zmierz go: `python -m train.evaluate --config baza.toml --json baza.json` oraz to samo z `--no-tools`.

2. Zbiór danych. Same przykłady wzorcowe (ok. 90, za mało na trening) dostaniesz bez modelu:

   ```
   python -m train.dataset --out train/data/iris.jsonl
   ```

   Do treningu dodaj nauczyciela, czyli większy model lokalny w Ollama opisany w `nauczyciel.toml` (ten sam format co wyżej):

   ```
   python -m train.dataset --out train/data/iris.jsonl --teacher-config nauczyciel.toml --n 300
   ```

   Polecenie wypisuje, ile tur odpadło i przez którą regułę. Przed użyciem modelu z chmury jako nauczyciela sprawdź, czy warunki dostawcy pozwalają trenować na jego odpowiedziach.

3. Trening (karta graficzna):

   ```
   pip install -r train/requirements.txt
   python -m train.finetune --base speakleash/Bielik-4.5B-v3.0-Instruct \
       --data train/data/iris.jsonl --out train/out/iris-lora --merge
   ```

   Jeśli pojawi się ostrzeżenie, że szablon modelu nie obsługuje narzędzi, model nauczy się tylko trybu z pamięcią w bloku kontekstu. Ustaw wtedy w hoście `tools = false` w `[brain.llm]`.

4. Eksport do Ollama. Z repozytorium llama.cpp:

   ```
   python convert_hf_to_gguf.py train/out/iris-lora/merged --outfile train/out/iris.gguf --outtype q8_0
   ```

   Skopiuj `train/Modelfile` obok pliku `iris.gguf`, uzupełnij w nim szablon rozmowy z modelu bazowego (`ollama show --modelfile <model bazowy>`) i zarejestruj model:

   ```
   ollama create iris -f Modelfile
   ```

5. Ocena modelu dostrojonego: `python -m train.evaluate` z `model = "iris"`. Wdrażaj tylko przy wyraźnie lepszym wyniku niż w kroku 1, a potem posłuchaj kilku rozmów: kontrole nie oceniają poprawności merytorycznej ani polszczyzny.

6. Wdrożenie: w `iris.toml` ustaw `[brain.llm]` z `engine = "ollama"` i `model = "iris"`.

## Pliki

- `scenarios.py`: przykłady wzorcowe (`GOLD`), pule dla nauczyciela, tury do oceny (`EVAL`)
- `run.py`: scenariusz na prawdziwej ścieżce hosta, zapis zapytań do silnika
- `checks.py`: reguły wspólne dla filtra danych i oceny
- `dataset.py`, `evaluate.py`, `finetune.py`: polecenia z kroków wyżej
- `Modelfile`: wzór dla Ollama
