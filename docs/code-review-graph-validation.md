# Validação do Code Review Graph

Validação executada em 11/09/2026 no próprio AI Dev Orchestrator, com
`code-review-graph==2.3.8`.

## Build e atualização

- Primeiro uso: build completo de 102 arquivos, 1.729 nós e 14.494 arestas.
- Uso seguinte: caminho incremental executado com zero arquivos alterados.
- Status após o update: grafo íntegro com 101 arquivos, 1.669 nós e 14.150
  arestas. A diferença decorre do pós-processamento incremental do CRG.
- O integrador do orquestrador observou `action='update'` em 2,70 s, sem erro.

## Comparação de descoberta estrutural

A mesma tarefa foi usada nos dois caminhos: localizar os pontos de entrada do
pipeline que entregam trabalho ao Codex, ao recovery e ao AI Review.

Sem CRG, a busca ampla pelos termos `doctor|watch|Codex|Antigravity|prompt|mcp|MCP|execute|review`
em `src/` retornou 641 linhas, 77.679 caracteres e 32 arquivos candidatos.

Com CRG, três consultas direcionadas (`RunPipeline`, `RecoveryEffects` e
`build_prompt`, limite 3) retornaram 170 linhas, 6.995 caracteres e 5 arquivos
candidatos. Os arquivos centrais encontrados foram os mesmos usados na
implementação: `pipeline.py`, `recovery_effects.py` e `review.py`.

Isso reduziu em 91% o volume textual da etapa de descoberta e em 84% os arquivos
candidatos (32 para 5). Caracteres são usados como medida reprodutível porque
essa execução da CLI não expôs tokenização para consultas `search`; o runtime
registra tokens reais quando o provider os inclui no JSONL.

Para o diff amplo desta Issue, o painel `update --brief` do CRG estimou economia
zero (66.208 tokens de contexto completo e de grafo). Esse resultado é mantido
como evidência, sem substituir pela redução do melhor caso: consultas de
descoberta foram menores, mas o contexto de impacto deste diff tocou boa parte
do pipeline. Os 829 testes aprovados demonstram que habilitar a integração não
degradou o resultado funcional.

## Fail-open

Os testes cobrem executável ausente, versão incompatível, status corrompido,
falha do integrador dentro do pipeline e configuração MCP inválida. Em todos os
casos do CRG o resultado é warning/fallback, sem transformar a execução em
`FAILED`; a configuração inválida do Antigravity é preservada sem sobrescrita.
