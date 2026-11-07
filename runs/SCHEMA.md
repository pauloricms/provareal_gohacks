# Formato de uma execução gravada (`runs/<ts>/`), seção F3d

O Streamlit só LÊ esta pasta: não chama LLM, não roda a cadeia, não consulta os CSVs. Código: `agente/gravacao.py`.

| Arquivo | O que tem |
|---|---|
| `eventos.jsonl` | Uma linha por acontecimento: `ts_real`, `tique` (= **hora simulada**, `AAAA-MM-DD HH:MM:SS`), `ator`, `tipo` e campos do tipo. Número do tique = horas desde 02/11 08:00. |
| `tiques/<NNN>.json` | Fotografia ao FIM do tique NNN (escrita atômica: `.tmp` + rename). |
| `llm/<NNN>.jsonl` | Cada chamada de LLM do tique: `agente`, `papel`, `modelo`, `latencia_s`, `tokens`, `finish`, `contexto`, `mensagens` (cada uma resumida acima de 4 mil caracteres) e `resposta` (`content`, `tool_calls[{nome, argumentos}]`). |
| `lotes/<lote>.json` | No fechamento: `saiu` (linhas de `lote_saida`, com o pós-lote), `retidos` (estado, elos quebrados, caso, motivo), totais. Reescrito a cada tique enquanto o pós-lote anda. |
| `estado.sqlite` | Estado vivo final (títulos, casos, quadro, detectores...). |
| `resumo.md` / `resumo.json` | Números do pitch (`metricas/resumo.py`). |
| `reconstrucao.json` | Só em execução reconstruída (`scripts/reconstruir_tiques.py`): lista o que não estava gravado. |

## Fotografia (`tiques/<NNN>.json`)
- `tique`, `hora_simulada`, `proximo_lote` {`lote_remessa`, `banco`, `fecha_em`, `faltam_h`}
- `titulos_proximo_lote[]`: `titulo_id`, `fornecedor_id`, `valor`, `vencimento`, `forma`, `estado` (no tique, pelo
  histórico de transições), `resultado` (PROVADO/DUVIDOSO/QUEBRADO), `quebrados`, `inconclusivos`,
  `elos[]` {`elo`, `nome`, `status`, `explicacao`, `evidencias`}, `caso_id`
- `casos[]`: `id`, `mesa`, `status`, `fornecedor_id`, `titulos`, `valor_em_jogo`, `prazo`, `origem`, `p_problema`, `desfecho`
- `cards[]`, `detectores[]`, `licoes[]`, `saude_cadeia[]`
- `metricas`: R$ retido por motivo, devolvidos pela Pontualidade, cards e decisões levadas a pessoas, chamadas de LLM
  por papel e por modelo, casos abertos/adiados, propostas, vereditos, refutações e dispensas do Crítico, citações
  barradas, ações executadas, pedidos de aprovação, respostas a fornecedor, tiques sem LLM,
  % provado só em código e % sem pessoa no próximo lote
- `faltaram[]`: vazio na gravação nativa; na reconstrução, o que não dá para saber pelo que foi gravado.

## Tipos de evento (F3d → nome no `eventos.jsonl`)
`tique_inicio` → `tique` (ator relogio) · `tique_fim` → `tique_resumo` · `evento_mundo` → `evento:<tipo>` ·
`triagem` → `caso_aberto` · `tarefa` → `plano` · `proposta` · `veredito` · `critico_refutou` · `critico_dispensado` ·
`caso_adiado` · `acao` → `ferramenta` com `tipo_ferramenta = execucao` (resultado ou recusa do guarda no `resultado`) ·
`card_humano` → `card_escalada` · `detector_criado` → `ferramenta` `salvar_detector` · `licao` · `lote_fechado` ·
`pos_lote` → `retorno_processado` · `llm_chamada` · `leitura_e9` · `resposta_fornecedor` · `encaminhamento_adiado` ·
`curadoria_pos_execucao` (pasta irmã `<ts>_curadoria/`).
