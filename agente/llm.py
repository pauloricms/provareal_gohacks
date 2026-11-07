"""Cliente do AI proxy (formato OpenAI), centralizado: modelo por papel, matriz de compatibilidade,
saída estruturada por ferramenta forçada + Pydantic, retry, finish_reason e log de latência/tokens.

Matriz medida no proxy em 07/11 (scripts/probe_llm.py):
- tool_choice apontando uma função específica NÃO funciona (gpt pede outro formato; k3.1 recusa com thinking).
  tool_choice="required" funciona em todos -> saída estruturada = enviar SÓ a ferramenta de registro + "required".
- k3.1-pro: só aceita temperature=1 (omitimos) e gasta tokens pensando (limite de saída maior).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ValidationError

load_dotenv()

# papel -> modelo (sobrescrevível por variável de ambiente); nunca 'latest'
MODELOS = {
    "decisao": os.getenv("MODELO_DECISAO", "gpt-6-sol"),   # Chefe, Investigador
    "leve": os.getenv("MODELO_LEVE", "gpt-6-luna"),        # leitura de comunicações (E9), Compras/Tesouraria
    "critico": os.getenv("MODELO_CRITICO", "k3.1-pro"),    # outra família: erra diferente do GPT
}
# Pool por papel (seção 9e): revezamento entre modelos da mesma família de capacidade, para espalhar a carga se o
# proxy limitar por modelo. Todos passaram no teste de ferramenta forçada (scripts/teste_concorrencia.py, 07/11 14:30).
# O Crítico NÃO entra em pool: continua de outra família (k3.1-pro) que a dos investigadores (GPT).
# POOL=0 no ambiente desliga (usa só MODELOS).
POOLS = {
    "decisao": ["gpt-6-sol", "gpt-6-astra", "gpt-5.6-sol"],
    # glm-5.3-flash e mimo-v2.6-flash passaram no teste curto (2 a 6 s), mas com os pedidos reais (5 a 10 mil tokens)
    # levaram 28 a 40 s por chamada na execução final (o gpt-6-luna leva 5 s): saíram do pool às 14:57.
    "leve": ["gpt-6-luna", "gpt-5.6-luna"],
}
USAR_POOL = os.getenv("POOL", "1") != "0"
PARAMS_PAPEL = {
    "decisao": {"temperature": 0.1, "max_tokens": 2500},
    "leve": {"temperature": 0, "max_tokens": 1500},
    "critico": {"temperature": 0, "max_tokens": 4000},
}
SEM_TEMPERATURA = {"k3.1-pro"}          # aprendido no probe; outros modelos aprendem em tempo de execução
MIN_TOKENS = {"k3.1-pro": 4000}         # modelos com thinking precisam de folga


class ErroLLM(Exception):
    pass


class LLM:
    def __init__(self, registrar: Callable[..., Any] | None = None):
        self.cliente = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL"),
                              timeout=60, max_retries=2)
        self.registrar = registrar  # callback de log (eventos.jsonl)
        self.gravar: Callable[[dict], Any] | None = None  # F3d: chamada completa para llm/NNN.jsonl
        self._sem_temp = set(SEM_TEMPERATURA)
        self._lock = threading.Lock()
        self.stats = {"chamadas": 0, "falhas": 0, "segundos": 0.0, "tokens": 0}
        self._giro = {p: 0 for p in POOLS}

    def modelo_para(self, papel: str) -> str:
        """Modelo da vez: revezamento no pool do papel (se ligado), senão o modelo fixo do papel."""
        pool = POOLS.get(papel) if USAR_POOL else None
        if not pool:
            return MODELOS[papel]
        with self._lock:
            m = pool[self._giro[papel] % len(pool)]
            self._giro[papel] += 1
        return m

    # ------------------------------------------------------------------ chamada básica
    def chamar(self, papel: str, mensagens: list[dict], tools: list[dict] | None = None,
               tool_choice: str | None = None, agente: str = "", contexto: str = "",
               modelo: str | None = None) -> Any:
        modelo = modelo or self.modelo_para(papel)
        p = dict(PARAMS_PAPEL[papel])
        p["max_tokens"] = max(p["max_tokens"], MIN_TOKENS.get(modelo, 0))
        for tentativa in range(2):
            kw: dict = {"model": modelo, "messages": mensagens, "max_tokens": p["max_tokens"]}
            if modelo not in self._sem_temp:
                kw["temperature"] = p["temperature"]
            if tools:
                kw["tools"] = tools
                kw["tool_choice"] = tool_choice or "auto"
            t0 = time.time()
            try:
                r = self.cliente.chat.completions.create(**kw)
            except Exception as e:
                msg = str(e)
                if "temperature" in msg and modelo not in self._sem_temp:
                    self._sem_temp.add(modelo)  # aprende e tenta de novo sem temperature
                    continue
                self._log(agente, modelo, papel, time.time() - t0, None, erro=msg[:300], contexto=contexto)
                raise ErroLLM(msg[:300]) from e
            seg = time.time() - t0
            ch = r.choices[0]
            self._log(agente, modelo, papel, seg, r.usage, finish=ch.finish_reason, contexto=contexto)
            if self.gravar:
                try:
                    self.gravar({"ts_real": time.strftime("%Y-%m-%dT%H:%M:%S"), "agente": agente or papel,
                                 "papel": papel, "modelo": modelo, "latencia_s": round(seg, 2),
                                 "tokens": getattr(r.usage, "total_tokens", None) if r.usage else None,
                                 "finish": ch.finish_reason, "contexto": contexto, "mensagens": mensagens,
                                 "resposta": {"content": ch.message.content,
                                              "tool_calls": [{"nome": tc.function.name,
                                                              "argumentos": tc.function.arguments}
                                                             for tc in (ch.message.tool_calls or [])]}})
                except Exception:
                    pass  # gravação nunca derruba a decisão
            if ch.finish_reason == "length" and tentativa == 0:
                p["max_tokens"] *= 2  # resposta cortada: repete uma vez com limite maior
                continue
            return ch.message
        return ch.message

    def _log(self, agente, modelo, papel, seg, usage, **extra):
        tokens = getattr(usage, "total_tokens", None) if usage else None
        with self._lock:
            self.stats["chamadas"] += 1
            self.stats["segundos"] += seg
            self.stats["tokens"] += tokens or 0
            if extra.get("erro"):
                self.stats["falhas"] += 1
        if self.registrar:
            self.registrar(agente or papel, "llm_chamada", modelo=modelo, papel=papel, latencia_s=round(seg, 2),
                           tokens=tokens, **{k: v for k, v in extra.items() if v is not None})

    # ------------------------------------------------------------------ saída estruturada
    def estruturado(self, papel: str, mensagens: list[dict], schema: type[BaseModel], nome_ferramenta: str,
                    descricao: str, agente: str = "", contexto: str = "") -> BaseModel:
        """Força uma ferramenta de registro (única ferramenta + tool_choice='required'), valida com Pydantic e
        reenvia UMA vez com o erro de validação."""
        tool = ferramenta_de_schema(nome_ferramenta, descricao, schema)
        msgs = list(mensagens)
        ultimo_erro = ""
        modelo = self.modelo_para(papel)  # o reenvio com o erro vai ao MESMO modelo
        for _ in range(2):
            m = self.chamar(papel, msgs, tools=[tool], tool_choice="required", agente=agente, contexto=contexto,
                            modelo=modelo)
            bruto = None
            if m.tool_calls:
                bruto = m.tool_calls[0].function.arguments
            elif m.content:
                bruto = extrair_json(m.content)
            try:
                dados = json.loads(bruto) if isinstance(bruto, str) else bruto
                return schema.model_validate(dados)
            except (ValidationError, json.JSONDecodeError, TypeError) as e:
                ultimo_erro = str(e)[:800]
                if self.registrar:
                    self.registrar(agente or papel, "llm_saida_invalida", modelo=modelo, erro=ultimo_erro)
                msgs = msgs + [{"role": "user", "content": f"Sua resposta não passou na validação: {ultimo_erro}. "
                                                           f"Chame {nome_ferramenta} de novo com os campos corretos."}]
        raise ErroLLM(f"saída inválida duas vezes: {ultimo_erro}")


def ferramenta_de_schema(nome: str, descricao: str, schema: type[BaseModel]) -> dict:
    js = schema.model_json_schema()
    return {"type": "function", "function": {"name": nome, "description": descricao, "parameters": js}}


def extrair_json(texto: str) -> str | None:
    """Tolerante a ```json ... ``` e a texto em volta."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", texto, re.S)
    if m:
        return m.group(1)
    m = re.search(r"\{.*\}", texto, re.S)
    return m.group(0) if m else None
