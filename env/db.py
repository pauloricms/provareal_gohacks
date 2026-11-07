"""Camada de dados: DuckDB construído a partir dos CSVs originais (que nunca são editados).

- Os CSVs são lidos como texto e convertidos para número/data só quando TODOS os valores
  não vazios da coluna convertem (assim IDs como '0412', '001' e CNPJs não perdem zeros).
- O extrato do Banco Litoral (separador ';', dd/mm/aaaa, vírgula decimal) é normalizado.
- O banco fica em cache num arquivo .duckdb; o agente só recebe conexões SOMENTE LEITURA,
  sem acesso a arquivos externos (não consegue ler _local/ nem nada fora do banco).
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import duckdb
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "data" / "pagamentos"
CACHE = RAIZ / ".cache" / "pagamentos.duckdb"

# colunas candidatas a conversão (o resto fica texto)
_NUMERICAS = re.compile(r"^(valor.*|saldo.*|.*_rs|qtd_.*|quantidade.*|volumes|valor_unitario|custo_rs)$")
_DATAS = re.compile(r"^(data.*|vencimento|.*_vigencia.*|saldo_em)$")
_SEM_CONVERSAO = {"dia_vencimento", "mes_reajuste"}


def _ler_csv(caminho: Path) -> pd.DataFrame:
    df = pd.read_csv(caminho, dtype=str, keep_default_na=False)
    return df.replace({"": None})


def _ler_litoral(caminho: Path) -> pd.DataFrame:
    df = pd.read_csv(caminho, sep=";", dtype=str, keep_default_na=False)
    df.columns = ["data", "historico", "docto", "valor", "saldo"]

    def num(s: str) -> float:
        return float(s.replace(".", "").replace(",", "."))

    df["data"] = pd.to_datetime(df["data"], format="%d/%m/%Y").dt.date
    df["valor"] = df["valor"].map(num)
    df["saldo"] = df["saldo"].map(num)
    return df


def _sql_tipada(con: duckdb.DuckDBPyConnection, nome_tmp: str, df: pd.DataFrame) -> str:
    """Monta um SELECT que converte as colunas que convertem sem perda."""
    exprs = []
    for col in df.columns:
        q = f'"{col}"'
        alvo = None
        if col not in _SEM_CONVERSAO:
            if _NUMERICAS.match(col):
                alvo = "DOUBLE"
            elif _DATAS.match(col):
                tem_hora = df[col].dropna().astype(str).str.contains(":").any()
                alvo = "TIMESTAMP" if tem_hora else "DATE"
        if alvo:
            falhas = con.execute(
                f"SELECT count(*) FROM {nome_tmp} WHERE {q} IS NOT NULL AND TRY_CAST({q} AS {alvo}) IS NULL"
            ).fetchone()[0]
            if falhas == 0:
                exprs.append(f"CAST({q} AS {alvo}) AS {q}")
                continue
        exprs.append(q)
    return f"SELECT {', '.join(exprs)} FROM {nome_tmp}"


def politicas_dict(df: pd.DataFrame) -> dict:
    """Converte politicas.csv (secao, regra 'a.b[1].c', valor) em dict aninhado (o 'politicas.json')."""
    raiz: dict = {}
    for _, linha in df.iterrows():
        secao, regra, valor = linha["secao"], linha["regra"], linha["valor"]
        if regra == secao:
            raiz[secao] = _converter_valor(valor)
            continue
        no = raiz.setdefault(secao, {})
        # chaves podem conter pontos dentro de nomes ('Banco Meridiano'); separamos por '.' fora de colchetes
        partes = regra.split(".")
        for i, parte in enumerate(partes):
            m = re.match(r"^(.*)\[(\d+)\]$", parte)
            ultimo = i == len(partes) - 1
            if m:
                lista = no.setdefault(m.group(1), [])
                idx = int(m.group(2)) - 1
                while len(lista) <= idx:
                    lista.append({})
                no = lista[idx]
            elif ultimo:
                no[parte] = _converter_valor(valor)
            else:
                no = no.setdefault(parte, {})
    return raiz


def _converter_valor(v):
    if v is None:
        return None
    try:
        return float(v) if re.fullmatch(r"-?\d+\.\d+", v) else int(v) if re.fullmatch(r"-?\d+", v) else v
    except ValueError:
        return v


def construir_cache(forcar: bool = False) -> Path:
    """(Re)constrói o arquivo DuckDB a partir dos CSVs quando algum CSV for mais novo que o cache."""
    csvs = sorted(DADOS.glob("*.csv"))
    if not forcar and CACHE.exists():
        if all(c.stat().st_mtime <= CACHE.stat().st_mtime for c in csvs) and \
                Path(__file__).stat().st_mtime <= CACHE.stat().st_mtime:
            return CACHE
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    try:
        for caminho in csvs:
            nome = caminho.stem
            if nome == "extrato_banco_litoral":
                df = _ler_litoral(caminho)
                con.register("_tmp", df)
                con.execute(f"CREATE TABLE {nome} AS SELECT * FROM _tmp")
            else:
                df = _ler_csv(caminho)
                con.register("_tmp", df)
                con.execute(f"CREATE TABLE {nome} AS {_sql_tipada(con, '_tmp', df)}")
            con.unregister("_tmp")
        # politicas também em formato aninhado (equivale ao politicas.json da documentação)
        pol = politicas_dict(_ler_csv(DADOS / "politicas.csv"))
        con.execute("CREATE TABLE politicas_json (conteudo VARCHAR)")
        con.execute("INSERT INTO politicas_json VALUES (?)", [json.dumps(pol, ensure_ascii=False)])
    finally:
        con.close()
    if CACHE.exists():
        CACHE.unlink()
    tmp.rename(CACHE)
    return CACHE


_lock = threading.Lock()


SEARCH_PATH = "SET search_path = 'dados.main,memory.main'"


def conexao_leitura(travar: bool = True) -> duckdb.DuckDBPyConnection:
    """Conexão somente leitura, sem acesso a arquivos externos.

    Os dados ficam no catálogo 'dados' (anexado READ_ONLY); o catálogo em memória principal
    serve só para registrar visões do estado da simulação (DataFrames). Depois de anexar,
    o acesso externo é desligado; com travar=True (SQL do agente) a configuração também é travada.
    """
    with _lock:
        construir_cache()
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{CACHE.as_posix()}' AS dados (READ_ONLY)")
    con.execute(SEARCH_PATH)
    con.execute("SET enable_external_access = false")
    if travar:
        con.execute("SET lock_configuration = true")
    return con


def cursor(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    """Cursor (seguro entre threads) de uma conexão NÃO travada, já apontando para o catálogo 'dados'."""
    c = con.cursor()
    c.execute(SEARCH_PATH)
    return c


def tabelas(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_catalog='dados' ORDER BY 1"
    ).fetchall()]


def politicas() -> dict:
    con = conexao_leitura()
    try:
        return json.loads(con.execute("SELECT conteudo FROM politicas_json").fetchone()[0])
    finally:
        con.close()


if __name__ == "__main__":
    import time

    t = time.time()
    construir_cache(forcar=True)
    print(f"cache construído em {time.time() - t:.1f}s -> {CACHE}")
    c = conexao_leitura()
    for tb in tabelas(c):
        n = c.execute(f"SELECT count(*) FROM {tb}").fetchone()[0]
        print(f"{tb:32s} {n:>7d}")
