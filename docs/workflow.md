# Fluxo

O fluxo principal é iniciado por `orch work` e está implementado assim:

```text
retoma execução ativa ou seleciona a próxima Issue Ready
    ↓
sincroniza base remota e cria branch/worktree
    ↓
Codex
    ↓
gates locais
    ↓
commit
    ↓
push
    ↓
Pull Request
    ↓
AI Review
    ↓
aguarda CI do HEAD exato
    ↓
Gemini
    ↓
REJECTED: findings → mesma sessão Codex → gates → push → CI → Gemini
    ↓
APPROVED: auto-merge quando habilitado
    ↓
Project Done
```

Se houver divergência, colisão de branch ou falha de sincronização, o comando
falha fechado. PR, branch, commit, worktree, sessão Codex e checkpoints já
existentes são preservados para retomada segura.

Quando não há nenhuma Issue em `Ready`, o próximo item aberto elegível em
`Backlog` é promovido para `Ready` antes de iniciar o pipeline, usando o mesmo
critério determinístico de prioridade e número. O Status `AI Review` é aplicado
na publicação e na recuperação de PRs. Se a CI falhar para o HEAD persistido,
`orch watch` retoma a mesma sessão Codex, worktree e PR para corrigir a causa;
o limite configurado de correções continua valendo.

## Concorrência controlada do supervisor

`execution.max_parallel_runs` é `1` por padrão e conserva o fluxo sequencial.
Quando configurado acima de `1`, apenas `orch watch` preenche as vagas, pela ordem
determinística de prioridade e número da Issue. Cada vaga é uma execução durável
separada: não há compartilhamento de worktree, branch, sessão Codex, PR ou HEAD.

O arquivo de lock associado ao banco impede dois supervisores locais de operar o
mesmo estado. Além disso, o índice único de execução ativa por Issue funciona como
claim transacional: em caso de disputa, a segunda tentativa falha fechada antes de
criar worktree. Esperas de provider preservam a identidade e o slot lógico da run,
mas o scheduler continua reconciliando e iniciando as demais vagas disponíveis.
