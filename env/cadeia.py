"""Cadeia de evidências (seção 3 do plano): o que PROVA que um pagamento é legítimo.

Cada elo vem da política (politicas.csv) e do dicionário de dados, não de uma lista de fraudes:
  E1 Compra · E2 Nota · E3 Valor · E4 Entrega · E5 Unicidade · E6 Destino · E7 Autoria e alçada ·
  E8 Prazo · E9 Sinais externos (o código só separa as comunicações ligadas ao título; quem lê é o LLM).

Status por elo: OK | QUEBRADO | INCONCLUSIVO | NAO_SE_APLICA (E9 também PENDENTE_LLM).
Resultado (E1 a E8): PROVADO (tudo OK/NA) | DUVIDOSO (algum inconclusivo) | QUEBRADO.
A cadeia NÃO decide nada: alimenta a Triagem. Elo quebrado é pergunta a investigar.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta

from env import boleto, db
from env.estado import cnpj_raiz, nf_norm
from env.fontes import FONTES_POR_ELO

OK, QUEBRADO, INCONCLUSIVO, NA, PENDENTE_LLM = "OK", "QUEBRADO", "INCONCLUSIVO", "NAO_SE_APLICA", "PENDENTE_LLM"
NOMES = {"E1": "Compra", "E2": "Nota", "E3": "Valor", "E4": "Entrega", "E5": "Unicidade", "E6": "Destino",
         "E7": "Autoria e alçada", "E8": "Prazo", "E9": "Sinais externos"}
CATEGORIAS_MERCADORIA = {"produto", "embalagens", "insumos_capa"}  # recebimento físico (WMS)
CAMPOS_BANCARIOS = {"banco", "agencia", "conta", "chave_pix"}
JANELA_COMUNICACOES_DIAS = 35
# politicas.csv (tres_vias.nf_x_oc, em texto): "a NF pode passar do valor da OC em até 2% ou R$ 50,00 (o que for maior)"
TOL_NF_OC_PCT, TOL_NF_OC_RS = 0.02, 50.0
TOL_TITULO_DUP = 0.10  # tres_vias.titulo_x_duplicata
_FONE = re.compile(r"\(\d{2}\)\s*\d{4,5}-\d{4}")
_GENERICAS = {"ltda", "eireli", "industria", "comercio", "servicos", "brasil", "grupo", "cia", "companhia",
              "distribuidora", "transportes", "comercial", "industrial", "empresa", "nordeste", "sul", "norte",
              "the", "and", "solucoes", "tecnologia", "digital"}


def _txt(s) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def _digitos(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _d(v) -> date | None:
    if v is None or v != v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _num(v) -> float | None:
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _brl(v: float | None) -> str:
    if v is None:
        return "?"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _elo(codigo: str, status: str, explicacao: str, evidencias: list | None = None, **extra) -> dict:
    return {"elo": codigo, "nome": NOMES[codigo], "status": status, "explicacao": explicacao,
            "evidencias": [str(e) for e in (evidencias or []) if e], **extra}


class Cadeia:
    """Índices estáticos carregados uma vez; o estado vivo é lido do Ambiente a cada verificação."""

    def __init__(self, amb):
        self.amb = amb
        self.pol = amb.politicas
        c = db.cursor(amb.con)

        def df(sql):
            d = c.execute(sql).df()
            return d.astype(object).where(d.notna(), None)  # NaN do pandas vira None (não "nan")

        tv = self.pol["tres_vias"]
        self.dispensa_oc = {tv["dispensas"]["oc"]}
        self.dispensa_nf = {tv["dispensas"]["nf"]}
        self.multa = float(self.pol["atraso"]["multa"])
        self.juros_mes = float(self.pol["atraso"]["juros_ao_mes"])
        alc = self.pol["alcadas"]
        self.lim_analista = float(alc["analista_cp"]["liberar_ou_agendar_ate"])
        self.lim_aprovador = float(alc["aprovador"]["aprova_ate"])

        forn = df("SELECT fornecedor_id, cnpj, categoria, razao_social, nome_fantasia FROM fornecedores")
        self.raiz_forn = {r.fornecedor_id: cnpj_raiz(r.cnpj) for r in forn.itertuples()}

        # notas e duplicatas
        nfs = df("SELECT chave_acesso, modelo, numero_nf, serie, cnpj_emitente, razao_social_emitente, "
                 "data_emissao, valor_total, valor_produtos_servicos, oc_informada, cnpj_destinatario, "
                 "situacao_sefaz, manifestacao_destinatario FROM notas_fiscais")
        self.nf_por_raiz_num: dict[tuple, list[dict]] = defaultdict(list)
        self.nf_por_num: dict[str, list[dict]] = defaultdict(list)
        for r in nfs.to_dict("records"):
            k = nf_norm(r["numero_nf"])
            self.nf_por_raiz_num[(cnpj_raiz(r["cnpj_emitente"]), k)].append(r)
            self.nf_por_num[k].append(r)
        self.raiz_empresa = nfs["cnpj_destinatario"].map(cnpj_raiz).mode().iloc[0]
        self.dups: dict[str, dict[int, dict]] = defaultdict(dict)
        for r in df("SELECT chave_acesso, n_dup, vencimento, valor FROM nf_duplicatas").to_dict("records"):
            self.dups[r["chave_acesso"]][int(r["n_dup"])] = r

        self.ocs = {r["oc_id"]: r for r in df("SELECT * FROM pedidos_compra").to_dict("records")}
        self.receb: dict[str, list[dict]] = defaultdict(list)
        for r in df("SELECT * FROM recebimentos_mercadoria").to_dict("records"):
            self.receb[r["chave_acesso"]].append(r)
        self.contratos: dict[str, list[dict]] = defaultdict(list)
        for r in df("SELECT * FROM contratos_fornecedores").to_dict("records"):
            self.contratos[r["fornecedor_id"]].append(r)

        # DDA
        self.dda_linha: dict[str, dict] = {}
        self.dda_doc: dict[tuple, list[dict]] = defaultdict(list)
        self.bancos_dda_raiz: dict[str, set] = defaultdict(set)
        for r in df("SELECT * FROM dda_boletos").to_dict("records"):
            self.dda_linha[r["linha_digitavel"]] = r
            raiz = cnpj_raiz(r["cnpj_beneficiario"])
            self.dda_doc[(raiz, str(r["numero_documento"]))].append(r)
            self.bancos_dda_raiz[raiz].add(str(r["banco_cobrador"]))

        # títulos históricos (não pendentes) por obrigação
        hist = df("SELECT titulo_id, fornecedor_id, numero_nf_digitado, parcela, valor, vencimento, status, "
                  "forma_pagamento, linha_digitavel, data_pagamento, lote_remessa, banco_pagador, lancado_por, "
                  "aprovado_por, nivel_aprovacao FROM contas_a_pagar")
        self.titulo_csv = {r["titulo_id"]: r for r in hist.to_dict("records")}
        self.obrigacao_hist: dict[tuple, list[dict]] = defaultdict(list)
        self.bancos_boleto_hist: dict[str, set] = defaultdict(set)
        self.pagos_fora_lote: list[dict] = []
        self.obrigacao_hist_por_forn: dict[str, list[dict]] = defaultdict(list)
        for r in hist.to_dict("records"):
            if r["status"] == "pago":
                self.obrigacao_hist_por_forn[r["fornecedor_id"]].append(r)
            if r["status"] in ("aberto", "agendado"):
                continue
            k = self._chave_obrigacao(r)
            if k:
                self.obrigacao_hist[k].append(r)
            if r["status"] == "pago":
                if r["forma_pagamento"] == "boleto" and r["linha_digitavel"]:
                    self.bancos_boleto_hist[r["fornecedor_id"]].add(str(r["linha_digitavel"])[:3])
                if not r["lote_remessa"]:
                    self.pagos_fora_lote.append(r)

        # autoria e aprovação
        self.usuarios = {r["login"]: r for r in df("SELECT * FROM usuarios_sistema").to_dict("records")}
        self.log_inclusao = {r["entidade_id"]: r for r in df(
            "SELECT * FROM log_auditoria_erp WHERE acao = 'inclusao_titulo'").to_dict("records")}
        self.aprovacoes: dict[str, list[dict]] = defaultdict(list)
        for r in df("SELECT * FROM aprovacoes").to_dict("records"):
            self.aprovacoes[r["titulo_id"]].append(r)

        # pagamentos avulsos nos extratos (sem título): Meridiano sem documento; Litoral não explicado pelo retorno
        self.avulsos = self._pagamentos_avulsos(df)

        # comunicações (texto normalizado)
        self.comunicacoes = df("SELECT comunicacao_id, data_hora, canal, remetente, nome_remetente, assunto, texto, "
                               "anexos FROM comunicacoes_fornecedores").to_dict("records")
        for r in self.comunicacoes:
            self._preparar_comunicacao(r)
        for a in self.avulsos:
            a["_toks"] = set(re.findall(r"[a-z0-9]+", _txt(a["descricao"])))

    @staticmethod
    def _preparar_comunicacao(r: dict) -> dict:
        """Texto normalizado, tokens e NFs citadas, calculados uma vez por comunicação."""
        r["_txt"] = _txt(" ".join(str(r.get(k) or "") for k in ("nome_remetente", "assunto", "texto", "anexos")))
        r["_toks"] = set(re.findall(r"[a-z0-9]+", r["_txt"]))
        r["_nfs"] = set(re.findall(r"(?:nf|nota fiscal|nota)\D{0,5}0*(\d{3,})", r["_txt"]))
        r["_rem"] = _txt(r.get("remetente"))
        r["_rem_dig"] = _digitos(r.get("remetente"))
        return r

    # ------------------------------------------------------------------ apoio
    def _chave_obrigacao(self, t: dict) -> tuple | None:
        n = nf_norm(t.get("numero_nf_digitado"))
        raiz = self.raiz_forn.get(t.get("fornecedor_id"))
        if not n or not raiz:
            return None
        return (raiz, n, str(t.get("parcela") or "").split("/")[0].lstrip("0"))

    def _pagamentos_avulsos(self, df) -> list[dict]:
        out = []
        for r in df("SELECT CAST(data AS VARCHAR) data, descricao, valor FROM extrato_banco_meridiano "
                    "WHERE tipo = 'PAGTO AVULSO'").to_dict("records"):
            out.append({**r, "banco": "Banco Meridiano"})
        lit = df("SELECT CAST(data AS VARCHAR) data, historico AS descricao, docto, valor FROM extrato_banco_litoral "
                 "WHERE valor < 0 AND historico NOT LIKE 'TAR%'").to_dict("records")
        explicados = defaultdict(int)
        for r in df("SELECT CAST(data_pagamento AS VARCHAR) d, valor_pago FROM retorno_remessa "
                    "WHERE lote_remessa LIKE '%-LIT' AND codigo_ocorrencia = '00'").to_dict("records"):
            explicados[(r["d"], round(float(r["valor_pago"]), 2))] += 1
        for r in lit:
            k = (r["data"], round(-float(r["valor"]), 2))
            if explicados[k] > 0:
                explicados[k] -= 1
                continue
            out.append({"data": r["data"], "descricao": r["descricao"], "valor": r["valor"], "banco": "Banco Litoral",
                        "docto": r["docto"]})
        return out

    def _tokens_nome(self, f: dict) -> set[str]:
        toks = set()
        for campo in ("nome_fantasia", "razao_social"):
            for w in re.findall(r"[a-z0-9]+", _txt(f.get(campo))):
                if len(w) >= 4 and w not in _GENERICAS:
                    toks.add(w)
        return toks

    def _dia_util(self, d: date) -> bool:
        return self.amb.relogio.dia_util(d)

    def _prox_util(self, d: date) -> date:
        return self.amb.relogio.proximo_dia_util(d)

    def _titulo(self, titulo_id: str) -> tuple[dict | None, bool]:
        t = self.amb.est.titulo(titulo_id)
        if t:
            return dict(t), True
        r = self.titulo_csv.get(titulo_id)
        return (dict(r), False) if r else (None, False)

    def _data_prevista(self, t: dict) -> date:
        if t.get("lote_remessa"):
            lote = self.amb.est.lote(t["lote_remessa"])
            if lote:
                return _d(lote["data_pagamento"])
        if t.get("data_pagamento"):
            return _d(t["data_pagamento"])
        venc = _d(t.get("vencimento")) or self.amb.est.agora.date()
        return self._prox_util(max(venc, self.amb.est.agora.date()))

    # ------------------------------------------------------------------ verificação
    def verificar(self, titulo_id: str) -> dict:
        t, pendente = self._titulo(titulo_id)
        if t is None:
            return {"titulo_id": titulo_id, "resultado": None, "erro": "título inexistente", "elos": []}
        f = self.amb.est.fornecedor(t["fornecedor_id"]) or {}
        ctx = {"t": t, "f": f, "raiz": self.raiz_forn.get(t["fornecedor_id"]) or f.get("cnpj_raiz"),
               "cat": f.get("categoria"), "nf": nf_norm(t.get("numero_nf_digitado")),
               "parcela_n": int(str(t.get("parcela") or "1/1").split("/")[0] or 1),
               "data_prevista": self._data_prevista(t), "agora": self.amb.est.agora}
        ctx["nota"] = self._achar_nf(ctx)
        ctx["dup"] = self.dups.get(ctx["nota"]["chave_acesso"], {}).get(ctx["parcela_n"]) if ctx["nota"] else None
        ctx["oc"] = self.ocs.get(t.get("oc_id")) if t.get("oc_id") else None
        ctx["contrato"] = next((c for c in self.contratos.get(t["fornecedor_id"], []) if c["status"] == "vigente"), None)
        elos = [self.e1(ctx), self.e2(ctx), self.e3(ctx), self.e4(ctx), self.e5(ctx), self.e6(ctx),
                self.e7(ctx), self.e8(ctx), self.e9(ctx)]
        # regra de segurança (seção 1d): dado que o sistema não entende nunca vira PROVADO
        drift = getattr(self.amb, "fontes_com_drift", set())
        if drift:
            for e in elos:
                afetadas = sorted(set(FONTES_POR_ELO.get(e["elo"], [])) & drift)
                if afetadas and e["status"] == OK:
                    e["status"] = INCONCLUSIVO
                    e["explicacao"] = f"fonte com drift ({', '.join(afetadas)}): não dá para provar; " + e["explicacao"]
                    e["evidencias"] = e["evidencias"] or [f"fonte:{f}" for f in afetadas]
        # E9 entra no resultado quando já foi lido pelo LLM (PENDENTE_LLM fica de fora e aparece em e9_pendente)
        st = [e["status"] for e in elos]
        resultado = QUEBRADO if QUEBRADO in st else ("DUVIDOSO" if INCONCLUSIVO in st else "PROVADO")
        return {
            "titulo_id": titulo_id, "fornecedor_id": t["fornecedor_id"], "categoria": ctx["cat"],
            "valor": _num(t.get("valor")), "lote_remessa": t.get("lote_remessa"),
            "data_prevista": ctx["data_prevista"].isoformat() if ctx["data_prevista"] else None,
            "pendente": pendente, "resultado": resultado, "e9_pendente": elos[8]["status"] == PENDENTE_LLM,
            "quebrados": [e["elo"] for e in elos if e["status"] == QUEBRADO],
            "inconclusivos": [e["elo"] for e in elos if e["status"] == INCONCLUSIVO],
            "elos": elos, "avaliado_em": self.amb.est.agora_txt,
        }

    def _achar_nf(self, ctx) -> dict | None:
        if not ctx["nf"]:
            return None
        cands = self.nf_por_raiz_num.get((ctx["raiz"], ctx["nf"]), [])
        return cands[0] if cands else None

    # ------------------------------------------------------------------ E1 Compra
    def e1(self, ctx) -> dict:
        t, oc = ctx["t"], ctx["oc"]
        if ctx["cat"] in self.dispensa_oc:
            return _elo("E1", NA, f"categoria {ctx['cat']} dispensada de OC pela política")
        if not t.get("oc_id"):
            return _elo("E1", QUEBRADO, "título sem OC informada (política: sem OC aprovada exige aprovação dupla)",
                        [t["titulo_id"]])
        if oc is None:
            return _elo("E1", QUEBRADO, f"OC {t['oc_id']} não existe no sistema de Compras", [t["titulo_id"]])
        ev = [t["titulo_id"], oc["oc_id"]]
        if cnpj_raiz(oc["cnpj_fornecedor"]) != ctx["raiz"]:
            return _elo("E1", QUEBRADO, f"OC {oc['oc_id']} é de outro fornecedor ({oc['nome_fornecedor']}, raiz "
                                        f"{cnpj_raiz(oc['cnpj_fornecedor'])} ≠ {ctx['raiz']})", ev)
        st = oc["status"]
        if st == "aprovada":
            return _elo("E1", OK, f"OC {oc['oc_id']} aprovada por {oc['aprovado_por']} em {_d(oc['data_aprovacao'])}", ev)
        if st == "encerrada":
            return _elo("E1", INCONCLUSIVO, f"OC {oc['oc_id']} está encerrada (Compras considera todas as parcelas "
                                            f"pagas), mas há título pendente nela", ev)
        return _elo("E1", QUEBRADO, f"OC {oc['oc_id']} com status '{st}' (não aprovada)", ev)

    # ------------------------------------------------------------------ E2 Nota
    def e2(self, ctx) -> dict:
        t, nota, nf = ctx["t"], ctx["nota"], ctx["nf"]
        if ctx["cat"] in self.dispensa_nf:
            return _elo("E2", NA, f"categoria {ctx['cat']} paga sem NF (boleto do contrato)")
        if not nf:
            return _elo("E2", QUEBRADO, "título sem número de NF (política: pagamento sem NF exige aprovação dupla)",
                        [t["titulo_id"]])
        if nota is None:
            outros = self.nf_por_num.get(nf, [])
            if outros:
                emis = "; ".join(f"{o['razao_social_emitente']} (raiz {cnpj_raiz(o['cnpj_emitente'])})" for o in outros[:3])
                return _elo("E2", QUEBRADO, f"NF {nf} não existe para a raiz do fornecedor ({ctx['raiz']}); "
                                            f"existe NF com esse número de outro emitente: {emis}",
                            [t["titulo_id"], *[f"chave:{o['chave_acesso']}" for o in outros[:3]]])
            return _elo("E2", QUEBRADO, f"NF {nf} não encontrada no Portal NF-e para a raiz {ctx['raiz']}",
                        [t["titulo_id"]])
        ev = [t["titulo_id"], f"chave:{nota['chave_acesso']}"]
        if nota["situacao_sefaz"] != "autorizada":
            return _elo("E2", QUEBRADO, f"NF {nf} com situação SEFAZ '{nota['situacao_sefaz']}'", ev)
        if cnpj_raiz(nota["cnpj_destinatario"]) != self.raiz_empresa:
            return _elo("E2", QUEBRADO, f"NF {nf} emitida para outro destinatário ({nota['cnpj_destinatario']})", ev)
        dups = self.dups.get(nota["chave_acesso"], {})
        if ctx["dup"] is None:
            return _elo("E2", QUEBRADO, f"NF {nf} não tem a duplicata {ctx['parcela_n']} (tem {sorted(dups)})", ev)
        total = str(t.get("parcela") or "").split("/")
        obs = ""
        if len(total) == 2 and total[1].isdigit() and int(total[1]) != len(dups):
            return _elo("E2", INCONCLUSIVO, f"título diz {t['parcela']} mas a NF tem {len(dups)} duplicatas", ev)
        if nota.get("oc_informada") and t.get("oc_id") and nota["oc_informada"] != t["oc_id"]:
            return _elo("E2", INCONCLUSIVO, f"NF cita a OC {nota['oc_informada']}, o título cita {t['oc_id']}", ev)
        return _elo("E2", OK, f"NF {nf} autorizada, emitente {nota['razao_social_emitente']}, duplicata "
                              f"{ctx['parcela_n']}/{len(dups)}{obs}", ev)

    # ------------------------------------------------------------------ E3 Valor
    def e3(self, ctx) -> dict:
        t, dup, nota, oc, ct = ctx["t"], ctx["dup"], ctx["nota"], ctx["oc"], ctx["contrato"]
        valor = _num(t.get("valor")) or 0.0
        ev = [t["titulo_id"]]
        problemas, duvidas, oks = [], [], []
        if dup is not None:
            ev.append(f"chave:{nota['chave_acesso']}#dup{ctx['parcela_n']}")
            vd = float(dup["valor"])
            if abs(valor - vd) <= TOL_TITULO_DUP:
                oks.append(f"título = duplicata ({_brl(vd)})")
            else:
                venc_dup = _d(dup["vencimento"])
                dias = (ctx["data_prevista"] - venc_dup).days if venc_dup and ctx["data_prevista"] else 0
                teto = vd * (1 + self.multa + self.juros_mes * max(dias, 0) / 30) + 0.10
                if dias > 0 and vd < valor <= teto:
                    oks.append(f"título {_brl(valor)} = duplicata {_brl(vd)} + multa/juros de {dias} dias de atraso")
                else:
                    problemas.append(f"título {_brl(valor)} ≠ duplicata {_brl(vd)} (diferença {_brl(valor - vd)})")
        elif ct is not None and ct.get("valor_variavel") == "não":
            ev.append(ct["contrato_id"])
            vm = float(ct["valor_mensal"])
            if abs(valor - vm) <= 0.10:
                oks.append(f"título = valor mensal do contrato {ct['contrato_id']} ({_brl(vm)})")
            else:
                duvidas.append(self._msg_contrato(valor, ct))
        elif ctx["cat"] not in self.dispensa_nf:
            duvidas.append("sem duplicata para conferir o valor")

        if nota is not None and oc is not None and cnpj_raiz(oc["cnpj_fornecedor"]) == ctx["raiz"]:
            base = float(oc["valor_mensal"]) if oc["tipo"] == "contrato" and _num(oc["valor_mensal"]) else float(oc["valor_total"])
            tol = max(TOL_NF_OC_PCT * base, TOL_NF_OC_RS)
            vn = float(nota["valor_total"])
            ev.append(oc["oc_id"])
            if vn > base + tol:
                problemas.append(f"NF {_brl(vn)} acima da OC {_brl(base)} além da tolerância ({_brl(tol)})")
            else:
                oks.append(f"NF {_brl(vn)} dentro da OC {_brl(base)}")
        if ct is not None and ct.get("valor_variavel") == "não" and nota is not None:
            vm, vn = float(ct["valor_mensal"]), float(nota["valor_total"])
            if abs(vn - vm) > max(TOL_NF_OC_PCT * vm, TOL_NF_OC_RS):
                duvidas.append(self._msg_contrato(vn, ct, "NF"))
                ev.append(ct["contrato_id"])
        if t.get("forma_pagamento") == "boleto" and t.get("linha_digitavel"):
            b = boleto.decodificar(t["linha_digitavel"])
            if b.get("valor") is not None and abs(b["valor"] - valor) > 0.01:
                problemas.append(f"boleto cobra {_brl(b['valor'])}, título é {_brl(valor)}")
        if problemas:
            return _elo("E3", QUEBRADO, "; ".join(problemas), ev)
        if duvidas:
            return _elo("E3", INCONCLUSIVO, "; ".join(duvidas + oks), ev)
        return _elo("E3", OK, "; ".join(oks) or "valores coerentes", ev)

    def _msg_contrato(self, valor: float, ct: dict, oque: str = "título") -> str:
        return (f"{oque} {_brl(valor)} difere do valor mensal do contrato {ct['contrato_id']} "
                f"({_brl(float(ct['valor_mensal']))}; reajuste anual pelo {ct['indice_reajuste']} no mês "
                f"{ct['mes_reajuste']}, último em {_d(ct['data_ultimo_reajuste'])})")

    # ------------------------------------------------------------------ E4 Entrega
    def e4(self, ctx) -> dict:
        t, nota, oc = ctx["t"], ctx["nota"], ctx["oc"]
        if ctx["cat"] not in CATEGORIAS_MERCADORIA:
            return _elo("E4", NA, f"categoria {ctx['cat']} não tem recebimento físico")
        if nota is None:
            return _elo("E4", INCONCLUSIVO, "sem NF localizada para conferir o recebimento", [t["titulo_id"]])
        recs = self.receb.get(nota["chave_acesso"], [])
        ev = [t["titulo_id"], f"chave:{nota['chave_acesso']}", *[r["recebimento_id"] for r in recs]]
        if not recs:
            return _elo("E4", INCONCLUSIVO, f"NF {ctx['nf']} sem recebimento registrado no WMS até agora", ev)
        qtd = sum(float(r["quantidade_recebida"] or 0) for r in recs)
        ressalvas = [r for r in recs if r["situacao"] != "conferido"]
        esperado = None
        if oc is not None and _num(oc.get("valor_unitario")) and _num(nota.get("valor_produtos_servicos")):
            esperado = float(nota["valor_produtos_servicos"]) / float(oc["valor_unitario"])
        if esperado and qtd < 0.98 * esperado:
            return _elo("E4", QUEBRADO, f"recebido {qtd:,.0f} {recs[0]['unidade']} de ~{esperado:,.0f} faturados "
                                        f"(NF / preço unitário da OC)", ev)
        txt = f"recebido {qtd:,.0f} {recs[0]['unidade']}" + (f" (~{esperado:,.0f} faturados)" if esperado else "")
        if ressalvas:
            # a pergunta do elo é "recebida em quantidade compatível?"; a ressalva fica registrada como observação
            obs = "; ".join(str(r.get("observacao") or "") for r in ressalvas)
            return _elo("E4", OK, f"{txt}; conferido com ressalva: {obs}", ev, ressalva=True)
        return _elo("E4", OK, txt, ev)

    # ------------------------------------------------------------------ E5 Unicidade
    def e5(self, ctx) -> dict:
        t = ctx["t"]
        valor = _num(t.get("valor")) or 0.0
        k = self._chave_obrigacao(t)
        ev = [t["titulo_id"]]
        problemas, duvidas = [], []
        if k:
            raiz, n, p = k
            ids_forn = [fid for fid, r in self.raiz_forn.items() if r == raiz]
            pend = self.amb.est.q(
                "SELECT titulo_id, estado, status_erp, lote_remessa, valor FROM titulos WHERE nf_norm = ? AND "
                f"fornecedor_id IN ({','.join('?' * len(ids_forn))}) AND titulo_id <> ?", [n, *ids_forn, t["titulo_id"]])
            pend = [r for r in pend if self._chave_obrigacao({**self.amb.est.titulo(r["titulo_id"])}) == k
                    and r["estado"] != "cancelado"]
            for r in pend:
                pago = r["status_erp"] == "pago"
                problemas.append(
                    f"outro título da mesma obrigação: {r['titulo_id']} ({_brl(r['valor'])}, "
                    f"{'já pago' if pago else r['estado']}{', lote ' + r['lote_remessa'] if r['lote_remessa'] else ''})")
                ev.append(r["titulo_id"])
            pendentes_ids = {r["titulo_id"] for r in pend}
            for r in self.obrigacao_hist.get(k, []):
                if r["titulo_id"] == t["titulo_id"] or r["titulo_id"] in pendentes_ids or r["status"] == "cancelado":
                    continue
                problemas.append(f"obrigação já {r['status']} pelo título {r['titulo_id']} "
                                 f"({_brl(r['valor'])} em {_d(r['data_pagamento'])})")
                ev.append(r["titulo_id"])
            # boleto da obrigação já liquidado na CIP
            for d in self.dda_doc.get((raiz, f"{n}/{p}"), []):
                if d["situacao"] == "liquidado" and not any(x["status"] == "pago" for x in self.obrigacao_hist.get(k, [])):
                    problemas.append(f"boleto {d['dda_id']} desta NF/parcela já está liquidado na CIP "
                                     f"(em {_d(d['data_situacao'])}) sem título pago no ERP")
                    ev.append(d["dda_id"])
        else:
            # sem NF: mesma competência (fornecedor + vencimento + valor)
            for r in self.amb.est.q("SELECT titulo_id, estado FROM titulos WHERE fornecedor_id = ? AND vencimento = ? "
                                    "AND abs(valor - ?) <= 0.10 AND titulo_id <> ? AND estado <> 'cancelado'",
                                    [t["fornecedor_id"], str(t.get("vencimento"))[:10], valor, t["titulo_id"]]):
                problemas.append(f"outro título do mesmo fornecedor, vencimento e valor: {r['titulo_id']} ({r['estado']})")
                ev.append(r["titulo_id"])
        # pagamento por outro caminho (extrato avulso com mesmo valor e nome do fornecedor)
        toks = self._tokens_nome(ctx["f"])
        inicio = _d(ctx["nota"]["data_emissao"]) if ctx["nota"] else (ctx["agora"] - timedelta(days=60)).date()
        for a in self.avulsos + self._avulsos_sim():
            if abs(abs(float(a["valor"])) - valor) > 0.01 or str(a["data"]) < str(inicio):
                continue
            if toks and not (toks & a.get("_toks", set())):
                continue
            if self._avulso_explicado(a, t):
                continue
            duvidas.append(f"pagamento avulso de mesmo valor no extrato do {a['banco']} em {a['data']} "
                           f"('{a['descricao']}'): pode ser esta obrigação paga por fora do lote")
            ev.append(f"extrato:{a['banco']}:{a['data']}:{a['descricao']}:{a['valor']}")
        if problemas:
            return _elo("E5", QUEBRADO, "; ".join(problemas + duvidas), ev)
        if duvidas:
            return _elo("E5", INCONCLUSIVO, "; ".join(duvidas), ev)
        return _elo("E5", OK, "nenhum outro título nem pagamento da mesma obrigação", ev)

    def _avulsos_sim(self) -> list[dict]:
        return []  # pagamentos manuais injetados durante a simulação entram aqui (eventos futuros)

    def _avulso_explicado(self, a: dict, t: dict) -> bool:
        """Avulso já baixado no ERP para outro título (pago fora do lote no mesmo dia e valor)."""
        for p in self.pagos_fora_lote:
            if str(_d(p["data_pagamento"])) == str(a["data"]) and abs(float(p["valor"]) - abs(float(a["valor"]))) <= 0.01 \
                    and p["titulo_id"] != t["titulo_id"] and self.raiz_forn.get(p["fornecedor_id"]) == self.raiz_forn.get(t["fornecedor_id"]):
                return True
        return False

    # ------------------------------------------------------------------ E6 Destino
    def e6(self, ctx) -> dict:
        t, f = ctx["t"], ctx["f"]
        forma = t.get("forma_pagamento")
        if forma == "boleto":
            return self._e6_boleto(ctx)
        ev = [t["titulo_id"], t["fornecedor_id"]]
        if forma == "ted" and not (f.get("banco") and f.get("agencia") and f.get("conta")):
            return _elo("E6", QUEBRADO, "TED sem banco/agência/conta no cadastro", ev)
        if forma == "pix" and not f.get("chave_pix"):
            return _elo("E6", QUEBRADO, "PIX sem chave no cadastro", ev)
        alts = [a for a in self.amb.alteracoes_cadastro(t["fornecedor_id"])
                if a["campo"] in CAMPOS_BANCARIOS and not a.get("revertida_por")]
        if not alts:
            return _elo("E6", OK, f"{forma} para o cadastro vigente, sem alteração bancária registrada", ev)
        agora = ctx["agora"]
        ultima_data = max(str(a["data_hora"]) for a in alts)
        grupo = [a for a in alts if str(a["data_hora"]) == ultima_data]
        ev += [a["alteracao_id"] for a in grupo]
        quando = datetime.fromisoformat(ultima_data)
        recente = (agora - quando) < timedelta(days=30)
        idade = f"há {(agora - quando).days} dias" if recente else f"em {quando.date()}"
        campos = ", ".join(sorted({a["campo"] for a in grupo}))
        confirmada = all((a.get("confirmacao_registrada") or "").lower() == "sim" for a in grupo)
        if not confirmada:
            # política (dados_bancarios.troca_de_conta): a troca SÓ VALE com confirmação registrada, em qualquer idade.
            # A idade entra como severidade (troca antiga com pagamentos liquidados é menos grave, mas não está provada).
            pagos_depois = [r for r in self.amb.est.q(
                "SELECT titulo_id FROM titulos WHERE fornecedor_id = ? AND status_erp = 'pago'", [t["fornecedor_id"]])]
            pagos_depois += [r for r in self.obrigacao_hist_por_forn.get(t["fornecedor_id"], [])
                             if r["status"] == "pago" and str(r["data_pagamento"]) >= ultima_data[:10]
                             and r["forma_pagamento"] in ("ted", "pix")]
            sev = "alta" if recente else ("media" if not pagos_depois else "baixa")
            return _elo("E6", QUEBRADO, f"dados bancários alterados {idade} ({campos}, origem {grupo[0].get('origem')}) "
                                        f"sem confirmação registrada (a troca só vale com confirmação); "
                                        f"{len(pagos_depois)} pagamento(s) ted/pix já feito(s) depois da troca",
                        ev, severidade=sev)
        # confirmação só vale por ligação ao telefone que estava no cadastro ANTES das alterações recentes
        valido = self._telefone_em(t["fornecedor_id"], quando - timedelta(days=30))
        texto = " ".join(str(a.get("confirmacao_como") or "") for a in grupo)
        fones = {_digitos(x) for x in _FONE.findall(texto)}
        if fones and _digitos(valido) in fones:
            return _elo("E6", OK, f"alteração bancária {idade} confirmada por ligação ao telefone anterior "
                                  f"({valido})", ev)
        if fones:
            return _elo("E6", QUEBRADO, f"alteração bancária {idade} 'confirmada' em telefone que não é o que estava no "
                                        f"cadastro antes ({', '.join(_FONE.findall(texto))} ≠ {valido})", ev)
        st = INCONCLUSIVO
        return _elo("E6", st, f"alteração bancária {idade} com confirmação que não foi ligação ao telefone anterior "
                              f"({texto.strip()[:160]})", ev)

    def _telefone_em(self, fornecedor_id: str, instante: datetime) -> str | None:
        f = self.amb.est.fornecedor(fornecedor_id) or {}
        v = f.get("telefone_cadastrado")
        tel = [a for a in self.amb.alteracoes_cadastro(fornecedor_id) if a["campo"] == "telefone"]
        for a in reversed(tel):
            if datetime.fromisoformat(str(a["data_hora"])) > instante:
                v = a["valor_anterior"]
        return v

    def _e6_boleto(self, ctx) -> dict:
        t, raiz = ctx["t"], ctx["raiz"]
        linha = t.get("linha_digitavel")
        ev = [t["titulo_id"]]
        b = boleto.decodificar(linha)
        if not b["valida"]:
            return _elo("E6", QUEBRADO, f"linha digitável inválida: {b.get('erro') or b.get('verificadores')}", ev)
        if t.get("cnpj_beneficiario_boleto") and cnpj_raiz(t["cnpj_beneficiario_boleto"]) != raiz:
            return _elo("E6", QUEBRADO, f"beneficiário do boleto ({t['cnpj_beneficiario_boleto']}) não é da raiz do "
                                        f"fornecedor ({raiz})", ev)
        d = self.dda_linha.get(linha)
        if d is not None:
            ev.append(d["dda_id"])
            if cnpj_raiz(d["cnpj_beneficiario"]) != raiz:
                return _elo("E6", QUEBRADO, f"boleto registrado no DDA para outro beneficiário: {d['beneficiario']} "
                                            f"({d['cnpj_beneficiario']})", ev)
            if d["situacao"] == "baixado pelo beneficiário":
                return _elo("E6", QUEBRADO, f"boleto {d['dda_id']} foi baixado pelo beneficiário em "
                                            f"{_d(d['data_situacao'])}: não deve ser pago", ev)
            return _elo("E6", OK, f"boleto registrado no DDA por {d['beneficiario']} (banco {d['banco_cobrador']}, "
                                  f"{d['situacao']})", ev)
        doc = f"{ctx['nf']}/{ctx['parcela_n']}" if ctx["nf"] else None
        outros = self.dda_doc.get((raiz, doc), []) if doc else []
        if outros:
            ev += [o["dda_id"] for o in outros]
            return _elo("E6", QUEBRADO, f"o boleto registrado no DDA para a NF/parcela {doc} tem outra linha digitável "
                                        f"(banco {outros[0]['banco_cobrador']}); o título usa banco {b['banco']}", ev)
        habituais = self.bancos_boleto_hist.get(t["fornecedor_id"], set()) | self.bancos_dda_raiz.get(raiz, set())
        f = ctx["f"]
        if b["banco"] in habituais or b["banco"] == str(f.get("banco")):
            return _elo("E6", INCONCLUSIVO, f"boleto não registrado no DDA (banco {b['banco']} é o de cobrança "
                                            f"habitual do fornecedor)", ev)
        return _elo("E6", QUEBRADO, f"boleto não registrado no DDA e banco de cobrança {b['banco']} nunca usado pelo "
                                    f"fornecedor (habituais: {sorted(habituais) or 'nenhum'}; cadastro: {f.get('banco')})", ev)

    # ------------------------------------------------------------------ E7 Autoria e alçada
    def e7(self, ctx) -> dict:
        t = ctx["t"]
        ev = [t["titulo_id"]]
        u = self.usuarios.get(t.get("lancado_por"))
        problemas, duvidas, oks = [], [], []
        dt_lanc = _d(t.get("data_lancamento"))
        if u is None:
            problemas.append(f"lançado por usuário desconhecido ({t.get('lancado_por')})")
        else:
            desl = _d(u.get("data_desligamento"))
            if desl and dt_lanc and dt_lanc > desl:
                problemas.append(f"lançado em {dt_lanc} por {u['nome']}, desligado em {desl}")
            adm = _d(u.get("data_admissao"))
            if adm and dt_lanc and dt_lanc < adm:
                problemas.append(f"lançado em {dt_lanc}, antes da admissão de {u['nome']} ({adm})")
            perfil_ok = u["perfil"] == "analista_cp" or (
                u["perfil"] == "integracao" and t.get("origem_lancamento") == "integracao_frete"
                and u["login"] == "integracao_frete")
            if not perfil_ok:
                problemas.append(f"perfil '{u['perfil']}' de {u['login']} não lança este título")
            else:
                oks.append(f"lançado por {u['nome']} ({u['perfil']})")
        log = self.log_inclusao.get(t["titulo_id"])
        if log is None:
            duvidas.append("inclusão sem trilha no log de auditoria do ERP")
        else:
            ev.append(log["log_id"])
            if log["usuario"] != t.get("lancado_por"):
                problemas.append(f"log de inclusão é de {log['usuario']}, título diz {t.get('lancado_por')}")
            elif u is not None and u.get("estacao_trabalho") and log["estacao"] != u["estacao_trabalho"]:
                duvidas.append(f"incluído da estação {log['estacao']}, a do usuário é {u['estacao_trabalho']}")
        for a in self.aprovacoes.get(t["titulo_id"], []):
            ev.append(a["aprovacao_id"])
            if a["decisao"] == "reprovado":
                problemas.append(f"título reprovado na conferência por {a['aprovador']} em {a['data_hora']}")
            if a["aprovador"] == t.get("lancado_por"):
                problemas.append(f"segregação: {a['aprovador']} lançou e aprovou")
        # fracionamento: PARTES DIFERENTES da mesma NF (mesmo fornecedor) pagas no mesmo dia.
        # Dois títulos da MESMA parcela não são fracionamento, são duplicidade (E5).
        if ctx["nf"]:
            dia = ctx["data_prevista"]
            mesmos, parcelas = [], set()
            for r in self.amb.est.q(
                    "SELECT titulo_id, valor, parcela FROM titulos WHERE fornecedor_id = ? AND nf_norm = ? "
                    "AND estado NOT IN ('cancelado')", [t["fornecedor_id"], ctx["nf"]]):
                if self._data_prevista(dict(self.amb.est.titulo(r["titulo_id"]))) != dia:
                    continue
                if r["titulo_id"] != t["titulo_id"] and r["parcela"] == t.get("parcela"):
                    continue
                if r["parcela"] in parcelas and r["titulo_id"] != t["titulo_id"]:
                    continue
                parcelas.add(r["parcela"])
                mesmos.append(r)
            soma = sum(float(r["valor"]) for r in mesmos)
            if len(mesmos) > 1 and soma > self.lim_analista and all(float(r["valor"]) <= self.lim_analista for r in mesmos):
                problemas.append(f"fracionamento: {len(mesmos)} títulos da mesma NF no dia {dia} somam {_brl(soma)}, "
                                 f"cada um abaixo da alçada do analista ({_brl(self.lim_analista)})")
                ev += [r["titulo_id"] for r in mesmos if r["titulo_id"] != t["titulo_id"]]
        if problemas:
            return _elo("E7", QUEBRADO, "; ".join(problemas), ev)
        if duvidas:
            return _elo("E7", INCONCLUSIVO, "; ".join(duvidas + oks), ev)
        return _elo("E7", OK, "; ".join(oks) or "autoria coerente", ev)

    # ------------------------------------------------------------------ E8 Prazo
    def e8(self, ctx) -> dict:
        t, dup, ct = ctx["t"], ctx["dup"], ctx["contrato"]
        venc = _d(t.get("vencimento"))
        ev = [t["titulo_id"]]
        prev = ctx["data_prevista"]
        problemas, duvidas, oks = [], [], []
        if venc is None:
            return _elo("E8", INCONCLUSIVO, "título sem vencimento", ev)
        if dup is not None:
            vd = _d(dup["vencimento"])
            ev.append(f"chave:{ctx['nota']['chave_acesso']}#dup{ctx['parcela_n']}")
            if venc == vd or venc == self._prox_util(vd):
                oks.append(f"vencimento {venc} = duplicata ({vd})")
            elif venc < vd:
                problemas.append(f"vencimento do título {venc} antes do vencimento da duplicata {vd} (antecipação)")
            else:
                duvidas.append(f"vencimento do título {venc} depois do da duplicata {vd}")
        elif ct is not None and _num(ct.get("dia_vencimento")):
            dia = int(float(ct["dia_vencimento"]))
            try:
                alvo = venc.replace(day=dia)
            except ValueError:
                alvo = None
            ev.append(ct["contrato_id"])
            if alvo and (venc == alvo or venc == self._prox_util(alvo)):
                oks.append(f"vencimento no dia {dia} do contrato {ct['contrato_id']}")
            else:
                duvidas.append(f"vencimento {venc} fora do dia {dia} do contrato {ct['contrato_id']}")
        if prev and prev < venc:
            problemas.append(f"pagamento previsto em {prev}, antes do vencimento {venc} (antecipação exige aprovação dupla)")
        elif prev and prev > self._prox_util(venc):
            oks.append(f"pagamento em {prev}, depois do vencimento {venc} (atraso: multa e juros)")
        if t.get("forma_pagamento") == "boleto" and t.get("linha_digitavel"):
            b = boleto.decodificar(t["linha_digitavel"])
            vb = _d(b.get("vencimento"))
            if vb and venc not in (vb, self._prox_util(vb)):
                duvidas.append(f"boleto vence em {vb}, título em {venc}")
        if problemas:
            return _elo("E8", QUEBRADO, "; ".join(problemas), ev)
        if duvidas:
            return _elo("E8", INCONCLUSIVO, "; ".join(duvidas + oks), ev)
        return _elo("E8", OK, "; ".join(oks) or f"vencimento {venc} coerente", ev)

    # ------------------------------------------------------------------ E9 Sinais externos
    def comunicacoes_ligadas(self, ctx) -> list[dict]:
        """Comunicações recentes do fornecedor (canal do cadastro, mesmo domínio ou nome no texto) que citam
        esta NF ou não citam NF nenhuma. O CÓDIGO só filtra; quem interpreta é o LLM (E9)."""
        f, nf = ctx["f"], ctx["nf"]
        agora = ctx["agora"]
        inicio = (agora - timedelta(days=JANELA_COMUNICACOES_DIAS)).strftime("%Y-%m-%d %H:%M:%S")
        fim = agora.strftime("%Y-%m-%d %H:%M:%S")
        email = _txt(f.get("email_cadastrado"))
        dominio = email.split("@")[-1] if "@" in email else None
        fone = _digitos(f.get("telefone_cadastrado"))
        toks = self._tokens_nome(f)
        sim = [self._preparar_comunicacao(r)
               for r in self.amb.est.q("SELECT * FROM comunicacoes_sim WHERE data_hora <= ?", [fim])]
        out = []
        for r in self.comunicacoes + sim:
            dh = str(r["data_hora"])[:19]
            if dh < inicio or dh > fim:
                continue
            rem = r["_rem"]
            do_forn = (rem == email or (dominio and rem.endswith("@" + dominio)) or (fone and r["_rem_dig"] == fone)
                       or bool(toks & r["_toks"]))
            if not do_forn:
                continue
            if nf and r["_nfs"] and nf not in r["_nfs"]:
                continue
            out.append(r)
        return out

    def e9(self, ctx) -> dict:
        coms = self.comunicacoes_ligadas(ctx)
        if not coms:
            return _elo("E9", NA, "nenhuma comunicação recente ligada a este título ou fornecedor")
        ids = [c["comunicacao_id"] for c in coms]
        sinal = self._leitura_e9(ctx["t"]["fornecedor_id"], ctx["t"]["titulo_id"], ids)
        if sinal is None:
            return _elo("E9", PENDENTE_LLM, f"{len(coms)} comunicação(ões) recente(s) ligadas: leitura pelo LLM "
                                            f"pendente", ids)
        ev = sinal.get("evidencias") or ids
        txt = f"comunicações lidas ({sinal['sinal']}, gravidade {sinal['gravidade']}): {sinal.get('resumo', '')}"
        if sinal["sinal"] in ("contradiz", "ameaca") and sinal["gravidade"] >= 2:
            return _elo("E9", QUEBRADO, txt, ev, leitura=sinal)
        if sinal["sinal"] in ("contradiz", "ameaca") and sinal["gravidade"] == 1:
            return _elo("E9", INCONCLUSIVO, txt, ev, leitura=sinal)
        return _elo("E9", OK, txt, ev, leitura=sinal)

    def comunicacoes_do_titulo(self, titulo_id: str) -> list[dict]:
        """Comunicações ligadas a um título (para o leitor de E9)."""
        t, _ = self._titulo(titulo_id)
        if t is None:
            return []
        f = self.amb.est.fornecedor(t["fornecedor_id"]) or {}
        ctx = {"t": t, "f": f, "nf": nf_norm(t.get("numero_nf_digitado")), "agora": self.amb.est.agora}
        return [{k: v for k, v in c.items() if not k.startswith("_")} for c in self.comunicacoes_ligadas(ctx)]

    def _leitura_e9(self, fornecedor_id: str, titulo_id: str, ids: list[str]) -> dict | None:
        """Usa a leitura mais recente do fornecedor se ela cobriu TODAS as comunicações ligadas a este título."""
        try:
            leituras = self.amb.est.q("SELECT comunicacoes, resultado FROM e9_leituras WHERE fornecedor_id = ? "
                                      "ORDER BY quando DESC", [fornecedor_id])
        except Exception:
            return None
        import json as _json
        for l in leituras:
            if set(ids) <= set(_json.loads(l["comunicacoes"])):
                for s in _json.loads(l["resultado"]):
                    if s["titulo_id"] == titulo_id:
                        return s
        return None
