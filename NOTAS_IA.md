# Notas: onde a IA errou e como foi corrigida

Registro honesto de cada vez que a IA (o Claude Code, que escreveu o código, e, a partir da F3, os próprios agentes)
errou e de como o erro foi pego. A banca avalia "conferir o que a IA fez", e este arquivo é a prova.
**Não existe gabarito**: os erros foram pegos por testes de mecanismo, revisão de código e inspeção dos motivos que a
cadeia apontava, nunca por comparação com uma lista de respostas.

## F1: Ambiente (Claude Code programando)

| # | Erro da IA | Como foi pego | Correção |
|---|---|---|---|
| 1 | Os cursores DuckDB criados a partir da conexão do ambiente perdiam o `search_path`, então as consultas internas falhavam com "tabela não existe". | Teste de fumaça das ferramentas (6 de 18 deram `erro_interno`). | `db.cursor()` redefine o `search_path`; a conexão interna não trava a configuração, e a do agente continua travada. |
| 2 | Parâmetros vazios em `ferramentas.csv` viravam `NaN` (float) e quebravam o catálogo. | Exceção na criação do ambiente. | Normalização para texto antes do parse. |
| 3 | `contatar_fornecedor` **ignorava em silêncio** um `destino` fora do cadastro. Era seguro, mas o agente não ficaria sabendo que errou. | Revisão da saída do teste de fumaça (`parametros_ignorados: destino`). | O guarda passou a **recusar** explicitamente (`canal_fora_do_cadastro`). Virou regra do projeto. |
| 4 | O pós-lote marcava para reavaliação todos os títulos com o mesmo **número de NF de qualquer fornecedor** (critérios unidos por OU). | Revisão do código antes de rodar. | A dependência passou a ser "mesmo fornecedor **e** mesma NF". Virou regra do projeto. |
| 5 | Um teste escolhia "qualquer título que saiu" e pegava um do lote Litoral, cujo retorno só chega às 18:00. | Falha no pytest. | O teste filtra pelo lote e verifica também que o Litoral ainda está `enviado`. |
| 6 | A primeira simulação de fornecedor respondia sempre "confirmo o cadastro atual", o que tornava o contato inútil (ou enganoso) se o cadastro tivesse sido trocado por terceiros. | Revisão do Paulo. | `env/mundo.py`: quem responde é derivado do histórico do mundo (seção 9.2 do plano). |
| 7 | O Python do agente tinha um furo: `pandas.read_csv("/caminho/qualquer")` lê arquivo sem usar `open`. | Revisão do Paulo. | O subprocesso roda num diretório temporário só com **cópias** (DuckDB copiado + Parquet do estado), sem caminho do repositório, sem variáveis do proxy, e a lista de bloqueio passou a cobrir `read_*`/`to_*`/`np.load`. |

## F2: Cadeia de evidências (Claude Code programando)

| # | Erro da IA | Como foi pego | Correção |
|---|---|---|---|
| 8 | `NaN` do pandas tratado como valor: 49 títulos com "NF cita a OC **nan**" (falso INCONCLUSIVO no E2). | Leitura dos motivos mais comuns no `relatorio_cadeia.py`. | Carga da cadeia converte NaN em `None`. Teste garante que nenhuma explicação contém "nan". |
| 9 | O mesmo NaN fazia `lote_remessa` vazio parecer preenchido (NaN é "verdadeiro" em Python): a lista de títulos pagos **fora do lote** ficava vazia e 4 parcelas anteriores pagas manualmente apareciam como "pagamento avulso suspeito" (E5). | Depois da correção 8 esses 4 alertas sumiram sem eu ter mexido na regra; em vez de aceitar, investiguei: cada "avulso" era a parcela anterior da mesma NF, paga fora do lote e baixada no ERP. | Mesma correção; o comportamento novo foi conferido caso a caso. |
| 10 | E8 comparava o vencimento do **boleto** com o do título sem aceitar o "próximo dia útil" (que a política aceita e que eu já aplicava à duplicata): 26 falsos INCONCLUSIVOS (boletos de sábado, domingo e Finados). | Motivos do relatório + inspeção (todos venciam em fim de semana/feriado). | Mesma regra de próximo dia útil para boleto e duplicata. |
| 11 | E7 contava **duplicidade** (dois títulos da mesma parcela) como **fracionamento**: 6 alertas que eram o mesmo problema que o E5 já apontava. | Inspeção dos 6 casos: pares com a mesma NF **e a mesma parcela**. | Fracionamento = partes **diferentes** da mesma NF no mesmo dia. Teste cobre os dois casos. |
| 12 | A fila de reavaliação ordenava pelo **nome** do lote: `RM…-LIT` vem antes de `RM…-MER` no alfabeto, mas o Meridiano fecha às 10h e o Litoral às 14h. | Revisão do código ao ligar a cadeia no tique. | Ordena pela data + horário real de fechamento. |
| 13 | Desempenho: regex refeito para cada título × comunicação e um commit (fsync) por escrita. Tique com 400 títulos levava ~4,5 s; a suíte de testes, 88 s. | `pytest --durations` + cProfile. | Tokens pré-computados e reavaliação em uma transação: **0,5 s** por tique, suíte em 24 s. |

