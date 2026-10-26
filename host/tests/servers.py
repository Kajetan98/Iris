"""Uruchamianie prawdziwych serwerów uvicorn w wątkach na potrzeby testów całego stosu."""

import asyncio
import inspect
import socket
import threading

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            return server, thread
        threading.Event().wait(0.05)
    raise AssertionError("serwer nie wystartował")


def stop(server, thread):
    server.should_exit = True
    thread.join(5)


async def until(fn, timeout=6.0):
    """Czeka, aż fn() (zwykła lub async) zwróci prawdę."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        value = fn()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("warunek nie został spełniony w czasie")


def ollama_stub_app(content="**Bateria** jest niska.\n"):
    """Atrapa API Ollamy (/api/chat). state["fail"] włącza błąd 503, state["requests"] zbiera zapytania."""
    app = FastAPI()
    state = {"fail": False, "requests": []}

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        state["requests"].append(body)
        if state["fail"]:
            return JSONResponse({"error": "model się ładuje"}, status_code=503)
        return JSONResponse(
            {
                "model": body["model"],
                "message": {"role": "assistant", "content": content},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 120,
                "eval_count": 6,
            }
        )

    return app, state


def anthropic_message(text="Dobrze.", *, stop_reason="end_turn", model="claude-opus-5-5", content=None,
                      usage=None, stop_details=None):
    """Treść odpowiedzi POST /v1/messages."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": usage or {"input_tokens": 120, "output_tokens": 9},
    }


def anthropic_error(error_type, message="błąd"):
    return {"type": "error", "error": {"type": error_type, "message": message}}


def anthropic_stub_app():
    """Atrapa POST /v1/messages (zwykłego i beta). state["script"]: lista (status, treść, nagłówki),
    ostatni element powtarza się w nieskończoność; state["requests"] zbiera żądania SDK."""
    app = FastAPI()
    state = {"script": [(200, anthropic_message(), {})], "requests": [], "delay": 0.0}

    @app.post("/v1/messages")
    async def messages(request: Request):
        body = await request.json()
        state["requests"].append(
            {"path": request.url.path, "query": str(request.url.query),
             "headers": {k.lower(): v for k, v in request.headers.items()}, "body": body}
        )
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        script = state["script"]
        status, content, headers = script.pop(0) if len(script) > 1 else script[0]
        # retry-after-ms skraca odstęp między ponowieniami SDK w testach
        return JSONResponse(content, status_code=status, headers={"retry-after-ms": "10", **headers})

    return app, state
