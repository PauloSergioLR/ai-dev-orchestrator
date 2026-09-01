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
executa o Codex, valida localmente, cria commit, faz push, abre o Pull Request e
move o item para `AI Review`. Em seguida, aguarda a CI do HEAD exato do PR: todos
os checks obrigatórios precisam terminar com `SUCCESS`. A CI verde deixa o PR
pronto para uma futura etapa de review; esta versão não executa reviewer ou merge.

## Configuração

Crie sua configuração local a partir do exemplo:

```powershell
Copy-Item orchestrator.example.toml orchestrator.toml
```

O arquivo `orchestrator.toml` é local e não é versionado. Consulte a
[documentação de configuração](docs/configuration.md) para os campos,
variáveis de ambiente e regras de segurança.