## Ajustes pós-F2 e F3 (Claude Code programando)

| # | Erro da IA | Como foi pego | Correção |
|---|---|---|---|
| 14 | E6: troca bancária sem confirmação com mais de 30 dias ficava INCONCLUSIVA; a política diz que a troca **só vale** com confirmação, em qualquer idade (os 30 dias são de outra regra). | Revisão do Paulo (9b.3). | QUEBRADO em qualquer idade; idade e pagamentos já liquidados viram **severidade** (alta/média/baixa). |
| 15 | Eu ia forçar a saída estruturada com `tool_choice` apontando a função; o proxy recusa nos três modelos (o `k3.1-pro` por estar com "thinking"). | `scripts/probe_llm.py` antes de escrever o cliente. | Envio só a ferramenta de registro com `tool_choice="required"` (funciona em todos). |
| 16 | Teste de drift copiava as tabelas via pandas e mudava os tipos (`DATE` → `TIMESTAMP_NS`): o monitor acusou drift real em todas as fontes, inclusive nas que o teste não mexeu. | Falha no pytest. | Cópia direta no DuckDB. Bom sinal: o monitor pega mudança de tipo que nem era intencional. |
| 17 | `carregar_atores` lia `pessoa_id` vazio como **NaN** (verdadeiro em Python): um aprovador comum parecia ter papel nomeado na política. Mesmo bug dos itens 8 e 9, em outro lugar. | Ao tirar os IDs fixos dos testes (seção 10), os testes passaram a escolher "aprovador sem pessoa da política" por critério e não acharam nenhum. | NaN vira `None` na carga dos usuários. |
| 18 | Eu disse que o teste do Curador "passou rodado sozinho" sem ter rodado a suíte completa depois de adicioná-lo. | Pergunta do Paulo. | Suíte completa rodada: 53 testes passando, inclusive o do Curador. Regra: fim de etapa = suíte completa, nunca só o teste novo. |
| 20 | Subi o Streamlit com o padrão do Streamlit (escuta em todas as interfaces): o painel, que lê `runs/` com dados confidenciais, ficou acessível pela rede local e por IP externo. | O log de partida listou "Network URL" e "External URL". | Derrubado em segundos e reiniciado com `--server.address 127.0.0.1`; `.streamlit/config.toml` fixa `127.0.0.1` como padrão do projeto. |
| 21 | Na largada da execução final, usei o modo de segundo plano da ferramenta, que tem limite de 10 min; a execução leva ~50. | Revisão antes de seguir. | Parada no primeiro minuto (pasta descartada) e relançada como processo separado do Windows (`Start-Process`), com log em `runs/execucao_final.log`. |
| 22 | Pus `glm-5.3-flash` e `mimo-v2.6-flash` no pool leve com base num teste de pedido curtíssimo (2 a 6 s). Com os pedidos reais (5 a 10 mil tokens) eles levaram **28 a 40 s** por chamada (o `gpt-6-luna`, 5 s) e o tique 2 da execução final ficou 3× mais lento que o da execução de 13:14. | Pergunta do Paulo ("por que ficou mais lento?"); tempos por modelo na trilha (`llm_chamada`). | Com autorização do Paulo: execução parada às 14:56 no meio do tique das 12:00, pool leve reduzido a `gpt-6-luna` e `gpt-5.6-luna`, 4 casos que estavam em investigação voltaram para a triagem, e a execução continuou na mesma pasta (`--run`), com evento `execucao_retomada`. Lição: teste de modelo tem que usar pedido do tamanho real. |
| 19 | IDs de pessoa da política (coordenadora, controller) e o login do tesoureiro estavam fixos no código e num prompt; os testes tinham ~40 IDs reais dos dados. | Varredura antes de publicar (script local que tira os termos sensíveis dos próprios dados). | Pessoas derivadas da política (`aprovacao_dupla.quem`, `acionar_quando`), tesoureiro derivado da política + usuários; testes escolhem o que precisam por critério (`tests/auxiliares.py`). Varredura: 0 achados. |

