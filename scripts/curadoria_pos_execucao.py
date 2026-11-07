"""Curador sobre uma execução JÁ GRAVADA (9d.1, A5), sem refazer a execução.

Copia o estado e a trilha para uma pasta irmã `<run>_curadoria/` (a original fica intacta), roda o Curador
(classifica alertas pelo desfecho dos casos, mede precisão, rebaixa ruidosos, consolida quase iguais, autonomia
conquistada e, com LLM, lições do dia) e grava o antes/depois como evento `curadoria_pos_execucao`.

    python scripts/curadoria_pos_execucao.py runs/<ts> [--sem-llm]
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agente import quadro  # noqa: E402
from agente.papeis import curador  # noqa: E402
from env.ambiente import Ambiente  # noqa: E402


def _detectores(amb) -> list[dict]:
    return [dict(r) for r in amb.est.q("SELECT id, nome, n_alertas, n_confirmados, n_falsos, ativo, motivo_status "
                                       "FROM detectores ORDER BY id")]


def main(run: Path, com_llm: bool) -> dict:
    destino = run.parent / f"{run.name}_curadoria"
    if destino.exists():
        shutil.rmtree(destino)
    destino.mkdir(parents=True)
    src = sqlite3.connect(str(run / "estado.sqlite"))
    dst = sqlite3.connect(str(destino / "estado.sqlite"))
    src.backup(dst)  # cópia consistente mesmo com WAL
    src.close()
    dst.close()
    shutil.copy(run / "eventos.jsonl", destino / "eventos.jsonl")

    amb = Ambiente(destino)
    quadro.iniciar(amb.est)
    antes = _detectores(amb)
    if com_llm:
        from agente.llm import LLM
        llm = LLM(registrar=lambda ag, tipo, **kw: amb.log.registrar(amb.est.agora_txt, ag, tipo, **kw))
        out = curador.curar(llm, amb)
    else:
        out = {"detectores": curador.manter_detectores(amb) + curador.consolidar_detectores(amb),
               "autonomia": curador.autonomia_conquistada(amb)}
    depois = _detectores(amb)
    resumo = {
        "origem": str(run.name), "detectores_antes": len(antes),
        "ativos_antes": sum(1 for d in antes if d["ativo"]), "ativos_depois": sum(1 for d in depois if d["ativo"]),
        "consolidados": [m for m in out["detectores"] if "em" in m],
        "rebaixados": [m for m in out["detectores"] if "em" not in m],
        "autonomia": out.get("autonomia", []), "licoes": out.get("licoes", 0), "propostas": out.get("propostas", 0),
        "detectores": depois,
    }
    amb.log.registrar(amb.est.agora_txt, "curador", "curadoria_pos_execucao", **resumo)
    (destino / "curadoria.json").write_text(json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in resumo.items() if k != "detectores"}, ensure_ascii=False, indent=2))
    return resumo


if __name__ == "__main__":
    main(Path(sys.argv[1]), com_llm="--sem-llm" not in sys.argv)
