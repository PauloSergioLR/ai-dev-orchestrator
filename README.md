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

O run sincroniza a base remota, persiste seu SHA concreto, cria o worktree nesse
mesmo commit e só então resolve e congela o contrato. `resume` reutiliza o JSON
persistido; alterações posteriores da base não mudam gates retrospectivamente.
Workflows que não atendem Pull Requests (`workflow_dispatch`, `schedule`,
`push`, `release`) são auditados, mas nunca viram gates locais. Se a Issue altera
build/CI, o plano baseline continua sendo o único executado e o contrato novo é
registrado como candidato para revisão, sem executar comandos recém-criados.
Falha determinística pode retomar a mesma sessão; discovery, ambiente, timeout
ou executable ausente preservam o budget de correção.

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
branch, PR e HEAD. Esperas de quota, provider bloqueado e `HUMAN_REQUIRED`
ocupam seu slot lógico, mas não bloqueiam os outros slots. O lock local ao lado do SQLite e o claim transacional por Issue
recusam disputa entre supervisores. `Ctrl+C` encerra o supervisor sem apagar
checkpoints.

## Execução manual de Issue

```powershell
orch run --issue <numero>
# override opcional: --branch <nome-da-branch>
```

Sem override, a branch é derivada do título pela mesma política do fluxo
autônomo. O comando lê a Issue explícita, valida seu item em `Ready`, prepara um worktree,
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
expor logs dos providers. `orch cleanup --issue N` aceita execução `COMPLETED`
ou `SUPERSEDED`: remove worktree Git comprovadamente limpo ou diretório órfão
vazio sob `worktrees_dir`. Worktree dirty, diretório com conteúdo desconhecido e
caminho fora da raiz são preservados. Para desbloquear um órfão não vazio sem
apagá-lo, `--quarantine-orphan` o move, após confirmação, para
`.orchestrator-quarantine`. A política pode permitir também branches.
Por padrão, o cleanup automático e a remoção de branches permanecem desabilitados.

Crie sua configuração local a partir do exemplo:

```powershell
orch init
```

O arquivo `orchestrator.toml` é local e não é versionado. Consulte a
[documentação de configuração](docs/configuration.md) para os campos,
variáveis de ambiente e regras de segurança.

## Supersessão explícita de execução antiga

Quando um Pull Request antigo foi fechado sem merge, ou um run pré-PR precisa
ser abandonado sem efeitos remotos, não apague o SQLite nem crie outra execução
manualmente. Primeiro observe e encerre
o run antigo de forma auditável:

```powershell
orch supersede --issue N --reason "PR antigo encerrado; Issue será refeita sobre main atual"
```

O comando exibe branch, PR e HEADs persistido/remoto e pede confirmação. Para
automação deliberada, use `--yes`. Com PR, ele exige identidade única, fechada
sem merge; pré-PR exige ausência comprovada de PR, merge e branch remota. PR
aberto exige recovery normal, e PR mergeado é reconciliado automaticamente.
Nenhum arquivo, sessão, PR, Project ou registro histórico é removido ou
alterado além da fase local `SUPERSEDED`. Depois disso, a Issue pode receber uma
nova execução quando voltar a `Ready`.

Se um contrato histórico estiver incorreto e o run possuir `base_sha`, use
`orch recover-contract --issue N`. O comando materializa um snapshot detached
temporário da base imutável, compara fingerprints e pede confirmação antes de
adotar o baseline reconstruído na mesma execução. O worktree atual, inclusive
dirty, não é usado como fonte e nenhum comando candidato é executado.

## Retomada após falhas de runtime

Falhas de rede e timeout preservam a execução ativa e têm até três retries com
backoff persistido. Quota aguarda reset confiável; autenticação, modelo e erros
de protocolo exigem intervenção. Nenhum desses casos cria outra sessão ou PR.

Se o Codex terminar sem diff, o orquestrador faz no máximo
`execution.max_no_changes_attempts` retomadas na mesma sessão. Persistindo a
ausência de mudanças, o run vai para `HUMAN_REQUIRED` com `NO_CHANGES`; o commit
vazio não chega à camada Git como erro cru. Chamadas longas de Codex e Gemini
emitem heartbeat a cada cinco minutos, e gates anunciam início e resultado, sem
imprimir prompts ou saída bruta dos providers.

Use orch state --issue N para o diagnóstico e orch resume --issue N para
retomar. Depois de corrigir um bloqueio, --retry-provider solicita uma tentativa
explícita. --recover-failed reconcilia registros históricos de falha transitória
somente quando a identidade local/remota pode ser comprovada. Consulte as
[regras de recuperação e encoding](docs/recovery-state-machine.md#falhas-de-provider-e-retomada).

Para um bloqueio ocorrido especificamente durante commit, push ou criação/adoção
do Pull Request, depois de inspecionar a execução use:

```powershell
orch resume --issue N --resume-publication
```

A opção aceita somente `INTERNAL_ERROR` ou `REMOTE_AMBIGUOUS` nas fases de
publicação recuperáveis. Ela observa Git e PR e exige uma decisão segura do
`RecoveryPlanner` antes de restaurar a fase; identidade incompleta ou qualquer
contradição permanece em `HUMAN_REQUIRED`. A mesma execução, sessão Codex,
branch e worktree são preservadas, e o pipeline segue normalmente após a
reconciliação.
