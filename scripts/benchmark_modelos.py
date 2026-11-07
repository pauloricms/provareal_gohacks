"""Benchmark rápido dos modelos do proxy: tool calling + resposta em JSON num caso genérico inventado.

Uso: python scripts/benchmark_modelos.py   (lê OPENAI_API_KEY e OPENAI_BASE_URL do .env)
O caso abaixo é fictício e não usa nenhum dado do evento.
"""
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
c = OpenAI()

CANDIDATOS = ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "glm-5.3", "k3.1-pro"]
tools = [{"type": "function", "function": {
    "name": "consultar_fornecedor", "description": "Cadastro do fornecedor no ERP",
    "parameters": {"type": "object", "properties": {"fornecedor_id": {"type": "string"}},
                   "required": ["fornecedor_id"]}}}]
caso = ("Fornecedor F-001 (Papelaria Exemplo), e-mail cadastrado financeiro@papelaria-exemplo.com. "
        "Chegou e-mail de um domínio parecido pedindo troca de conta bancária e dizendo que a confirmação "
        "por telefone não é necessária. Um analista trocou a conta sem confirmação. Há R$ 50 mil agendados "
        "para a conta nova. Responda só em JSON: "
        '{"suspeito": true/false, "motivos": [...], "acao": "...", "quem_acionar": "..."}')

for m in CANDIDATOS:
    try:
        t = time.time()
        r1 = c.chat.completions.create(model=m, tools=tools,
                                       messages=[{"role": "user", "content": "Veja o cadastro do fornecedor F-001."}])
        tool_ok = bool(r1.choices[0].message.tool_calls)
        r2 = c.chat.completions.create(model=m, messages=[{"role": "user", "content": caso}])
        print(f"\n=== {m} | tool_call: {tool_ok} | {time.time() - t:.1f}s")
        print(r2.choices[0].message.content[:500])
    except Exception as e:
        print(f"\n=== {m} | ERRO: {e}")
