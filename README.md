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
Doctor e runtime validam as mesmas flags da CLI configurada em `review.executable`
(padrão `agy`, confirmado na CLI oficial). No Windows, um PATH desatualizado
pode ser contornado com o caminho absoluto em `ORCH_REVIEW__EXECUTABLE`.
O diagnóstico local não garante autenticação, quota nem resposta do modelo;
cada review exige um `structured_output` válido. Veja [revisão Gemini](docs/review.md).
O reviewer analisa o dossier já coletado sem executar comandos. Se a CLI
reportar `denied_actions`, a revisão fica bloqueada e recuperável; não amplie
permissões de shell para contornar esse diagnóstico.

## Configuração inicial e uso diário

```powershell
orch init
orch work
orch watch
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

`orch init` descobre o repositório, o remote, branches e convenções documentadas,
confirma somente escolhas ambíguas e grava atomicamente o perfil humano em
`orchestrator.toml`. `AGENTS.md` melhora as sugestões, mas não é obrigatório e
seu texto nunca é executado como comando.

`orch watch` usa o mesmo `WorkService` e recovery em modo sequencial. Quando um
provider informa de forma confiável a próxima tentativa, aguarda sem busy-loop e
retoma a mesma execução, sessão, worktree, branch, PR e HEAD. Sem esse horário (ou
uma política local explícita), para de modo seguro e pede intervenção. `Ctrl+C`
encerra o supervisor sem apagar checkpoints.

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

## Histórico e cleanup

`orch history` (ou `orch history --issue N`) mostra o histórico local, duração,
esperas de CI/quota, revisões, correções, modelos, merge, Project e cleanup sem
expor logs dos providers. `orch cleanup --issue N` remove somente o worktree
limpo de uma execução `COMPLETED`; a política pode permitir também branches.
Por padrão, o cleanup automático e a remoção de branches permanecem desabilitados.

Crie sua configuração local a partir do exemplo:

```powershell
orch init
```

O arquivo `orchestrator.toml` é local e não é versionado. Consulte a
[documentação de configuração](docs/configuration.md) para os campos,
variáveis de ambiente e regras de segurança.

## Supersessão explícita de execução antiga

Quando um Pull Request antigo foi fechado sem merge e a Issue será refeita, não
apague o SQLite nem crie outra execução manualmente. Primeiro observe e encerre
o run antigo de forma auditável:

```powershell
orch supersede --issue N --reason "PR antigo encerrado; Issue será refeita sobre main atual"
```

O comando exibe branch, PR e HEADs persistido/remoto e pede confirmação. Para
automação deliberada, use `--yes`. Ele só aceita o PR persistido, único, fechado
sem merge; PR aberto exige recovery normal, e PR mergeado exige reconciliação de
merge. Nenhum arquivo, sessão, PR, Project ou registro histórico é removido ou
alterado além da fase local `SUPERSEDED`. Depois disso, a Issue pode receber uma
nova execução quando voltar a `Ready`.

## Retomada após falhas de runtime

Falhas de rede e timeout preservam a execução ativa e têm até três retries com
backoff persistido. Quota aguarda reset confiável; autenticação, modelo e erros
de protocolo exigem intervenção. Nenhum desses casos cria outra sessão ou PR.

Use orch state --issue N para o diagnóstico e orch resume --issue N para
retomar. Depois de corrigir um bloqueio, --retry-provider solicita uma tentativa
explícita. --recover-failed reconcilia registros históricos de falha transitória
somente quando a identidade local/remota pode ser comprovada. Consulte as
[regras de recuperação e encoding](docs/recovery-state-machine.md#falhas-de-provider-e-retomada).
