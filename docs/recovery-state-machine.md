# Máquina de estados de retomada segura

## Separação de responsabilidades

A retomada segura parte de três entradas distintas: a `RunRecord` persistida,
uma `RecoveryObservation` e uma `RecoveryPolicy`. A observação contém somente
fatos coletados no momento da retomada: worktree, HEADs, PRs, CI, merge e estado
do projeto. A política contém configuração e invariantes esperadas:
repositório, base do Pull Request e autorização de auto-merge.

O `RecoveryPlanner` é puro: recebe essas entradas e devolve uma
`RecoveryDecision`. Um futuro executor realizará no máximo a ação decidida,
registrará checkpoint e observará novamente. Assim, decisão e efeito externo
não se misturam.

## Fases e ações

As fases legadas `PUBLISHING` e `MERGING` permanecem por compatibilidade.
As novas fases explicitam cada efeito: `COMMIT_PENDING`, `PUSH_PENDING`,
`PR_PENDING`, `MERGE_PENDING` e `PROJECT_DONE_PENDING`.

| Fase | Fato exigido | Ação |
| --- | --- | --- |
| `PREPARING` | worktree ausente ou convergente | preparar ou avançar |
| `CODEX_RUNNING` | sessão Codex persistida | retomar Codex |
| `TESTING` | gates pendentes | executar gates locais |
| `COMMIT_PENDING` | alteração rastreada ou commit direto comprovado | criar ou registrar commit |
| `PUSH_PENDING` | remoto ausente, pai direto ou igual ao local | push ou registrar push |
| `PR_PENDING` | identidade completa do PR convergente | criar ou adotar PR |
| `WAITING_CI` | CI do HEAD exato | aguardar ou registrar sucesso |
| `GEMINI_REVIEWING` | review persistida no record | revisar ou avançar |
| `NEEDS_CHANGES` | sessão, review rejeitada e findings do mesmo HEAD | retomar correção |
| `MERGE_PENDING` | PR, CI, HEAD local e merge convergentes | merge ou registrar merge |
| `PROJECT_DONE_PENDING` | estado explícito do projeto | marcar Done ou completar |

O planner falha fechado: contradição, ambiguidade, SHA divergente ou estado
`UNKNOWN` produz `BLOCK`. Em especial, `UNKNOWN` nunca é interpretado como
false ou `NOT_DONE`.

## Provas de histórico e review

Para não aceitar divergências silenciosas, commit e push usam relações diretas:
um commit já existente só é aceito quando o pai imediato do HEAD local é o
checkpoint; um push pendente só é permitido para remoto ausente ou pai direto
do HEAD local. Relações ancestrais arbitrárias não bastam.

O resultado do Gemini só é recuperável depois que executor futuro persistir
veredito, SHA revisado e findings no store. Se o processo cair após a chamada
ao Gemini e antes desse checkpoint, a fase `GEMINI_REVIEWING` não tem review
persistida e planeja `REVIEW_HEAD` novamente. Findings estruturados futuros
serão associados ao SHA rejeitado para provar que a correção responde à revisão.

A cobertura futura terá três níveis: planner puro, executor com doubles dos
adapters e integração com persistência e serviços externos controlados.
