# Fluxo

O final implementado do fluxo é:

```text
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
CI verde: pronto para futura etapa de review
```

Ainda não há revisão Gemini, feedback, merge, limpeza ou automação da próxima
Issue. Se a CI falhar, expirar ou o HEAD mudar, PR, branch, commit e worktree são
preservados e o Status continua em `AI Review`.
