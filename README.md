# ai-dev-orchestrator

Orquestrador local-first de desenvolvimento com IA para GitHub, Codex CLI e Antigravity CLI.

## Diagnóstico do ambiente

Execute o diagnóstico dos pré-requisitos locais com:

```powershell
orch doctor
```

O comando apenas informa o estado de Python, CLIs, repositório Git e configuração.
O Antigravity CLI é o executável local usado para a futura revisão com Gemini.
Ele não corrige problemas, instala ferramentas, altera autenticação ou envia prompts para IAs.

## Execução experimental de Issue

```powershell
orch run --issue <numero> --branch <nome-da-branch>
```

O comando lê a Issue explícita, valida seu item em `Ready`, prepara um worktree,
move somente o Status para `In Progress` e executa o Codex. Nesta etapa, não
cria commit, push, Pull Request, review ou merge.

## Configuração

Crie sua configuração local a partir do exemplo:

```powershell
Copy-Item orchestrator.example.toml orchestrator.toml
```

O arquivo `orchestrator.toml` é local e não é versionado. Consulte a
[documentação de configuração](docs/configuration.md) para os campos,
variáveis de ambiente e regras de segurança.
