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
```

Em `[github]`, `owner`, `repository` e `ready_status` devem ser textos não
vazios. `project_number` deve ser um inteiro maior que zero.

Em `[execution]`, `max_attempts` e `max_parallel_runs` devem ser inteiros
maiores que zero. `auto_merge` deve ser estritamente `true` ou `false`. A
configuração atual apenas registra esse valor; ela não executa merge automático.

Campos fora desses três grupos ou com nomes incorretos são rejeitados, para que
erros de digitação não passem despercebidos.

Em `[workspace]`, `repository_path` é a raiz explícita e absoluta do repositório
de origem, `worktrees_dir` é a raiz explícita e absoluta dos worktrees e
`base_ref` é a referência Git usada sem `fetch` ou `pull` automático. Paths
relativos são rejeitados para que a execução não dependa do diretório atual. Em
`[github]`, `in_progress_status` tem como padrão `In Progress`.

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
```

Por exemplo, `ORCH_EXECUTION__MAX_ATTEMPTS=3` substitui apenas esse valor. A
precedência é: variáveis de ambiente > arquivo TOML. Não há suporte a `.env`.

## Erros e segurança

Arquivo ausente, TOML inválido e valores inválidos geram um erro de configuração
claro, com a causa original preservada para diagnóstico. Não armazene tokens,
senhas ou qualquer credencial neste arquivo. Autenticação futura deve usar as
ferramentas autenticadas ou um mecanismo de segredos específico.
