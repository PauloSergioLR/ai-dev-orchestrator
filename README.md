# ai-dev-orchestrator

Orquestrador local-first de desenvolvimento com IA para GitHub, Codex CLI e Antigravity CLI.

## Diagnóstico do ambiente

Execute o diagnóstico dos pré-requisitos locais com:

```powershell
orch doctor
```

O comando apenas informa o estado de Python, CLIs, repositório Git e configuração.
O Antigravity CLI é o executável local usado para a revisão com Gemini.
Ele não corrige problemas, instala ferramentas, altera autenticação ou envia prompts para IAs.

## Uso diário

```powershell
orch work
```

Ou, sem instalar o entry point:

```powershell
uv run orch work
```

O comando retoma primeiro uma execução interrompida. Quando não há uma,
seleciona deterministicamente a próxima Issue aberta e elegível em `Ready`,
sincroniza a base remota, cria uma branch descritiva e conduz o pipeline completo:
Codex, gates locais, commit, push, Pull Request, CI, review Gemini, correções,
auto-merge quando habilitado e atualização do Project para `Done`.

## Execução manual de Issue

```powershell
orch run --issue <numero> --branch <nome-da-branch>
```

O comando lê a Issue explícita, valida seu item em `Ready`, prepara um worktree,
executa o Codex, valida localmente, cria commit, faz push, abre o Pull Request e
move o item para `AI Review`. Em seguida, aguarda a CI do HEAD exato do PR, executa
o review Gemini e aplica o ciclo de correção na mesma sessão Codex. Com aprovação,
faz merge automaticamente quando configurado e conclui o item no Project.

Os comandos `orch resume --issue N`, `orch state --issue N` e `orch doctor`
continuam disponíveis para operação e diagnóstico explícitos.

## Configuração

Crie sua configuração local a partir do exemplo:

```powershell
Copy-Item orchestrator.example.toml orchestrator.toml
```

O arquivo `orchestrator.toml` é local e não é versionado. Consulte a
[documentação de configuração](docs/configuration.md) para os campos,
variáveis de ambiente e regras de segurança.
