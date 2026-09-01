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

## Configuração

Crie sua configuração local a partir do exemplo:

```powershell
Copy-Item orchestrator.example.toml orchestrator.toml
```

O arquivo `orchestrator.toml` é local e não é versionado. Consulte a
[documentação de configuração](docs/configuration.md) para os campos,
variáveis de ambiente e regras de segurança.