## F3: erros e correções DOS AGENTES (observados na trilha da primeira execução real)

| # | O que aconteceu | Quem pegou | Efeito |
|---|---|---|---|
| A1 | O leitor de E9 (`gpt-6-luna`) marcou "contradiz/2" num título porque o e-mail trazia um boleto com outro valor e outra linha digitável. | O **Investigador** cruzou duplicatas e DDA: o anexo era o boleto da parcela 1/3, e o título é a 2/3. | Proposta "manter no lote"; o **Crítico** reproduziu com consultas próprias. Um falso positivo evitado sem humano. |
| A2 | O Crítico (`k3.1-pro`) errou nomes de tabela no SQL e gastou chamadas caras (15 a 50 s cada). | Leitura da trilha (`llm_chamada` + resultado `sql_erro`). | Catálogo de tabelas no prompt fixo de todos os papéis; Crítico ganhou `descrever_tabela`; orçamento do Crítico caiu de 4 para 3. |
| A3 | Num caso com várias alegações, o Crítico confirmou a central (duplicidade) e declarou explicitamente que **não** reproduziu duas secundárias. | O próprio Crítico (objeções no veredito). | Comportamento desejado: o veredito não "carimba" o que não verificou. |

| A4 | O Investigador propôs `cancelar_titulo` acima da alçada do analista (R$ 5 mil) em 7 casos de duplicidade. | O **guarda** barrou (`requer_aprovacao`); o Executor pediu aprovação e tirou o título do lote enquanto isso; o **Crítico** também apontou a alçada nas objeções. | O dinheiro não saiu e a decisão foi para a pessoa certa, num card único por lote. |
| A5 | 16 detectores criados em 26 tiques, vários quase iguais (variações de "mesma NF e parcela lançada duas vezes"), e dois ruidosos (25 e 28 alertas para divergência de centavos). | Leitura da tabela `detectores`. | Pendência: o Curador (que entrou depois do início desta execução) deve consolidar duplicados e medir a precisão; detector ruidoso é rebaixado sozinho. |
| A6 | O Relacionamento mandou ao controller um item "pagamento parcial" sem título nem pagamento encontrado (R$ 0,00), um possível acionamento desnecessário (R$ 60). | Leitura do card das 17h. | Corrigido (14:45): sem fato ligado, não aciona pessoa (`encaminhamento_adiado`) e pede o dado ao fornecedor pelo canal do cadastro. Teste. |
| A7 | 3 chamadas estouraram o timeout de 60 s (proxy enfileirado); 1 `agendar_verificacao` com data inválida; 1 detector com SQL inválido. | Eventos `llm_falha` e respostas de erro das ferramentas. | Caso volta para a triagem no próximo tique; ferramenta recusa com motivo explícito (nada passa em silêncio). |

| A8 | **Bug do Executor (código, não do LLM):** enquanto uma ação forte esperava aprovação, ele tirava do lote **todos** os títulos da proposta. Num par duplicado de R$ 9.006,36, o agente decidiu "cancelar a cópia e pagar o legítimo"; o cancelamento exigia aprovação e o legítimo foi retirado junto. Atraso indevido de um título de produto: **R$ 3.201,14** pela política (2% + juros de 7 dias + R$ 3.000 de relacionamento). | Pergunta do Paulo ("dos 9 retidos, quantos eram o legítimo de um par?"), conferida título a título na trilha. | Executor segura só o **alvo** da ação (`_alvos`); teste com par duplicado garante que o legítimo continua no lote. |
| A9 | Noutro par duplicado (R$ 4.750), o agente segurou as duas vias de propósito: um e-mail relatava pagamento em dobro de uma parcela anterior, e o Crítico confirmou no ERP. Decisão de julgamento, não bug: se o título era devido, o atraso custa **R$ 906,08**. | Mesma conferência. | Entra no placar como custo de atraso (sem gabarito: pela regra da política, rotulado como decisão do agente). |

Observação de operação (execução de 13:14): com os prompts reais (contexto de vários mil tokens), o `gpt-6-luna` levou ~6 s sozinho e ~26 s com 4 em paralelo; o `k3.1-pro` leva de 15 a 50 s. O gargalo do tique é o Crítico. **O teste de concorrência de 14:30 (abaixo) mostrou que, com pedido curto, NÃO há fila**: a lentidão em paralelo vem do tamanho do pedido, não de uma fila por modelo.

