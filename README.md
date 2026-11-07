# Prova antes do pagamento: sistema de agentes de contas a pagar

GoHacks Talent 2026 · desafio d2 (Pagamentos).

Toda manhã, um lote de remessa sai para o banco e **o que está no lote é pago**. Este sistema confere cada título
antes do fechamento. Ele exige uma **cadeia de evidências** (compra → nota → valor → recebimento → duplicidade →
dados bancários → fracionamento → prazo → comunicações) e só deixa sair o que está provado. O que o código não
consegue provar vira um caso para agentes de IA investigarem. Um segundo agente, de outra família de modelo, refaz
a checagem. As pessoas só são chamadas quando a política exige.

O objetivo é o placar do desafio: **−(dinheiro pago indevidamente + custo de atrasar pagamento legítimo + R$ 60 por
acionamento desnecessário)**. Não existe gabarito: o sistema não conhece a resposta certa e decide pelas evidências e
pelo custo esperado, com as regras de `politicas.csv`.

## Como funciona, em uma figura

```
a cada hora simulada (tique):
  código ── cadeia E1–E9 em todos os títulos em aberto ──► PROVADO? ──► certificado (sai no lote)
     │                                                    não
     ▼
  triagem ──► Chefe (prioriza pelo lote que fecha antes) ──► Investigadores (LLM, em paralelo)
                                                                 │ proposta + evidências + custo esperado
                                                                 ▼
                                                  Crítico (outra família de modelo, consultas próprias)
                                                                 │ concorda / refuta
                                                                 ▼
                         Executor (código) ──► Guarda da política (alçadas, aprovação dupla, segregação,
                                                 canal do cadastro) ──► ação ou pedido de aprovação
                                                                 ▼
                         Comunicador: 1 card por pessoa e lote, só com o que a política reserva a pessoas
  + Pontualidade (devolve o legítimo ao lote a tempo) · Relacionamento (responde fornecedor pelo canal do cadastro)
  + Curador às 18h (consolida detectores, mede precisão, lições, propostas de processo)
```

Detalhes e decisões em [`NOTAS_IA.md`](NOTAS_IA.md). Mapa do código em [`CLAUDE.md`](CLAUDE.md). Formato da gravação
de uma execução em [`runs/SCHEMA.md`](runs/SCHEMA.md).

## 1. Preparar

**Requisitos:** Python 3.12 ou mais novo (desenvolvido em 3.14) e acesso a um endpoint de LLM compatível com a API da
OpenAI. No evento, usamos o AI proxy da organização.

```
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install openai duckdb pandas pyarrow python-dotenv pydantic pytest streamlit
```

Todos os comandos abaixo rodam a partir da raiz do repositório.

**Dados (não estão no repositório; são confidenciais do evento):** copie os 26 arquivos da base de Pagamentos (CSVs,
`dicionario_de_dados.md` e `parametros.md`) para:

```
data/pagamentos/
```

**Credenciais:** crie um arquivo `.env` na raiz. Ele está no `.gitignore` e nunca deve ser versionado.

```
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
# opcionais: troca de modelo por papel
# MODELO_DECISAO=gpt-6-sol
# MODELO_LEVE=gpt-6-luna
# MODELO_CRITICO=mimo-v2.6-pro
# POOL=0            # desliga o revezamento de modelos por papel
```

**Conferir a instalação** (sem LLM; os testes usam um LLM falso e determinístico):

```
python -m env.db                  # monta o cache DuckDB a partir dos CSVs (.cache/)
python -m pytest -q               # 61 testes, ~2 min
python relatorio_cadeia.py        # cadeia de evidências no próximo lote, só código (~2 s)
```

## 2. Reproduzir o experimento (deixar o sistema rodar)

A simulação começa em **02/11/2026 08:00**, com o retrato dos dados, e avança uma hora por tique. O primeiro lote
fecha **terça 03/11 às 10:00** (Banco Meridiano), e o que estiver nele é pago.

```
python -m agente.orquestrador --ate "2026-11-03 10:00"
```

- A primeira linha mostra a pasta da execução, por exemplo `execução: runs\20261107_143739`. Tudo fica gravado nela.
- Cada tique imprime uma linha de progresso: hora simulada, segundos reais, chamadas de LLM, casos, decisões e cards.
- **Duração:** de 45 a 55 min de relógio para as 26 horas simuladas. As horas da madrugada passam em ~1 s, porque o
  código resolve tudo sem LLM.
- Para um teste curto, use `--ate "2026-11-02 10:00"` (2 tiques, ~15 min; os primeiros tiques são os mais pesados,
  porque a fila inicial de casos é grande).
- Para continuar uma execução interrompida na mesma pasta:
  `python -m agente.orquestrador --run runs\<pasta> --ate "2026-11-03 10:00"`
- Outros parâmetros: `--orcamento` (chamadas de LLM por tique, padrão 25) e `--paralelo` (investigações simultâneas,
  padrão 4).

O sistema não depende de nenhuma pessoa durante a execução. As decisões que a política reserva a pessoas viram
cards (fila de aprovação) e a simulação segue. O tesoureiro assina o lote no horário, como faria sem conferir.

## 3. Ver as métricas

Ao fim da execução, gere o placar e o painel da pasta (troque `<pasta>` pelo nome impresso na largada):

```
python -m metricas.resumo runs/<pasta>     # cria runs/<pasta>/resumo.md e runs/<pasta>/resumo.json
python -m app.painel runs/<pasta>          # cria runs/<pasta>/painel.html (sem argumento: a execução mais recente)
```

