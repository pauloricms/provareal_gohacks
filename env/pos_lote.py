"""Depois do fechamento: retorno do banco, linha de extrato, conciliação e baixa (seção 1b, item 5).

O banco simulado rejeita o que não consegue pagar (conta incompleta, pix sem chave, boleto com
linha digitável inválida). Pago = vira linha de extrato no formato do banco e é conciliado.
Títulos ainda pendentes ligados à mesma obrigação (fornecedor/NF) são marcados para reavaliação.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from env import boleto, ciclo_vida
from env.estado import FMT

HORA_RETORNO = {"Banco Meridiano": 16, "Banco Litoral": 18}  # histórico: ~15:30 e ~17:30


def agendar_retorno(relogio, lote: dict) -> str:
    dia = datetime.fromisoformat(lote["data_pagamento"])
    quando = dia.replace(hour=HORA_RETORNO.get(lote["banco_pagador"], 17))
    relogio.agendar_evento(quando, "retorno_banco", {"lote_remessa": lote["lote_remessa"]}, origem="banco")
    return quando.strftime(FMT)


def _validar_destino(saida: dict, titulo: dict, fornecedor: dict | None) -> tuple[str, str]:
    forma = saida["forma_pagamento"]
    f = fornecedor or {}
    # códigos e descrições iguais aos do retorno_remessa.csv
    if forma == "ted":
        if not (f.get("banco") and f.get("agencia") and f.get("conta")):
            return "AN", "Conta corrente/DV do favorecido inválido"
    elif forma == "pix":
        if not f.get("chave_pix"):
            return "PJ", "Chave Pix não localizada no DICT"
    elif forma == "boleto":
        d = boleto.decodificar(titulo.get("linha_digitavel"))
        if not d["valida"]:
            return "CC", "Código de barras - dígito verificador geral inválido"
    else:
        return "FP", "Forma de pagamento inválida"
    return "00", "Crédito ou débito efetivado"


def _linha_extrato(banco: str, saida: dict, titulo: dict, fornecedor: dict | None) -> dict:
    from env.relogio import nome_abreviado

    f = fornecedor or {}
    nome = nome_abreviado(f.get("nome_fantasia") or f.get("razao_social"))
    forma = saida["forma_pagamento"]
    cod = (titulo.get("linha_digitavel") or "")[:3] if forma == "boleto" else (f.get("banco") or "")
    if banco == "Banco Meridiano":
        tipo = {"ted": "PAGTO TED", "pix": "PAGTO PIX", "boleto": "PAGTO BOLETO"}.get(forma, "PAGTO AVULSO")
        desc = f"{'BOLETO' if forma == 'boleto' else forma.upper()} {cod} {nome}".strip()
        doc = saida["titulo_id"]
    else:
        tipo = forma.upper()
        desc = {"ted": f"TED ENV {cod} {nome}", "pix": f"PIX ENV {nome}",
                "boleto": f"PAG BOL {cod} {nome}"}.get(forma, nome)
        doc = None  # o Litoral não traz o número do título
    return {"banco": banco, "tipo": tipo, "descricao": desc, "documento": doc, "valor": -round(saida["valor"], 2)}


def processar_retorno(est, log, lote_remessa: str) -> dict:
    agora = est.agora_txt
    lote = est.lote(lote_remessa)
    banco = lote["banco_pagador"]
    pagos, rejeitados, afetados = [], [], set()
    with est.transacao():
        for s in est.q("SELECT * FROM lote_saida WHERE lote_remessa = ? AND enviado_em IS NOT NULL", [lote_remessa]):
            t = est.titulo(s["titulo_id"])
            f = est.fornecedor(s["fornecedor_id"])
            cod, desc = _validar_destino(s, t, f)
            est.x("UPDATE lote_saida SET ocorrencia = ?, descricao_ocorrencia = ?, retorno_em = ? "
                  "WHERE lote_remessa = ? AND titulo_id = ?", [cod, desc, agora, lote_remessa, s["titulo_id"]])
            ciclo_vida.transicionar(est, s["titulo_id"], "retorno_banco", "banco", f"retorno {cod}: {desc}")
            if cod != "00":
                ciclo_vida.transicionar(est, s["titulo_id"], "retido", "pos_lote",
                                        f"rejeitado pelo banco ({cod}: {desc}); precisa reagendar",
                                        campos={"lote_remessa": None, "status_erp": "aberto", "reavaliar": 1,
                                                "motivo_reavaliacao": f"rejeitado pelo banco: {desc}"})
                rejeitados.append({"titulo_id": s["titulo_id"], "ocorrencia": cod, "descricao": desc})
                continue
            linha = _linha_extrato(banco, s, t, f)
            est.inserir("extrato_sim", {**linha, "data": lote["data_pagamento"]})
            # conciliação: Meridiano pelo documento; Litoral por valor + nome (sem número do título)
            ciclo_vida.transicionar(est, s["titulo_id"], "conciliado", "pos_lote", "conciliado com o extrato")
            est.x("UPDATE lote_saida SET conciliado_em = ? WHERE lote_remessa = ? AND titulo_id = ?",
                  [agora, lote_remessa, s["titulo_id"]])
            ciclo_vida.transicionar(est, s["titulo_id"], "baixado", "pos_lote", "baixa pelo retorno",
                                    campos={"status_erp": "pago", "data_pagamento": lote["data_pagamento"]})
            pagos.append(s["titulo_id"])
            if t["nf_norm"]:
                afetados.update(r["titulo_id"] for r in est.q(
                    "SELECT titulo_id FROM titulos WHERE fornecedor_id = ? AND nf_norm = ? AND estado IN (%s)"
                    % ",".join(f"'{e}'" for e in ciclo_vida.ABERTOS | ciclo_vida.NA_FILA | {"bloqueado"}),
                    [t["fornecedor_id"], t["nf_norm"]]))
        # obrigação relacionada foi paga: reavaliar os pendentes da mesma NF
        afetados -= set(pagos)
        ciclo_vida.marcar_reavaliacao(est, sorted(afetados), f"NF relacionada paga no {lote_remessa}")
    resumo = {"lote_remessa": lote_remessa, "pagos": len(pagos), "rejeitados": rejeitados,
              "reavaliacao_disparada": sorted(afetados)}
    log.registrar(agora, "banco", "retorno_processado", **resumo)
    return resumo