### Teste de concorrência do proxy (seção 9e, 07/11 14:30, `scripts/teste_concorrencia.py`)
Mesmo pedido curto ("17 + 25", ferramenta forçada com `tool_choice="required"`), sem retry:

| Rodada | Tempo de cada chamada | Total |
|---|---|---|
| Sequencial, 1 por vez | luna 2,7 s · 5.6-luna 2,0 · glm-5.3-flash 5,1 · mimo-v2.6-flash 2,6 · 6-sol 2,0 · 6-astra 2,3 · 5.6-sol 1,7 | 18,4 s |
| 4× o mesmo modelo (`gpt-6-luna`) ao mesmo tempo | 1,6 · 3,2 · 2,7 · 2,9 | **3,2 s** |
| 4 modelos leves diferentes ao mesmo tempo | 2,0 · 2,0 · 6,2 (glm) · 3,7 | **6,2 s** (= o mais lento) |
| 3 modelos de decisão ao mesmo tempo | 2,5 · 2,4 · 1,9 | **2,5 s** |

**Onde está a fila:** com pedido curto, **nem por modelo nem global**: 4 chamadas simultâneas ao mesmo modelo levam o
tempo de uma. Todos os 7 modelos chamaram a ferramenta forçada e acertaram. A lentidão observada na execução de 13:14
(luna de 6 s para 26 s com 4 em paralelo) aparece com **pedidos longos**, o que aponta para limite de vazão (tokens) do
proxy ou do provedor, e não para fila por modelo; o teste curto não distingue as duas coisas para pedidos longos.
**Decisão:** ligar a paralelização com **pool de modelos por papel** (se o limite for por modelo, o pool espalha a carga;
se for por token, não piora nada). Pool leve: `gpt-6-luna`, `gpt-5.6-luna`, `glm-5.3-flash`, `mimo-v2.6-flash` (os 4
passaram); pool de decisão: `gpt-6-sol`, `gpt-6-astra`, `gpt-5.6-sol` (todos GPT). Crítico fora do pool: `k3.1-pro`,
outra família. Um modelo por laço de ferramentas inteiro (o reenvio e as voltas do laço vão ao mesmo modelo).
`POOL=0` no ambiente desliga. Paraleliza só o pensar: o Executor continua serial, com trava por título.

### Checklist de largada da execução final (9e)
- A8 corrigido (teste `test_executor_segura_so_o_alvo_enquanto_espera_aprovacao`).
- A6 corrigido: sem título ou pagamento ligado, o Relacionamento não aciona pessoa (evento `encaminhamento_adiado`)
  e pede ao fornecedor, pelo canal do cadastro, NF, parcela, valor e data (teste `test_relacionamento_sem_fato_ligado...`).
- Gravação F3d nativa: `agente/gravacao.py` (`tiques/`, `llm/`, `lotes/`, escrita atômica), formato em `runs/SCHEMA.md`.
- Prazo manda na fila de casos (9c.3): evento `caso_adiado` (teste `test_prazo_manda_na_fila_de_casos`).
- **Verificação de citações e auditoria por amostragem (2.3b, camadas 3 e 9): NÃO implementadas.** Larguei sem elas;
  a métrica `citacoes_barradas` sai 0 por não existir o verificador, não por não haver citação errada.
- Curador ativo às 18h.

## Modelos por papel (F3) e por quê
Medido no proxy em 07/11 (`scripts/probe_llm.py`, ferramenta forçada com `tool_choice="required"`):

| Papel | Modelo | Por quê |
|---|---|---|
| Chefe, Investigador (decisão) | `gpt-6-sol` | padrão da organização, objetivo; ~3 s com ferramenta forçada, ~8 s em texto livre |
| Leitor de E9, agentes de Compras e Tesouraria (leve) | `gpt-6-luna` | ~2,7 s com ferramenta forçada; tarefa curta e frequente (uma chamada por fornecedor, com cache) |
| Crítico | `k3.1-pro` | **outra família** (erra diferente do GPT); aceita só `temperature=1` e pensa antes de responder (~8 s, precisa de limite de saída maior) |

Os modelos ficam centralizados em `agente/llm.py` e podem ser trocados por variável de ambiente (`MODELO_DECISAO`, `MODELO_LEVE`, `MODELO_CRITICO`).

