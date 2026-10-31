# RobotAI: robot uczący się chodzić

Dwunożny robot w symulatorze PyBullet uczy się chodzić algorytmem PPO (Stable-Baselines3). Opcjonalnie lokalny model Gemma, uruchomiony przez LiteRT-LM, decyduje, kiedy robot ma iść, a kiedy stać. Cały program jest w `main.py`, testy w `test_main.py`.

Ten katalog jest niezależny od IRIS: ma własne zależności i nie wchodzi do testów ani CI hosta.

## Przygotowanie

1. Python 3.10-3.13 i osobne środowisko wirtualne:
   ```
   python -m venv venv
   venv\Scripts\activate            (Windows)
   source venv/bin/activate         (Linux, macOS)
   ```
2. Biblioteki (dokładne komendy są też na początku `main.py`):
   ```
   pip install "gymnasium>=1.0" "numpy>=1.24" pybullet torch "stable-baselines3>=2.3" litert-lm-api
   ```
   Jeśli dla Twojego systemu nie ma gotowego pakietu PyBullet, pip skompiluje go ze źródeł. Na Windows potrzebne są wtedy Microsoft C++ Build Tools.
3. Do trybu z Gemmą skopiuj do tego katalogu plik `gemma-4-E2B-it.litertlm` (ok. 2,6 GB, z `litert-community/gemma-4-E2B-it-litert-lm` na Hugging Face). Repozytorium go ignoruje (`.gitignore`).

## Uruchomienie

Trening ma dwa etapy. Pierwszy uczy samego chodu, drugi doucza go karami za "nieludzki" ruch:

```
python main.py train --reward basic --timesteps 1000000 --out models/etap1
python main.py train --reward humanlike --init models/etap1/final_model.zip --timesteps 3000000 --out models/etap2
python main.py eval --policy models/etap2/final_model.zip      # pokaz w oknie 3D, akcje deterministyczne
python main.py gemma --policy models/etap2/final_model.zip
```

Trening od razu z nagrodą `humanlike` (bez `--init`) kończy się zwykle robotem, który stoi w miejscu (sekcja "Dwa etapy" niżej).

Każdy trening zapisuje w katalogu `--out`: `best_model.zip` (najlepszy wynik w ocenach co `--eval-every` kroków), `final_model.zip` i statystyki normalizacji obserwacji (`vecnormalize_*.pkl`), bez których polityka nie działa poprawnie. Ocena w trakcie treningu to tylko 5 epizodów, więc `best_model.zip` bywa gorszy od `final_model.zip`; warto sprawdzić oba przez `eval --headless --episodes 20`. Ctrl+C przerywa trening i zapisuje stan. Zamknięcie okna PyBullet kończy pokaz bez błędu.

Na 4 rdzeniach procesora z 4 środowiskami trening idzie z prędkością ok. 570 kroków na sekundę, więc 2 mln kroków to ok. godziny. Proces uczący używa jednego wątku PyTorch (`TrainConfig.torch_threads`): przy małych sieciach to szybciej niż kilka wątków, które konkurują o rdzenie z procesami symulacji.

## Jak to działa

### Robot i środowisko

Gymnasium nie ma robota kroczącego na PyBullet: `Ant-v4` działa na MuJoCo, a `BipedalWalker-v3` na Box2D. Dlatego `main.py` rejestruje własne środowisko `HumanlikeWalker-v0` (tworzone przez `gym.make`) z robotem Walker2D z pakietu PyBullet:

- Robot ma tułów i dwie nogi. Każda noga ma biodro, kolano i kostkę, w sumie 6 silników sterowanych momentem do 40 N·m.
- Ruch odbywa się w płaszczyźnie pionowej, więc robot może upaść tylko do przodu albo do tyłu.
- Fizyka liczy 240 kroków na sekundę, a sterowanie działa 60 razy na sekundę.
- Epizod kończy upadek (tułów niżej niż 0,8 m albo przechylony o więcej niż 57°) albo upływ 1000 kroków.

### Nagroda "ludzkiego" chodu

`HumanlikeGaitReward` liczy nagrodę w każdym kroku:

```
r = 1,5·min(v_x, 1,2)  + 1                      postęp do przodu (bez premii za bieg) i przeżycie
  - 2·θ² - 0,1·θ̇²                               tułów pionowo, bez kiwania
  - 0,5·v_z² - 3·(h - 1,2)²                     stała wysokość środka ciężkości, bez podskoków
  - 0,1·średnia(a²) - 0,02·średnia(|a·q̇|)       energia i moc w stawach
  - 0,3·średnia((a_t - a_t-1)²)                 szarpanie silnikami
  - 0,5 gdy żadna stopa nie dotyka ziemi
  - 50 przy upadku (i natychmiastowy koniec epizodu)
```

Kary kwadratowe są małe przy drobnych odchyleniach i duże przy dużych, więc najbardziej opłaca się spokojny ruch blisko równowagi. Wagi są w klasie `RewardWeights` i można je zmieniać.

`v_x` i `v_z` to średnie prędkości w kroku sterowania: przesunięcie tułowia podzielone przez 1/60 s. Pierwsza wersja brała prędkość chwilową z końca kroku. Polityka nauczyła się wtedy drgać w miejscu tak, że w chwili odczytu robot zawsze szedł do przodu. Dostawała nagrodę za postęp, a w 16,7 s przesuwała się o 0,17 m.

### PPO

Ustawienia są w funkcji `ppo_hyperparameters()`, z opisem każdego parametru:
- 8 środowisk po 512 kroków daje 4096 próbek na aktualizację, w paczkach po 128.
- Współczynnik uczenia maleje liniowo od 3·10⁻⁴ do zera.
- `clip_range` wynosi 0,2.
- Eksploracja gSDE daje gładszy szum niż losowanie w każdym kroku.
- Obserwacje i nagrody są normalizowane (`VecNormalize`).

### Gemma jako warstwa decyzyjna

Model językowy odpowiada w czasie rzędu sekund, a robot musi dostawać polecenia 60 razy na sekundę. Gemma nie steruje więc silnikami bezpośrednio, tylko wybiera zachowanie, a momenty liczy kontroler niskiego poziomu:

```
co 0,5 s:  stan robota → state_to_prompt() → Gemma (LiteRT-LM, osobny wątek) → {"command": "walk" | "stand"}
co 1/60 s: komenda → walk: polityka PPO | stand: regulator PD postawy → momenty w 6 stawach → PyBullet
```

- `state_to_prompt()` opisuje słowami wysokość, przechył, prędkość, kontakt stóp i przebyty dystans.
- `GemmaBrain` używa oficjalnego API Pythona (`litert_lm.Engine`, `create_conversation`, `send_message`). Każda decyzja to nowa rozmowa z instrukcją systemową. Odpowiedź jest ograniczona schematem JSON; jeśli model albo biblioteka tego nie obsługuje, odpowiedź jest parsowana z tekstu.
- `parse_command()` zamienia nieczytelną odpowiedź na bezpieczne "stand".
- `AsyncDecider` wysyła zapytanie w osobnym wątku. Symulacja biegnie w czasie rzeczywistym z poprzednią komendą, a nowa obowiązuje od klatki, w której przyszła odpowiedź. Kolejne zapytanie idzie dopiero po poprzedniej odpowiedzi.
- `PostureController` to regulator PD z korektą równowagi w kostkach. Jego wzmocnienia dobrano przeszukaniem w tej symulacji.

## Ograniczenia

- Ścieżki z prawdziwym modelem Gemma nie uruchamiałem, bo plik modelu nie był dostępny. API LiteRT-LM jest sprawdzone na pakiecie `litert-lm-api` 0.19.0, a pętlę decyzji testują atrapy modelu z opóźnieniem. Szybkość odpowiedzi Gemmy na CPU zależy od komputera.
- Regulator postawy utrzymuje robota na stojąco, ale silne pchnięcie zwykle go przewraca. Przejście z chodu do stania w trakcie kroku może skończyć się upadkiem.
- Bez `--policy` komenda "walk" działa jak "stand", bo chód wymaga wytrenowanej polityki.
- Okno 3D wymaga środowiska graficznego. Na serwerze bez ekranu użyj `--headless`.
