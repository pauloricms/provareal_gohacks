"""Guarda de política: toda ação passa por aqui ANTES de executar.

Liberdade para pensar (leituras livres), trava para agir. Regras tiradas de politicas.csv:
- papel permitido por ferramenta (ferramentas.csv);
- alçadas: analista até R$ 5 mil; aprovador até R$ 50 mil; diretora (PS03) acima;
- fracionamento: a alçada vale pela soma do mesmo fornecedor + mesma NF no mesmo dia;
- aprovação dupla: alteração bancária, sem NF, sem OC aprovada, antecipação, troca bancária
  recente (< 30 dias) sem confirmação registrada;
- segregação: quem solicita não aprova; o agente nunca aprova nada;
- contato com fornecedor só pelo canal do cadastro;
- limites diários (contatar_fornecedor 60/dia, acionar_pessoa 40/dia).
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from env import db

ATOR_AGENTE = "agente"
PAPEIS_AGENTE = {"analista_cp", "auditor"}
CAMPOS_BANCARIOS = {"banco", "agencia", "conta", "chave_pix"}


def brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@dataclass
class Veredito:
    ok: bool
    codigo: str = "ok"
    mensagem: str = ""
    requer: list[str] = field(default_factory=list)
    motivos: list[str] = field(default_factory=list)

    def como_erro(self) -> dict:
        d = {"ok": False, "erro": self.codigo, "mensagem": self.mensagem}
        if self.requer:
            d["requer_aprovacao_de"] = self.requer
            d["motivos"] = self.motivos
            d["dica"] = "use solicitar_aprovacao com esta ação, os parâmetros e as evidências"
        return d


def carregar_atores() -> dict[str, dict]:
    """Agente + usuários humanos (usuarios_sistema.csv) com seus papéis na simulação."""
    con = db.conexao_leitura()
    try:
        us = con.execute("SELECT login, nome, perfil, alcada_rs, pessoa_id, status FROM usuarios_sistema "
                         "WHERE sistema = 'ERP'").df().to_dict("records")
    finally:
        con.close()
    atores = {ATOR_AGENTE: {"login": ATOR_AGENTE, "nome": "Agente de contas a pagar", "papeis": set(PAPEIS_AGENTE),
                            "humano": False, "alcada": 5000.0, "pessoa_id": None, "ativo": True}}
    mapa = {"analista_cp": {"analista_cp"}, "aprovador": {"aprovador"}, "diretoria": {"aprovador"},
            "tesouraria": {"aprovador"}, "auditor": {"auditor"}}
    for u in us:
        u = {k: (None if isinstance(v, float) and v != v else v) for k, v in u.items()}  # NaN do pandas -> None
        alc = u["alcada_rs"]
        atores[u["login"]] = {
            "login": u["login"], "nome": u["nome"], "papeis": mapa.get(u["perfil"], set()), "humano": True,
            "perfil": u["perfil"], "alcada": float("inf") if alc == "sem limite" else float(alc or 0),
            "pessoa_id": u["pessoa_id"], "ativo": u["status"] == "ativo",
        }
    return atores


class Guarda:
    def __init__(self, amb):
        self.amb = amb
        self.pol = amb.politicas
        alc = self.pol["alcadas"]
        self.lim_analista = float(alc["analista_cp"]["liberar_ou_agendar_ate"])
        self.lim_cancelar = float(alc["analista_cp"]["cancelar_ate"])
        self.lim_corrigir = float(alc["analista_cp"]["corrigir_diferenca_de_valor_ate"])
        self.lim_aprovador = float(alc["aprovador"]["aprova_ate"])
        self.pessoa_diretora = alc["diretora_financeira"]["pessoa_id"]
        self.pessoas = {p["pessoa_id"]: p for p in self.pol["acionamento_humano"]["pessoas"]}
        # coordenadora da aprovação dupla: a primeira pessoa citada em alcadas.aprovacao_dupla.quem (da política)
        quem = str(alc["aprovacao_dupla"]["quem"])
        citadas = [m for m in re.findall(r"\b([A-Z]{2}\d+)\b", quem) if m in self.pessoas]
        self.pessoa_coord = next((p for p in citadas if p != self.pessoa_diretora), None)
        self.dispensa_oc = {self.pol["tres_vias"]["dispensas"]["oc"]}
        self.dispensa_nf = {self.pol["tres_vias"]["dispensas"]["nf"]}

    # ------------------------------------------------------------------ requisitos de aprovação
    def _por_valor(self, valor: float, limite_proprio: float) -> tuple[list[str], list[str]]:
        if valor <= limite_proprio:
            return [], []
        if valor <= self.lim_aprovador:
            return ["aprovador"], [f"{brl(valor)} acima da alçada do analista ({brl(limite_proprio)})"]
        return [self.pessoa_diretora], [f"{brl(valor)} acima de {brl(self.lim_aprovador)}: diretora financeira"]

    def _dupla(self, valor: float) -> list[str]:
        return ["aprovador", self.pessoa_diretora if valor > self.lim_aprovador else self.pessoa_coord]

    def pessoa_para(self, termo: str) -> str | None:
        """Pessoa da política cujo 'acionar_quando' cobre o assunto (sem ID fixo no código)."""
        t = unicodedata.normalize("NFKD", termo).encode("ascii", "ignore").decode().lower()
        for pid, p in self.pessoas.items():
            q = unicodedata.normalize("NFKD", str(p.get("acionar_quando", ""))).encode("ascii", "ignore").decode().lower()
            if t in q:
                return pid
        return None

    def pessoa_do_requisito(self, requisito: str) -> str:
        """'aprovador' (papel) é levado à coordenadora, que também é aprovadora; demais requisitos já são pessoas."""
        return self.pessoa_coord if requisito in ("aprovador", self.pessoa_coord) else requisito

    def troca_bancaria_recente_sem_confirmacao(self, fornecedor_id: str) -> list[dict]:
        limite = (self.amb.est.agora - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        return [a for a in self.amb.alteracoes_cadastro(fornecedor_id)
                if a["campo"] in CAMPOS_BANCARIOS and str(a["data_hora"]) >= limite
                and (a.get("confirmacao_registrada") or "").lower() != "sim" and not a.get("revertida_por")]

    def gatilhos_dupla_pagamento(self, titulo: dict, lote: dict | None) -> list[str]:
        """Condições da política que exigem aprovação dupla para PAGAR o título."""
        motivos = []
        f = self.amb.est.fornecedor(titulo["fornecedor_id"]) or {}
        cat = f.get("categoria")
        if not titulo.get("numero_nf_digitado") and cat not in self.dispensa_nf:
            motivos.append("pagamento sem NF")
        if cat not in self.dispensa_oc:
            oc = self.amb.status_oc(titulo.get("oc_id"))
            if oc != "aprovada":
                motivos.append(f"pagamento sem OC aprovada (OC {titulo.get('oc_id') or 'vazia'}: {oc or 'inexistente'})")
        if lote and titulo.get("vencimento") and lote["data_pagamento"] < str(titulo["vencimento"]):
            motivos.append(f"antecipação (lote {lote['data_pagamento']} antes do vencimento {titulo['vencimento']})")
        if titulo.get("forma_pagamento") in ("ted", "pix") and self.troca_bancaria_recente_sem_confirmacao(titulo["fornecedor_id"]):
            motivos.append("fornecedor com troca bancária há menos de 30 dias sem confirmação registrada")
        return motivos

    def valor_fracionado(self, ator: str, titulo: dict, para: tuple[str, ...]) -> float:
        """Soma do mesmo fornecedor + mesma NF que o ator já moveu hoje para 'para' + este título."""
        if not titulo.get("nf_norm"):
            return float(titulo["valor"] or 0)
        hoje = self.amb.est.agora.strftime("%Y-%m-%d")
        rows = self.amb.est.q(
            "SELECT DISTINCT t.titulo_id, t.valor FROM transicoes x JOIN titulos t USING (titulo_id) "
            "WHERE x.ator = ? AND substr(x.quando, 1, 10) = ? AND x.para IN (%s) "
            "AND t.fornecedor_id = ? AND t.nf_norm = ? AND t.titulo_id <> ?" % ",".join("?" * len(para)),
            [ator, hoje, *para, titulo["fornecedor_id"], titulo["nf_norm"], titulo["titulo_id"]])
        return float(titulo["valor"] or 0) + sum(r["valor"] or 0 for r in rows)

    def requisitos(self, ator: str, acao: str, args: dict) -> tuple[list[str], list[str]]:
        """(aprovações exigidas, motivos) para executar 'acao' com 'args'. Vazio = pode sozinho."""
        est = self.amb.est
        req, mot = [], []
        if acao == "agendar_titulo":
            t = est.titulo(args.get("titulo_id", ""))
            if not t:
                return [], []
            lote = est.lote(args.get("lote_remessa", ""))
            v = self.valor_fracionado(ator, t, ("agendado", "reagendado"))
            r, m = self._por_valor(v, self.lim_analista)
            req += r; mot += m
            dupla = self.gatilhos_dupla_pagamento(t, lote)
            if dupla:
                req += self._dupla(v); mot += dupla
        elif acao == "cancelar_titulo":
            t = est.titulo(args.get("titulo_id", ""))
            if t:
                r, m = self._por_valor(self.valor_fracionado(ator, t, ("cancelado",)), self.lim_cancelar)
                req += r; mot += m
        elif acao == "corrigir_lancamento":
            t = est.titulo(args.get("titulo_id", ""))
            campo = args.get("campo")
            if campo in ("fornecedor_id", "forma_pagamento", "linha_digitavel"):
                req.append("aprovador"); mot.append(f"trocar {campo} exige aprovador")
            elif campo == "valor" and t:
                try:
                    dif = abs(float(args.get("valor_novo")) - float(t["valor"]))
                except (TypeError, ValueError):
                    dif = 0.0
                if dif > self.lim_corrigir:
                    req.append("aprovador" if dif <= self.lim_aprovador else self.pessoa_diretora)
                    mot.append(f"mudança de valor de {brl(dif)} acima de {brl(self.lim_corrigir)}")
        elif acao in ("reverter_alteracao_cadastro", "aprovar_alteracao_bancaria"):
            req += self._dupla(0); mot.append("alteração de dados bancários exige aprovação dupla")
        # propor_correcao_processo não é barrada: a proposta nasce 'aguardando aprovador'
        # remove duplicatas preservando ordem
        vistos, req_u = set(), []
        for r in req:
            if r not in vistos:
                vistos.add(r); req_u.append(r)
        return req_u, mot

    # ------------------------------------------------------------------ checagem principal
    def checar(self, ator: str, ferramenta: str, args: dict, autorizacao: str | None = None) -> Veredito:
        amb = self.amb
        meta = amb.catalogo.get(ferramenta)
        if meta is None:
            return Veredito(False, "ferramenta_desconhecida", f"ferramenta {ferramenta} não existe")
        a = amb.atores.get(ator)
        if a is None or not a["ativo"]:
            return Veredito(False, "ator_invalido", f"ator {ator} inexistente ou inativo")
        papeis_ok = set(meta["papeis"])
        if papeis_ok and not (a["papeis"] & papeis_ok):
            return Veredito(False, "papel_negado",
                            f"{ator} ({', '.join(sorted(a['papeis'])) or 'sem papel'}) não pode usar {ferramenta} "
                            f"(permitido: {', '.join(sorted(papeis_ok))})")
        faltando = [p for p in meta["obrigatorios"] if args.get(p) in (None, "", [])]
        if faltando:
            return Veredito(False, "parametro_obrigatorio", f"faltam parâmetros: {', '.join(faltando)}")

        lim = meta.get("limites") or {}
        if "por_dia" in lim:
            usados = amb.uso_hoje(ator, ferramenta)
            if usados >= int(lim["por_dia"]):
                return Veredito(False, "limite_diario", f"{ferramenta}: limite de {lim['por_dia']}/dia atingido")

        especifico = getattr(self, f"_checar_{ferramenta}", None)
        if especifico:
            v = especifico(ator, args)
            if v is not None and not v.ok:
                return v

        if meta["tipo"] in ("execucao", "investigacao") and autorizacao is None:
            req, mot = self.requisitos(ator, ferramenta, args)
            if req:
                return Veredito(False, "requer_aprovacao", f"{ferramenta} precisa de aprovação: {'; '.join(mot)}",
                                requer=req, motivos=mot)
        if autorizacao is not None:
            sol = amb.est.q1("SELECT * FROM solicitacoes WHERE id = ?", [autorizacao])
            if not sol or sol["status"] != "aprovada" or sol["acao"] != ferramenta:
                return Veredito(False, "autorizacao_invalida", f"solicitação {autorizacao} não autoriza {ferramenta}")
            if json.loads(sol["parametros"]) != args:
                return Veredito(False, "autorizacao_invalida", "parâmetros diferentes dos aprovados")
        return Veredito(True)

    # ------------------------------------------------------------------ checagens por ferramenta
    def _lote_aberto(self, lote_remessa: str) -> Veredito | None:
        ok, msg = self.amb.relogio.lote_aberto_para_alteracao(lote_remessa)
        return None if ok else Veredito(False, "lote_fechado", msg)

    def _titulo_pendente(self, titulo_id: str) -> Veredito | None:
        t = self.amb.est.titulo(titulo_id)
        if t is None:
            return Veredito(False, "titulo_nao_pendente",
                            f"{titulo_id} não está entre os títulos pendentes (já pago, cancelado ou inexistente)")
        return None

    def _checar_agendar_titulo(self, ator, args):
        v = self._titulo_pendente(args["titulo_id"])
        if v:
            return v
        t = self.amb.est.titulo(args["titulo_id"])
        if t["bloqueado"]:
            return Veredito(False, "titulo_bloqueado", "título bloqueado: só o aprovador desbloqueia")
        f = self.amb.est.fornecedor(t["fornecedor_id"])
        if f and f["bloqueado"]:
            return Veredito(False, "fornecedor_bloqueado", "fornecedor bloqueado: só o aprovador desbloqueia")
        if t["estado"] in ("cancelado", "enviado", "baixado"):
            return Veredito(False, "estado_invalido", f"título em estado {t['estado']}")
        return self._lote_aberto(args["lote_remessa"])

    def _checar_retirar_do_lote(self, ator, args):
        v = self._titulo_pendente(args["titulo_id"])
        if v:
            return v
        t = self.amb.est.titulo(args["titulo_id"])
        if not t["lote_remessa"]:
            return Veredito(False, "fora_de_lote", "título não está em nenhum lote")
        return self._lote_aberto(t["lote_remessa"])

    def _checar_cancelar_titulo(self, ator, args):
        return self._titulo_pendente(args["titulo_id"])

    def _checar_corrigir_lancamento(self, ator, args):
        v = self._titulo_pendente(args["titulo_id"])
        if v:
            return v
        permitidos = {"valor", "numero_nf_digitado", "numero_nf", "vencimento", "forma_pagamento",
                      "linha_digitavel", "fornecedor_id"}
        if args["campo"] not in permitidos:
            return Veredito(False, "campo_invalido", f"campo {args['campo']} não pode ser corrigido; use {sorted(permitidos)}")
        return None

    def _checar_bloquear_titulo(self, ator, args):
        return self._titulo_pendente(args["titulo_id"])

    def _checar_contatar_fornecedor(self, ator, args):
        canais = {"email", "telefone", "telefone_anterior"}
        if args["canal"] not in canais:
            return Veredito(False, "canal_invalido", f"canal deve ser um de {sorted(canais)} (sempre o do cadastro)")
        if self.amb.est.fornecedor(args["fornecedor_id"]) is None:
            return Veredito(False, "fornecedor_inexistente", f"fornecedor {args['fornecedor_id']} não existe")
        destino = args.get("destino")
        if destino:
            oficial = self.amb.destino_contato(args["fornecedor_id"], args["canal"])
            if str(destino).strip().lower() != str(oficial).strip().lower():
                return Veredito(False, "canal_fora_do_cadastro",
                                "contato com fornecedor só pelo e-mail/telefone do cadastro; o destino informado "
                                "não é o do cadastro (texto de mensagem nunca define o canal)")
        return None

    def _checar_acionar_pessoa(self, ator, args):
        if args["pessoa_id"] not in self.pessoas:
            return Veredito(False, "pessoa_invalida", f"pessoa_id deve ser um de {sorted(self.pessoas)}")
        return None

    def _checar_aprovar_acao(self, ator, args):
        sol = self.amb.est.q1("SELECT * FROM solicitacoes WHERE id = ?", [args["solicitacao_id"]])
        if not sol:
            return Veredito(False, "solicitacao_inexistente", f"{args['solicitacao_id']} não existe")
        if sol["status"] != "pendente":
            return Veredito(False, "solicitacao_encerrada", f"solicitação está {sol['status']}")
        if sol["solicitante"] == ator:
            return Veredito(False, "segregacao", "quem solicita não aprova a própria solicitação")
        a = self.amb.atores[ator]
        if not a["humano"]:
            return Veredito(False, "segregacao", "o agente nunca aprova ações; aprovação é de pessoa")
        aprov = json.loads(sol["aprovacoes"] or "[]")
        if any(x["login"] == ator for x in aprov):
            return Veredito(False, "segregacao", "a mesma pessoa não conta duas vezes numa aprovação dupla")
        # alçada: acima de R$ 50 mil o requisito é a diretora (PS03), que só ela supre
        if not self._contribui(sol, a):
            return Veredito(False, "aprovador_errado",
                            f"faltam aprovações de {self._faltam(sol)}; {a['nome']} não supre nenhuma")
        return None

    def _checar_recusar_acao(self, ator, args):
        sol = self.amb.est.q1("SELECT * FROM solicitacoes WHERE id = ?", [args["solicitacao_id"]])
        if not sol or sol["status"] != "pendente":
            return Veredito(False, "solicitacao_encerrada", "solicitação inexistente ou já decidida")
        if sol["solicitante"] == ator:
            return Veredito(False, "segregacao", "quem solicita não decide a própria solicitação")
        return None

    def _checar_liberar_lote(self, ator, args):
        return self._lote_aberto(args["lote_remessa"])

    def _checar_aprovar_alteracao_bancaria(self, ator, args):
        if not str(args.get("confirmacao") or "").strip():
            return Veredito(False, "sem_confirmacao", "exige confirmação registrada no telefone anterior")
        return None

    # ------------------------------------------------------------------ aprovação dupla: quem supre o quê
    def _faltam(self, sol: dict) -> list[str]:
        req = list(json.loads(sol["requer"] or "[]"))
        aprov = json.loads(sol["aprovacoes"] or "[]")
        for x in aprov:  # cada pessoa supre no máximo um requisito, os específicos primeiro
            for r in sorted(req, key=lambda r: r == "aprovador"):
                if self._supre(x, r):
                    req.remove(r)
                    break
        return req

    @staticmethod
    def _supre(pessoa: dict, requisito: str) -> bool:
        if requisito == "aprovador":
            return "aprovador" in set(pessoa.get("papeis") or [])
        return pessoa.get("pessoa_id") == requisito

    def _contribui(self, sol: dict, a: dict) -> bool:
        return any(self._supre({"papeis": a["papeis"], "pessoa_id": a["pessoa_id"]}, r) for r in self._faltam(sol))

    def solicitacao_completa(self, sol: dict) -> bool:
        return not self._faltam(sol)
