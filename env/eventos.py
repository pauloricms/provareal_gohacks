"""Trilha de auditoria: tudo o que acontece vai para runs/<ts>/eventos.jsonl."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_MAX_TXT = 4000


def _enxugar(v: Any) -> Any:
    s = json.dumps(v, ensure_ascii=False, default=str)
    if len(s) <= _MAX_TXT:
        return v
    return {"_truncado": True, "inicio": s[:_MAX_TXT], "tamanho": len(s)}


class LogEventos:
    def __init__(self, caminho: Path | str):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.ouvintes: list = []  # callbacks (ex.: Streamlit, Telegram)

    def registrar(self, tique: str, ator: str, tipo: str, **dados: Any) -> dict:
        ev = {"ts_real": time.strftime("%Y-%m-%dT%H:%M:%S"), "tique": tique, "ator": ator, "tipo": tipo}
        ev.update({k: _enxugar(v) for k, v in dados.items()})
        linha = json.dumps(ev, ensure_ascii=False, default=str)
        with self._lock:
            with self.caminho.open("a", encoding="utf-8") as f:
                f.write(linha + "\n")
        for cb in self.ouvintes:
            try:
                cb(ev)
            except Exception:  # ouvinte nunca derruba a simulação
                pass
        return ev

    def ler(self) -> list[dict]:
        if not self.caminho.exists():
            return []
        with self.caminho.open(encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