**Abra no navegador o arquivo `runs/<pasta>/painel.html`.** Por exemplo, na nossa execução final:
`runs/20261107_143739/painel.html`. A pasta é gerada pela execução e não vai para o repositório, porque contém
valores dos dados. O arquivo é independente (os dados vão embutidos), não precisa de servidor e
funciona offline. Acrescente `#t=26` ao endereço para abrir direto na última hora.

O que o painel mostra:

| Onde | O quê |
|---|---|
| Topo | replay (play/pausa, barra do tempo, velocidade, "próximo lote", "ir para o caso") |
| 4 números | quanto vai sair no próximo lote · % provado só pelo código · **pagamentos ilegítimos barrados** · decisões levadas a pessoas |
| Gráfico "Sem o sistema, isto seria pago" | R$ barrado × custo de atraso possível, acumulados hora a hora, e a **margem de segurança** |
| O lote, título a título | cada título é um quadrado que muda de cor (provado, duvidoso, elo quebrado, segurado, pago) |
| O que o sistema está fazendo | os acontecimentos da hora em frases curtas, com link para o caso |
| Resultado | sem o sistema × com o sistema, ganho líquido, tabela de sensibilidade e **cada pagamento barrado** com motivo, certeza do agente e custo de atraso se fosse legítimo |
| Abas | Casos (a história de cada um), Aprendizado (detectores e curadoria), Tempo (hora simulada × tempo real) |

Outras formas de ver os mesmos dados:
- `runs/<pasta>/resumo.md`: o placar em texto (atraso por obrigação, bug × decisão, lotes futuros, casos, tempos).
- `runs/<pasta>/eventos.jsonl`: a trilha de auditoria completa (cada ferramenta, proposta, veredito e chamada de LLM).
- `python -m streamlit run app/streamlit_app.py`: painel alternativo em Streamlit, restrito a `127.0.0.1`.

### Como o placar é contado (sem gabarito)

- **Por obrigação** (fornecedor + NF + parcela), nunca por título: um par duplicado é um pagamento possível.
- **Pagamento em dobro evitado:** cópia segurada quando a mesma obrigação saiu pela outra via.
- **Suspeito barrado:** conta o valor indevido (em erro de valor digitado, só o excesso) e a probabilidade de
  problema estimada pelo agente. Rotulado "estimado pelo agente, verificável pela trilha".
- **Custo de atraso** de um legítimo segurado: multa 2% + juros de 1% a.m. por 7 dias + custo de relacionamento da
  categoria, pela política. O atraso causado por **bug** do código aparece separado do atraso por **decisão** do agente.
- **Margem de segurança:** até que fração dos barrados poderia ser legítima com o saldo ainda positivo.

## 4. Resultado da execução final

Execução até o fechamento do lote de terça 03/11 10:00, com o código final:

| Métrica | Valor |
|---|---|
| Títulos conferidos pela cadeia (código) | 2.398 em aberto; 192 no lote de terça |
| Provados só pelo código, no lote | 86,5% |
| Investigados por agentes (LLM) | 79 títulos em 29 casos |
| Pagamentos ilegítimos barrados | 31 pagamentos, R$ 271.790 (R$ 265.797 pela probabilidade do agente) |
| No lote de terça | 4 pagamentos em dobro evitados (R$ 20.174,83) + 3 suspeitos segurados |
| Ganho líquido no lote de terça | R$ 30.835,14 (sem o sistema: R$ 32.264,92 · custo do sistema: R$ 1.429,78) |
| Atraso causado por bug | R$ 0 (um bug da execução anterior, de R$ 3.201,14, foi achado, corrigido e testado) |
| Margem de segurança | saldo positivo mesmo se até **88,8%** dos barrados fossem legítimos |
| Decisões levadas a pessoas | 9, em 4 cards (94,3% dos títulos sem envolver pessoa) |
| Crítico | 24 vereditos, 5 dispensas pela regra de custo |
| Tempo | 46 min reais para 24 h simuladas; a hora mais pesada usou 13,5% de uma hora real |

Os imprevistos da execução (cota de um modelo esgotada no meio, troca do Crítico, modelos lentos retirados do pool)
estão registrados em [`NOTAS_IA.md`](NOTAS_IA.md), junto com cada erro da IA que programou e dos próprios agentes.

## Segurança e limites

- O SQL dos agentes roda em DuckDB **somente leitura**, sem acesso a arquivos. Só o Executor (código) altera o
  estado, e sempre pelo guarda da política.
- Texto de e-mail, NF ou mensagem de colega é tratado como **dado, nunca como instrução**. Contato com fornecedor só
  pelo canal do cadastro.
- **Limitação conhecida:** o Python dos agentes roda com lista de bloqueio e diretório temporário com cópias. Não é um
  sandbox de verdade (próximo passo: contêiner).
- Ainda não implementados: verificação automática de citações, auditoria por amostragem e troca automática de modelo
  quando a cota de um provedor acaba.

## Estrutura

```
env/        ambiente simulado: dados, relógio, lotes, banco, guarda da política, ferramentas, cadeia E1–E9
agente/     LLM, quadro compartilhado, orquestrador, papéis (triagem, chefe, investigador, crítico, executor...)
metricas/   resumo e placar de uma execução
app/        painel HTML/JS (painel.py + painel.html) e painel Streamlit
scripts/    testes do proxy e de modelos, reconstrução e curadoria de execuções gravadas
tests/      61 testes (sem rede; LLM falso)
runs/       execuções gravadas (fora do git; só runs/SCHEMA.md sobe)
```
