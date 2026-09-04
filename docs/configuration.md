# Configuração

O AI Dev Orchestrator lê sua configuração local do arquivo `orchestrator.toml`
no diretório atual. Para começar, copie o exemplo versionado:

```powershell
Copy-Item orchestrator.example.toml orchestrator.toml
```

O arquivo real é ignorado pelo Git e não deve ser compartilhado como parte do
repositório.

## Formato e campos

```toml
[github]
owner = "seu-usuario"
repository = "seu-repositorio"
project_number = 1
ready_status = "Ready"
in_progress_status = "In Progress"
status_field_name = "Status"
ai_review_status = "AI Review"
pull_request_base = "main"

[workspace]
repository_path = "C:/caminho/para/repositorio"
worktrees_dir = "C:/caminho/para/worktrees"
base_ref = "main"
remote_name = "origin"

[execution]
max_attempts = 2
max_parallel_runs = 1
auto_merge = false
merge_timeout_seconds = 30

[state]
database_path = "C:/caminho/para/ai-dev-orchestrator/state/orchestrator.db"

[ci]
required_checks = ["test"]
poll_interval_seconds = 5
timeout_seconds = 900

[convergence]
poll_interval_seconds = 1
timeout_seconds = 30

[review]
max_correction_attempts = 3
```

Em `[github]`, `owner`, `repository` e `ready_status` devem ser textos não
vazios. `project_number` deve ser um inteiro maior que zero.

Em `[execution]`, `max_attempts` e `max_parallel_runs` devem ser inteiros
maiores que zero. `auto_merge` deve ser estritamente `true` ou `false` e
permanece `false` no exemplo. Quando habilitado, o merge commit só é executado
depois de review aprovado, CI verde e revalidação final do PR e do HEAD local.
`merge_timeout_seconds` define o limite positivo, em segundos, da chamada de
merge ao GitHub.

Campos fora dos grupos documentados ou com nomes incorretos são rejeitados, para que
erros de digitação não passem despercebidos.

Em `[workspace]`, `repository_path` é a raiz explícita e absoluta do repositório
de origem, `worktrees_dir` é a raiz explícita e absoluta dos worktrees e
`base_ref` é a referência Git usada sem `fetch` ou `pull` automático. Paths
relativos são rejeitados para que a execução não dependa do diretório atual. Em
`[github]`, `in_progress_status` tem como padrão `In Progress`.

Em `[ci]`, `required_checks` define os checks que devem existir e terminar com
`SUCCESS` para liberar o fluxo. O padrão é `["test"]`; a lista não pode ser
vazia. `poll_interval_seconds` (padrão `5`) e `timeout_seconds` (padrão `900`)
devem ser positivos. Checks fora da lista não bloqueiam o gate.

Em `[convergence]`, `poll_interval_seconds` (padrão `1`) controla o intervalo
entre leituras do GitHub após uma mutação remota, e `timeout_seconds` (padrão
`30`) limita a espera total. O polling repete somente consultas e nunca repete
push, criação de Pull Request, merge ou alteração de Project.

Em `[review]`, `max_correction_attempts` define quantas correções após um
`REJECTED` podem ocorrer na mesma sessão Codex. O padrão é `3` e o valor deve
ser um inteiro positivo.

Em `[state]`, `database_path` é o caminho absoluto do banco SQLite local. O
diretório pai é criado quando necessário. Se omitido, o caminho determinístico
é `~/.ai-dev-orchestrator/orchestrator.db`, fora do repositório e dos worktrees.
O banco contém apenas checkpoints resumidos; prompts, diffs e credenciais não
são persistidos.

## Variáveis de ambiente

Variáveis com prefixo `ORCH_` podem sobrescrever o arquivo. Para campos
aninhados, use dois sublinhados entre o grupo e o campo:

```text
ORCH_GITHUB__OWNER
ORCH_GITHUB__REPOSITORY
ORCH_GITHUB__PROJECT_NUMBER
ORCH_GITHUB__READY_STATUS
ORCH_GITHUB__IN_PROGRESS_STATUS
ORCH_GITHUB__AI_REVIEW_STATUS
ORCH_GITHUB__PULL_REQUEST_BASE
ORCH_WORKSPACE__REPOSITORY_PATH
ORCH_WORKSPACE__WORKTREES_DIR
ORCH_WORKSPACE__BASE_REF
ORCH_WORKSPACE__REMOTE_NAME
ORCH_EXECUTION__MAX_ATTEMPTS
ORCH_EXECUTION__MAX_PARALLEL_RUNS
ORCH_EXECUTION__AUTO_MERGE
ORCH_EXECUTION__MERGE_TIMEOUT_SECONDS
ORCH_CI__REQUIRED_CHECKS
ORCH_CI__POLL_INTERVAL_SECONDS
ORCH_CI__TIMEOUT_SECONDS
ORCH_CONVERGENCE__POLL_INTERVAL_SECONDS
ORCH_CONVERGENCE__TIMEOUT_SECONDS
ORCH_REVIEW__MAX_CORRECTION_ATTEMPTS
ORCH_STATE__DATABASE_PATH
```

Por exemplo, `ORCH_EXECUTION__MAX_ATTEMPTS=3` substitui apenas esse valor. A
precedência é: variáveis de ambiente > arquivo TOML. Não há suporte a `.env`.

## Erros e segurança

Arquivo ausente, TOML inválido e valores inválidos geram um erro de configuração
claro, com a causa original preservada para diagnóstico. Não armazene tokens,
senhas ou qualquer credencial neste arquivo. Autenticação futura deve usar as
ferramentas autenticadas ou um mecanismo de segredos específico.