## Decisões de projeto registradas
- **Sem gabarito.** Nada no projeto compara resultados com uma lista de títulos "errados". As análises prévias
  (`investigacao.md`, `investigar.py`) ficam em `_local/` (fora do repositório e inacessíveis ao agente).
- **Os dados não trazem eventos depois de 02/11 08:00.** O relógio tem uma fila `eventos_futuros` alimentada pelo mundo
  (respostas de fornecedor, retornos do banco), pelas verificações que o agente agenda e, no futuro, pelo **simulado (F7)**.
- **Mundo do fornecedor (`env/mundo.py`)**: o fornecedor verdadeiro atende nos contatos anteriores a alterações não
  confirmadas e informa os dados da última versão confirmada; um canal alterado sem confirmação é atendido por quem o
  controla, que confirma o cadastro atual. Resposta em 2 a 6 tiques (semente fixa), só em horário comercial.
- **Cadeia (`env/cadeia.py`)**: elos tirados da política e do dicionário. Decisões de definição:
  - E4: "conferido com ressalva" não muda a resposta à pergunta do elo (recebido em quantidade compatível?): fica OK,
    com a ressalva na explicação. Quantidade recebida abaixo do faturado (NF ÷ preço unitário da OC) quebra o elo.
  - E6: troca bancária sem confirmação registrada QUEBRA em qualquer idade (`dados_bancarios.troca_de_conta`: a troca
    só vale com confirmação; os 30 dias são da regra de aprovação dupla). A idade e os pagamentos ted/pix já feitos
    depois da troca entram como **severidade** (alta < 30 dias; média; baixa se já houve pagamento sem reclamação).
    Confirmação só vale se o texto registrar ligação ao telefone que estava no cadastro **antes** das alterações recentes.
  - E9: o código só **separa** as comunicações ligadas ao título (canal do cadastro, mesmo domínio ou nome no texto, que
    citam esta NF ou nenhuma); quem lê é o LLM leve (F3). O resultado agregado usa E1 a E8, e E9 aparece como pendente.
  - Título agendado PROVADO só em código (E1-E8 ok, sem comunicação ligada) passa a `certificado` sozinho; se uma
    reavaliação posterior não provar, volta para `agendado` (degrau "código" da escada de decisão).
- **Orquestração (F3)**: os agentes se coordenam pelo quadro (tarefas, propostas, vereditos, travas). Só o Executor
  (código) escreve, e sempre pelo guarda. **Regra de custo do Crítico** (ele é o gargalo, de 15 a 50 s por chamada):
  sempre em ação irreversível ou cara (cancelar, reverter cadastro, corrigir lançamento, bloquear fornecedor,
  acionar pessoa), acima da alçada, em `bloquear_titulo` e quando a proposta deixa sair um título com elo QUEBRADO;
  dispensado (evento `critico_dispensado`, com motivo) só em ação reversível e barata quando o título tem elo E1-E8
  quebrado comprovado pelo código. Refutado duas vezes = inconclusivo, e a regra do prazo decide pelo custo
  esperado. O Chefe só é chamado quando há escolha (mais de um caso).
- **Pontualidade (mesa 2, código)**: nestes dados nenhum título em aberto vence antes de 14/11 fora da agenda; a
  mesa trabalha sobre o que o próprio time segura (devolve o legítimo ao primeiro lote que ainda dá tempo, sem
  antecipar). Não desfaz decisão da Integridade: só um fato novo (novo caso, recusa humana) muda.
- **Relacionamento (mesa 3, LLM leve)**: só responde se o remetente é o canal do cadastro; resposta usa só fatos
  juntados pelo código (ERP, retorno, agenda) e sai pelo canal do cadastro sem disparar resposta simulada
  (`aguardar_resposta=false`). "Recebimento em duplicidade" vai ao controller (recuperação).
- O SQL do agente roda em DuckDB com os dados `READ_ONLY`, `enable_external_access=false` e configuração travada.
- **Limitação conhecida:** o Python do agente usa lista de bloqueio + diretório temporário com cópias; não é um sandbox de
  verdade (próximo passo: contêiner ou processo sem acesso ao sistema de arquivos). `executar_sql` é a ferramenta principal.
- Decodificador de boleto validado contra 100% dos 3.722 boletos do DDA; destino calculado reproduz 100% da agenda.
- Códigos de rejeição do banco simulado iguais aos do `retorno_remessa.csv` (`AN`, `CC`, `PJ`).
