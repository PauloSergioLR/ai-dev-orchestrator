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

Campos fora desses dois grupos ou com nomes incorretos são rejeitados, para que
erros de digitação não passem despercebidos.

## Variáveis de ambiente

Variáveis com prefixo `ORCH_` podem sobrescrever o arquivo. Para campos
aninhados, use dois sublinhados entre o grupo e o campo:

```text
ORCH_GITHUB__OWNER
ORCH_GITHUB__REPOSITORY
ORCH_GITHUB__PROJECT_NUMBER
ORCH_GITHUB__READY_STATUS
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
