# Máquina de estados de retomada segura

## Separação de responsabilidades

A retomada segura parte de três entradas distintas: a `RunRecord` persistida,
uma `RecoveryObservation` e uma `RecoveryPolicy`. A observação contém somente
fatos coletados no momento da retomada: worktree, HEADs, PRs, CI, merge e estado
do projeto. A política contém configuração e invariantes esperadas:
repositório, base do Pull Request e autorização de auto-merge.

O `RecoveryPlanner` é puro: recebe essas entradas e devolve uma
`RecoveryDecision`. O `RecoveryExecutor` realiza no máximo a ação decidida,
registra checkpoint e o serviço observa novamente. Assim, decisão e efeito
externo não se misturam.

O `RecoveryExecutor` aplica apenas uma decisão recebida e delega I/O a
`RecoveryEffects`, conectado aos adapters existentes. Cada
efeito bem-sucedido recebe checkpoint no mesmo execution_id. Uma queda entre o
efeito e o checkpoint será reconciliada pela próxima observação e planejamento.
Antes de qualquer efeito, o executor recarrega o record, confere que ele não
mudou desde o planejamento e valida que a ação é compatível com a fase atual.

Na integração final, o observer coleta fatos externos somente por leituras e o
serviço `resume` repete observar, planejar e aplicar uma única ação até haver
checkpoint terminal ou bloqueio seguro. Novas execuções usam as fases explícitas
de commit, push, PR, merge e Project Done; `PUBLISHING` e `MERGING` permanecem
somente para compatibilidade histórica.

## Fases e ações

As fases legadas `PUBLISHING` e `MERGING` permanecem por compatibilidade. Na
retomada, `PUBLISHING` só migra para checkpoints granulares quando a relação
direta do commit e o HEAD remoto permitem provar o próximo passo; `MERGING`
reutiliza as mesmas provas de PR, CI, review e merge de `MERGE_PENDING`.
As novas fases explicitam cada efeito: `COMMIT_PENDING`, `PUSH_PENDING`,
`PR_PENDING`, `MERGE_PENDING` e `PROJECT_DONE_PENDING`.

| Fase | Fato exigido | Ação |
| --- | --- | --- |
| `PREPARING` | worktree ausente ou convergente | preparar ou avançar |
| `CODEX_RUNNING` | sessão Codex persistida | retomar Codex |
| `TESTING` | gates pendentes | executar gates locais |
| `COMMIT_PENDING` | qualquer alteração do worktree ou commit direto comprovado | criar ou registrar commit |
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

Toda fase que depende de worktree exige a identidade persistida completa:
branch, caminho do worktree e ref base. As fases publicadas também exigem HEAD
local idêntico ao checkpoint. A partir de `WAITING_CI`, o número e a URL do PR
devem estar persistidos juntos, e a observação deve conter exatamente um PR com
repositório, base, branch e HEAD convergentes. Uma identidade parcial bloqueia.

## Provas de histórico e review

Para não aceitar divergências silenciosas, commit e push usam relações diretas:
um commit já existente só é aceito quando o pai imediato do HEAD local é o
checkpoint; um push pendente só é permitido para remoto ausente ou SHA igual ao
pai imediato do HEAD local. Relações ancestrais arbitrárias não bastam. O fato
de o worktree estar dirty inclui arquivos novos: a validação de conteúdo cabe
aos gates do executor, não ao planner.

`PROJECT_DONE_PENDING` somente pode marcar o projeto depois de merge comprovado
no record: item de projeto, SHA revisado, SHA merged igual ao revisado e commit
de merge precisam estar presentes. Isso impede marcar Done após uma observação
incompleta ou após merge não persistido.

O resultado do Gemini só é recuperável depois que o executor persistir
veredito, SHA revisado e findings no store. Se o processo cair após a chamada
ao Gemini e antes desse checkpoint, a fase `GEMINI_REVIEWING` não tem review
persistida e planeja `REVIEW_HEAD` novamente. Findings estruturados são
associados ao SHA rejeitado para provar que a correção responde à revisão.
Os findings vivem no mesmo SQLite da execução e a persistência do review é
atômica com o evento de journal. A tentativa de correção é incrementada antes
da chamada ao Codex, preservando auditoria mesmo se o provider falhar.

A cobertura tem três níveis: planner puro, executor com doubles dos adapters e
integração controlada do observer, effects, serviço de retomada e CLI.
