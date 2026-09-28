#!/usr/bin/env python3
"""Ollama token-generation-speed exporter (sidecar).

Tails ``journalctl -u ollama -f`` and parses the ``slot print_timing`` lines to
extract live generation speed (``tg``, tokens/s) and prompt-processing progress
(``progress``, ``tokens per second``). Serves both as JSON on GET /speed.

Generation line:
    slot print_timing: id  0 | task 425533 | n_gen =    622, tg = 5.10 t/s, tg_3s = 6.04 t/s
Prompt-processing line:
    slot print_timing: id  0 | task 425533 | prompt processing, n_tokens =  62464, progress = 0.77, t = 976.55 s / 63.96 tokens per second

Produces (idle):
    {"phase": "idle", "tg": 0.0, "tg_3s": 0.0, "n_gen": 0, "prompt_tokens": 0,
     "prompt_progress": 0.0, "prompt_tps": 0.0, "task_id": null, "ts": 0.0}
"""
from __future__ import annotations

import http.server
import json
import re
import subprocess
import threading
import time

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 9090

# Generation: ... | task <id> | n_gen = <n>, tg = <x> t/s, tg_3s = <y> t/s
_GEN = re.compile(
    r"task\s+(\d+).*?n_gen\s*=\s*(\d+),\s*tg\s*=\s*([\d.]+)\s*t/s,\s*tg_3s\s*=\s*([\d.]+)\s*t/s"
)

# Prompt: ... | task <id> | prompt processing, n_tokens = <n>, progress = <p>, t = <s> s / <tps> tokens per second
_PROMPT = re.compile(
    r"task\s+(\d+).*?prompt processing,\s*n_tokens\s*=\s*(\d+),\s*progress\s*=\s*([\d.]+),\s*t\s*=\s*([\d.]+)\s*s\s*/\s*([\d.]+)\s*tokens per second"
)

_state: dict = {
    "phase": "idle",
    "tg": 0.0,
    "tg_3s": 0.0,
    "n_gen": 0,
    "prompt_tokens": 0,
    "prompt_progress": 0.0,
    "prompt_tps": 0.0,
    "task_id": None,
    "ts": 0.0,
}
_lock = threading.Lock()

# Gilt eine letzte print_timing-Zeile als "live"? Nach STALE_AFTER_SECONDS ohne
# neue Ollama-Zeile wird der Zustand als idle ausgeliefert — sonst meldet /speed
# dauerhaft die letzte (veraltete) Phase, obwohl Ollama längst fertig ist.
STALE_AFTER_SECONDS = 30.0


def _snapshot() -> dict:
    """Liefert den aktuellen Zustand; überschrieben zu idle, wenn veraltet."""
    with _lock:
        state = dict(_state)
    if state.get("phase") != "idle" and state.get("ts"):
        age = time.time() - float(state.get("ts"))
        if age > STALE_AFTER_SECONDS:
            return {
                "phase": "idle",
                "tg": 0.0,
                "tg_3s": 0.0,
                "n_gen": state.get("n_gen", 0),
                "prompt_tokens": state.get("prompt_tokens", 0),
                "prompt_progress": 1.0,
                "prompt_tps": 0.0,
                "task_id": state.get("task_id"),
                "ts": state.get("ts"),
            }
    return state


def _tail_journald() -> None:
    proc = subprocess.Popen(
        ["journalctl", "-u", "ollama", "-f", "--no-pager", "-o", "cat"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        for line in proc.stdout:
            m = _PROMPT.search(line)
            if m:
                with _lock:
                    _state.update({
                        "phase": "prompt",
                        "task_id": int(m.group(1)),
                        "prompt_tokens": int(m.group(2)),
                        "prompt_progress": float(m.group(3)),
                        "prompt_tps": float(m.group(5)),
                        "ts": time.time(),
                    })
                continue
            m = _GEN.search(line)
            if m:
                with _lock:
                    _state.update({
                        "phase": "generate",
                        "task_id": int(m.group(1)),
                        "n_gen": int(m.group(2)),
                        "tg": float(m.group(3)),
                        "tg_3s": float(m.group(4)),
                        "ts": time.time(),
                    })
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/speed", "/tokens", "/healthz"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(_snapshot()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    threading.Thread(target=_tail_journald, daemon=True).start()
    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()