"""Teste rápido de modelos candidatos a Crítico com pedido de TAMANHO REAL (~8 mil tokens) e ferramenta forçada.
Lição do erro 22 (NOTAS_IA): teste com pedido curto não mostra a latência real. Caso inventado, sem dados do evento."""
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
TOOL = {"type": "function", "function": {"name": "registrar_veredito", "description": "Registra o veredito.",
        "parameters": {"type": "object", "properties": {
            "reproduziu": {"type": "boolean"}, "objecoes": {"type": "array", "items": {"type": "string"}},
            "evidencias_proprias": {"type": "array", "items": {"type": "string"}}},
            "required": ["reproduziu", "objecoes", "evidencias_proprias"]}}}
linhas = "\n".join(f"T{i:05d}; FORN-{i % 37:03d}; NF {10000 + i}; parcela 1/1; R$ {1000 + i * 7.31:.2f}; vence 2030-01-{1 + i % 28:02d}"
                   for i in range(400))
MSG = [{"role": "system", "content": "Você é o Crítico de um time de contas a pagar. Refaça a checagem com os dados e "
                                     "diga se reproduz a conclusão. Responda chamando registrar_veredito."},
       {"role": "user", "content": f"Proposta do investigador: o título T00042 duplica T00079 (mesma NF e parcela). "
                                   f"Dados (fictícios):\n{linhas}\nT00079; FORN-005; NF 10042; parcela 1/1; R$ 1307.02"}]


def uma(modelo):
    t0 = time.time()
    try:
        kw = dict(model=modelo, messages=MSG, tools=[TOOL], tool_choice="required", max_tokens=4000)
        r = cli.chat.completions.create(**kw)
        m = r.choices[0].message
        args = m.tool_calls[0].function.arguments if m.tool_calls else (m.content or "")[:200]
        return modelo, round(time.time() - t0, 1), r.usage.total_tokens if r.usage else None, str(args)[:160]
    except Exception as e:
        return modelo, round(time.time() - t0, 1), None, "ERRO " + str(e)[:160]


if __name__ == "__main__":
    modelos = sys.argv[1:] or ["glm-5.3", "mimo-v2.6-pro", "gpt-5.6-terra"]
    with ThreadPoolExecutor(len(modelos)) as ex:
        for r in ex.map(uma, modelos):
            print(json.dumps(r, ensure_ascii=False))
