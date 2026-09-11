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

Antes de habilitar `orch watch`, valide os caminhos reais dos providers por
intenção explícita:

```powershell
orch doctor --deep
orch doctor --deep --state
```

`--deep` pode consumir quota/tokens: cria apenas um diretório temporário e
sessão sintética, sem Issue, PR, commit, push, merge ou alteração de Project.
Ele reporta `LOCAL_CAPABILITY` e `LIVE_PROVIDER` separadamente. `--state`
adiciona a conferência `STATE_CONSISTENCY`, abrindo o SQLite apenas em leitura e
comparando execuções ativas com PRs e Project, sem corrigir divergências.

O Code Review Graph pode ser habilitado como integração local, versionada e
fail-open. O pipeline constrói/atualiza o grafo antes de Codex e Antigravity,
orienta os agentes a consultar sua estrutura antes de buscas amplas e mantém
grep/leitura direta como fallback. `orch doctor` valida pacote, versão, grafo e
MCP dos dois providers. Veja [configuração do Code Review Graph](docs/configuration.md#code-review-graph).

## Descoberta agnóstica do projeto

O orquestrador não pede linguagem, framework ou package manager. `orch init`
analisa o repositório em modo somente leitura e combina evidências do CI
versionado, scripts, `AGENTS.md`, `CONTRIBUTING.md`, README e documentação. O
resultado é um `ProjectContract` com comandos em `argv`, diretório de trabalho,
timeout, categoria, risco e evidência de origem.

```powershell
cd C:\caminho\do\projeto
orch init
orch doctor
orch watch
```

Comandos usados pelo CI oficial têm precedência sobre scripts e documentação.
Quando não há prova suficiente, `init` pede um único override estruturado e o
grava; ele nunca transforma texto livre em shell. Deploy, release, publicação,
migração remota e outras operações mutáveis são exibidos pelo `doctor`, mas não
entram nos gates automáticos.

O contrato recebe um fingerprint e é congelado no run. `resume` reutiliza o JSON
persistido, de modo que alterações posteriores da branch base não mudam
retrospectivamente os gates. Após cada execução Codex, o control plane roda por
conta própria todos os gates obrigatórios. Falha determinística pode retomar a
mesma sessão no mesmo worktree; falha de ambiente, timeout ou executable ausente
é classificada separadamente.

Repositórios com vários componentes são representados por múltiplos `cwd`.
Ferramentas customizadas funcionam da mesma forma, desde que documentação e
automação versionadas provem o comando. `ci.required_checks` continua aceito
como override; no modo automático, a CI é sempre conferida no HEAD exato do PR.
O SQLite padrão é namespaced por `owner/repository`, e `history`/`inspect`
mostram identidade, fingerprint e contadores separados de correções.

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

`orch watch` usa o mesmo `WorkService` e recovery. Por padrão,
`max_parallel_runs = 1` preserva o modo sequencial. Com valor maior, o supervisor
mantém até esse número de execuções independentes, em ordem determinística de
prioridade e Issue; cada uma conserva seu próprio checkpoint, sessão, worktree,
branch, PR e HEAD. Uma espera de quota ocupa seu slot lógico, mas não bloqueia os
outros slots. O lock local ao lado do SQLite e o claim transacional por Issue
recusam disputa entre supervisores. `Ctrl+C` encerra o supervisor sem apagar
checkpoints.

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

## Diagnóstico de uma execução

Use `orch inspect --issue N` durante um incidente para reunir, em uma consulta
local, fase, terminalidade, identidade da execução, worktree, sessão e modelos,
PR, HEADs, review/findings, quota, intervenção humana, Project, cleanup e os
dez eventos mais recentes do journal. Inconsistências persistidas evidentes são
sinalizadas ao fim da saída.

```powershell
orch inspect --issue 64
orch inspect --issue 64 --json
```

O comando abre o SQLite em modo somente leitura: não cria ou migra o banco e
nunca chama Git, providers, GitHub, CI ou Project. A saída JSON tem chaves
ordenadas e contrato estável para suporte e automação. Textos persistidos são
redigidos pela mesma política do store; prompts e logs completos de providers
não fazem parte da saída.

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
