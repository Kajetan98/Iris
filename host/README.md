# IRIS Core (host)

Python 3.10+. Składa się z:

- `iris-gateway`: serwer TCP przyjmujący ramki z HERMES, dekodujący audio i przekazujący je do potoku STT -> agent -> TTS,
- konfiguracji OpenJarvis (silnik Claude, Ollama jako zapas),
- własnych narzędzi agenta (`hermes_device_*`, dom, przypomnienia).

Gotowe: `iris_protocol.py` (koder, dekoder strumieniowy z resynchronizacją, test: `python3 iris_protocol.py`).

Pozostały kod powstaje od etapu M0 (zob. [../docs/spec.md](../docs/spec.md)).
