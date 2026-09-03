# Máquina de estados de retomada segura

## Motivo da arquitetura

Uma tentativa anterior de retomada misturava a decisão sobre o estado com a execução de comandos e chamadas externas. Esse desenho não oferece uma fonte determinística para explicar o que já ocorreu depois de uma interrupção. A arquitetura atual parte de uma `RunRecord` persistida e de uma observação externa normalizada, sem depender daquela tentativa.

O `RecoveryPlanner` é puro: recebe fatos e devolve uma `RecoveryDecision`. Um futuro `RecoveryExecutor` executará somente a ação escolhida, registrará um checkpoint e fará uma nova observação antes de planejar de novo.

## Fases novas

As fases legadas `PUBLISHING` e `MERGING` permanecem por compatibilidade. As novas fases explicitam cada efeito: `COMMIT_PENDING`, `PUSH_PENDING`, `PR_PENDING`, `MERGE_PENDING` e `PROJECT_DONE_PENDING`.

| Fase | Observação determinante | Ação |
| --- | --- | --- |
| `PREPARING` | worktree ausente/convergente | preparar/avançar fase |
| `CODEX_RUNNING` | sessão persistida | retomar Codex |
| `TESTING` | gates pendentes | executar gates locais |
| `COMMIT_PENDING` | alterações ou commit já observado | criar/registrar commit |
| `PUSH_PENDING` | remoto anterior ou igual ao local | push/registrar push |
| `PR_PENDING` | nenhum PR ou um PR convergente | criar/adotar PR |
| `WAITING_CI` | CI pendente ou verde para HEAD exato | aguardar/registrar CI |
| `GEMINI_REVIEWING` | review ausente, rejeitada ou aprovada | revisar/avançar fase |
| `NEEDS_CHANGES` | sessão e findings do mesmo HEAD | retomar correção |
| `MERGE_PENDING` | merge existente ou PR aprovado e verde | registrar/executar merge |
| `PROJECT_DONE_PENDING` | item Done ou não Done | completar/marcar Done |

Cada decisão representa no máximo uma mutação externa. Checkpoints que só avançam a fase também são explícitos. Qualquer ambiguidade, contradição ou SHA diferente bloqueia o fluxo: não há repetição cega de efeitos externos.

No futuro, findings estruturados serão persistidos com o SHA revisado, permitindo provar que uma correção responde exatamente à rejeição recebida. A cobertura será dividida em três níveis: testes unitários do planner puro, testes do executor com doubles de adapters e testes de integração com persistência e serviços externos controlados.
