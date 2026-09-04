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
