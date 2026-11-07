"""Teste de concorrência do proxy (seção 9e, passo 1): onde está a fila? Por modelo ou global/por token?
4 chamadas simultâneas ao MESMO modelo x 4 simultâneas a modelos DIFERENTES, mesmo pedido curto, ferramenta forçada.
Também mede 1 chamada isolada de cada modelo (referência). Não imprime o token."""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
cli = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL"), timeout=90, max_retries=0)

TOOL = {"type": "function", "function": {"name": "registrar", "description": "Registra a resposta.",
        "parameters": {"type": "object", "properties": {"soma": {"type": "integer"}, "ok": {"type": "boolean"}},
                       "required": ["soma", "ok"]}}}
MSG = [{"role": "user", "content": "Quanto é 17 + 25? Responda chamando a ferramenta registrar."}]


def uma(modelo: str, t_base: float) -> dict:
    t0 = time.time()
    try:
        r = cli.chat.completions.create(model=modelo, messages=MSG, tools=[TOOL], tool_choice="required",
                                        max_tokens=300)
        m = r.choices[0].message
        args = m.tool_calls[0].function.arguments if m.tool_calls else None
        ok = bool(args) and json.loads(args).get("soma") == 42
        return {"modelo": modelo, "ini": round(t0 - t_base, 2), "fim": round(time.time() - t_base, 2),
                "seg": round(time.time() - t0, 2), "ferramenta_ok": ok}
    except Exception as e:
        return {"modelo": modelo, "ini": round(t0 - t_base, 2), "seg": round(time.time() - t0, 2),
                "erro": str(e)[:160]}


def rodada(nome: str, modelos: list[str]) -> dict:
    t = time.time()
    with ThreadPoolExecutor(len(modelos)) as ex:
        res = list(ex.map(lambda m: uma(m, t), modelos))
    total = round(time.time() - t, 2)
    print(f"\n== {nome}: total {total}s")
    for r in res:
        print("  ", r)
    return {"rodada": nome, "total": total, "chamadas": res}


if __name__ == "__main__":
    DIF = ["gpt-6-luna", "gpt-5.6-luna", "glm-5.3-flash", "mimo-v2.6-flash"]
    extra = sys.argv[1:]  # opcional: modelos de decisão para validar ferramenta forçada
    saida = []
    ref = []
    t = time.time()
    for m in DIF + extra:
        ref.append(uma(m, t))
    print("\n== referência sequencial (1 por vez):")
    for r in ref:
        print("  ", r)
    saida.append({"rodada": "sequencial", "chamadas": ref})
    saida.append(rodada("4x mesmo modelo (gpt-6-luna)", ["gpt-6-luna"] * 4))
    saida.append(rodada("4 modelos diferentes", DIF))
    if extra:
        saida.append(rodada("decisão diferentes", extra))
    print("\nJSON:", json.dumps(saida, ensure_ascii=False))
