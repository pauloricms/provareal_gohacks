"""Roda a cadeia de evidências num lote e mostra a distribuição por elo (F2).

    python relatorio_cadeia.py                         # o próximo lote a fechar
    python relatorio_cadeia.py --lote <lote_remessa>
    python relatorio_cadeia.py --agenda                # todos os lotes da agenda
    python relatorio_cadeia.py --detalhe               # lista cada elo quebrado/inconclusivo com evidências

Sem gabarito: os números são o que a cadeia (código, sem LLM) consegue provar ou não.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter

from env.ambiente import Ambiente
from env.cadeia import NOMES, Cadeia
from env.guarda import brl

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lote", default=None, help="lote_remessa (padrão: o próximo a fechar)")
    ap.add_argument("--agenda", action="store_true", help="todos os títulos da agenda (todos os lotes)")
    ap.add_argument("--detalhe", action="store_true")
    a = ap.parse_args()

    t0 = time.perf_counter()
    amb = Ambiente.novo()
    cadeia = Cadeia(amb)
    t_init = time.perf_counter() - t0
    if not a.lote:
        a.lote = amb.relogio.proximo_fechamento()["lote_remessa"]
    if a.agenda:
        ids = [r["titulo_id"] for r in amb.est.q("SELECT titulo_id FROM titulos WHERE lote_remessa IS NOT NULL")]
        alvo = "agenda inteira"
    else:
        ids = [r["titulo_id"] for r in amb.est.q("SELECT titulo_id FROM titulos WHERE lote_remessa = ? "
                                                 "ORDER BY valor DESC", [a.lote])]
        alvo = a.lote
    t1 = time.perf_counter()
    res = [cadeia.verificar(i) for i in ids]
    t_aval = time.perf_counter() - t1

    n = len(res)
    total = sum(r["valor"] for r in res)
    print(f"== Cadeia de evidências: {alvo} ({n} títulos, {brl(total)})")
    print(f"   tempo: carga {t_init:.1f} s + avaliação {t_aval:.1f} s ({1000 * t_aval / max(n, 1):.0f} ms/título)\n")

    print("Resultado (elos E1 a E8, só código):")
    for st in ("PROVADO", "DUVIDOSO", "QUEBRADO"):
        sel = [r for r in res if r["resultado"] == st]
        print(f"   {st:9s} {len(sel):4d} ({100 * len(sel) / n:5.1f}%)  {brl(sum(r['valor'] for r in sel)):>18s}")
    so_codigo = [r for r in res if r["resultado"] == "PROVADO" and not r["e9_pendente"]]
    e9 = [r for r in res if r["resultado"] == "PROVADO" and r["e9_pendente"]]
    print(f"\n   % provado SÓ em código (E1-E8 ok e sem comunicação ligada): {100 * len(so_codigo) / n:.1f}% "
          f"({len(so_codigo)} títulos, {brl(sum(r['valor'] for r in so_codigo))})")
    print(f"   provados em E1-E8 que só dependem da leitura de comunicações (E9, LLM leve): {len(e9)}")

    print("\nPor elo:")
    print(f"   {'elo':22s} {'OK':>5s} {'QUEBR':>6s} {'INCON':>6s} {'N/A':>5s} {'LLM':>5s}")
    for cod, nome in NOMES.items():
        c = Counter(next(e["status"] for e in r["elos"] if e["elo"] == cod) for r in res)
        print(f"   {cod} {nome:19s} {c['OK']:5d} {c['QUEBRADO']:6d} {c['INCONCLUSIVO']:6d} "
              f"{c['NAO_SE_APLICA']:5d} {c['PENDENTE_LLM']:5d}")

    print("\nMotivos mais comuns (primeira frase da explicação) por elo quebrado/inconclusivo:")
    motivos = Counter()
    for r in res:
        for e in r["elos"]:
            if e["status"] in ("QUEBRADO", "INCONCLUSIVO"):
                chave = e["explicacao"].split(";")[0]
                chave = " ".join(w if not any(ch.isdigit() for ch in w) else "#" for w in chave.split())
                motivos[(e["elo"], e["status"], chave[:110])] += 1
    for (elo, st, m), k in motivos.most_common(25):
        print(f"   {k:4d}  {elo} {st:12s} {m}")

    if a.detalhe:
        print("\nDetalhe (títulos QUEBRADOS e DUVIDOSOS):")
        for r in sorted(res, key=lambda r: (r["resultado"] != "QUEBRADO", -r["valor"])):
            if r["resultado"] == "PROVADO":
                continue
            print(f"\n   {r['titulo_id']}  {r['fornecedor_id']} ({r['categoria']})  {brl(r['valor'])}  -> {r['resultado']}")
            for e in r["elos"]:
                if e["status"] in ("QUEBRADO", "INCONCLUSIVO"):
                    print(f"      {e['elo']} {e['status']}: {e['explicacao']}")
                    print(f"         evidências: {', '.join(e['evidencias'][:8])}")

    saida = amb.run_dir / f"relatorio_cadeia_{'agenda' if a.agenda else a.lote}.json"
    saida.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\nJSON completo: {saida}")


if __name__ == "__main__":
    main()
