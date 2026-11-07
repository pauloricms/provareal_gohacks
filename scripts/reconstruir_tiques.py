"""Reconstrói `tiques/<NNN>.json`, `llm/<NNN>.jsonl` e `lotes/<lote>.json` (seção F3d) de uma execução gravada
ANTES da gravação nativa, a partir do `eventos.jsonl` + o estado SQLite final. Não inventa: o que não estava gravado
fica listado em `faltaram` de cada fotografia e em `reconstrucao.json`.

    python scripts/reconstruir_tiques.py runs/<ts>
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agente.gravacao import escrever_atomico, foto_lote, fotografia, ler_eventos, numero_tique  # noqa: E402
from env.estado import Estado  # noqa: E402


def main(run: Path) -> None:
    est = Estado(run / "estado.sqlite")
    eventos = ler_eventos(run)
    horas = sorted({e["agora"] for e in eventos if e["tipo"] == "tique_resumo"})
    llm_por_tique: dict[int, list] = defaultdict(list)
    for e in eventos:
        if e["tipo"] == "llm_chamada":
            llm_por_tique[numero_tique(e["tique"])].append(
                {"ts_real": e.get("ts_real"), "agente": e.get("ator"), "papel": e.get("papel"),
                 "modelo": e.get("modelo"), "latencia_s": e.get("latencia_s"), "tokens": e.get("tokens"),
                 "finish": e.get("finish"), "contexto": e.get("contexto"), "erro": e.get("erro"),
                 "mensagens": None, "resposta": None})
    for h in horas:
        n = numero_tique(h)
        escrever_atomico(run / "tiques" / f"{n:03d}.json",
                         json.dumps(fotografia(est, eventos, h, reconstrucao=True), ensure_ascii=False, default=str))
        escrever_atomico(run / "llm" / f"{n:03d}.jsonl",
                         "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in llm_por_tique.get(n, [])))
    lotes = [l["lote_remessa"] for l in est.q("SELECT lote_remessa FROM lotes WHERE fechado_em IS NOT NULL")]
    for l in lotes:
        escrever_atomico(run / "lotes" / f"{l}.json",
                         json.dumps(foto_lote(est, eventos, l), ensure_ascii=False, default=str))
    info = {"reconstruida": True, "tiques": len(horas), "lotes": lotes,
            "faltaram": fotografia(est, [], horas[0], reconstrucao=True)["faltaram"] if horas else []}
    escrever_atomico(run / "reconstrucao.json", json.dumps(info, ensure_ascii=False, indent=2))
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
