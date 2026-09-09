# Configuração

O AI Dev Orchestrator lê sua configuração local do arquivo `orchestrator.toml`
no diretório atual. Para começar ou reconfigurar, execute o assistente:

```powershell
orch init
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
done_status = "Done"
pull_request_target = "main"
protected_branches = ["main"]

[workspace]
repository_path = "C:/caminho/para/repositorio"
worktrees_dir = "C:/caminho/para/worktrees"
base_branch = "main"
remote_name = "origin"

[providers]
codex_model = "default"
gemini_model = "default"

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
executable = "agy"
max_correction_attempts = 3

[supervisor]
poll_interval_seconds = 60
max_sleep_seconds = 300

[cleanup]
auto_cleanup = false
remove_local_branch = false
remove_remote_branch = false
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
`base_branch` é a branch da qual nascem os trabalhos. `pull_request_target`
é o destino dos Pull Requests e `protected_branches` impede automação direta
nesses nomes. Os três papéis são independentes. Paths
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

Em `[review]`, `executable` define o nome no PATH ou caminho absoluto da CLI
Antigravity; o padrão oficial é `agy`. `ORCH_REVIEW__EXECUTABLE` sobrescreve
esse campo. Doctor, pipeline e recovery usam a mesma configuração; não há
troca automática para Gemini CLI ou para o aplicativo gráfico Antigravity.
Consulte o [contrato validado do reviewer](review.md).
`max_correction_attempts` define quantas correções após um
`REJECTED` podem ocorrer na mesma sessão Codex. O padrão é `3` e o valor deve
ser um inteiro positivo.

Em `[state]`, `database_path` é o caminho absoluto do banco SQLite local. O
diretório pai é criado quando necessário. Se omitido, o caminho determinístico
é `~/.ai-dev-orchestrator/orchestrator.db`, fora do repositório e dos worktrees.
O banco contém apenas checkpoints resumidos; prompts, diffs e credenciais não
são persistidos.

Em `[providers]`, `default` (ou `auto`) preserva a seleção feita pela CLI.
Identificadores explícitos são encaminhados ao início e à retomada. Os modelos
usados ficam registrados no run e não podem ser trocados silenciosamente.

`orch watch` usa `[supervisor]` para polling conservador. Nenhum horário de reset
é inferido. `retry_without_reset_seconds` é opcional e somente deve ser definido
quando o projeto possuir uma política segura de retry sem horário do provider.

Em `[cleanup]`, todas as opções começam desabilitadas. Mesmo quando habilitado,
o cleanup só atua em `COMPLETED`, recusa worktree sujo, base/destino/branches
protegidas e só remove referência remota após a confirmação persistida do merge
do HEAD esperado. Uma falha é registrada como pendência e não muda a conclusão.

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
ORCH_GITHUB__DONE_STATUS
ORCH_GITHUB__PULL_REQUEST_TARGET
ORCH_GITHUB__PROTECTED_BRANCHES
ORCH_WORKSPACE__REPOSITORY_PATH
ORCH_WORKSPACE__WORKTREES_DIR
ORCH_WORKSPACE__BASE_BRANCH
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
ORCH_PROVIDERS__CODEX_MODEL
ORCH_PROVIDERS__GEMINI_MODEL
ORCH_SUPERVISOR__POLL_INTERVAL_SECONDS
ORCH_SUPERVISOR__MAX_SLEEP_SECONDS
ORCH_SUPERVISOR__RETRY_WITHOUT_RESET_SECONDS
```

Por exemplo, `ORCH_EXECUTION__MAX_ATTEMPTS=3` substitui apenas esse valor. A
precedência é: variáveis de ambiente > arquivo TOML. Não há suporte a `.env`.

## Erros e segurança

Arquivo ausente, TOML inválido e valores inválidos geram um erro de configuração
claro, com a causa original preservada para diagnóstico. Não armazene tokens,
senhas ou qualquer credencial neste arquivo. Autenticação futura deve usar as
ferramentas autenticadas ou um mecanismo de segredos específico.

## Retentativas de runtime

Falhas de rede e timeout usam até três retries, com intervalos persistidos de
30, 60 e 120 segundos. Esse contador é independente de max_correction_attempts:
repetir transporte não consome outra correção. Não há configuração que habilite
retry ilimitado. retry_without_reset_seconds aplica-se somente às esperas de
quota/rate-limit sem reset informado; não contorna bloqueios de autenticação,
modelo, protocolo ou limite de retries transitórios.

Após tratar a causa, orch resume --issue N --retry-provider solicita retry
explícito. Para FAILED histórico com evidência transitória, use --recover-failed.
As opções mantêm as verificações de identidade e convergência descritas na
[máquina de estados](recovery-state-machine.md#falhas-de-provider-e-retomada).
# Notificações operacionais e intervenção humana

`orch init --notifications` pergunta se deseja configurar notificações e quais
canais ativar. Exibe apenas os nomes das variáveis ausentes. Também é possível
editar o perfil diretamente:

```toml
[notifications]
channels = ["email", "discord", "telegram"]
timeout_seconds = 15
retry_seconds = 300
max_attempts = 3
```

O padrão `channels = []` desativa as entregas, mas mantém o escalonamento no
SQLite e no GitHub Project. Cada canal é independente. Credenciais são lidas do
ambiente do processo; nunca coloque seus valores no TOML, Issue ou banco.

| Canal | Variáveis de ambiente |
| --- | --- |
| E-mail | `ORCH_SMTP_HOST`, `ORCH_SMTP_USER`, `ORCH_SMTP_PASSWORD`, `ORCH_EMAIL_FROM`, `ORCH_EMAIL_TO` |
| Discord | `ORCH_DISCORD_WEBHOOK` |
| Telegram | `ORCH_TELEGRAM_TOKEN`, `ORCH_TELEGRAM_CHAT_ID` |

O e-mail usa SMTP com STARTTLS obrigatório e validação de certificado. A porta
padrão é 587; `ORCH_SMTP_PORT` permite ajustá-la. Discord usa um webhook HTTPS e
desativa menções; Telegram envia texto sem interpretação de Markdown.

As mensagens contêm repositório, Issue, motivo estruturado, fase interrompida,
PR, HEAD, número de correções, horário UTC e uma ação de inspeção. Não incluem
prompts, findings, stdout ou exceções dos canais.

O limite de correções usa `review.max_correction_attempts`: se for 5, o sexto
ciclo de correção não inicia. A execução passa a `HUMAN_REQUIRED` e o Project
recebe `Human Review`. Autenticação, modelo irrecuperável, divergência remota,
CI terminal/expirada, merge sem convergência e erro interno recebem `Blocked`.
Quota com retry comprovado continua em espera. Sem retry comprovado, somente
`supervisor.retry_without_reset_seconds` autoriza a espera automática segura.

`orch watch` encerra ao encontrar intervenção humana, preservando a execução
ativa e impedindo a seleção de outra Issue. `orch state --issue N` mostra motivo,
fase, horário e resultados das entregas. Ao executar novamente `orch watch` ou
`orch resume --issue N`, apenas entregas pendentes podem ser tentadas novamente,
respeitando intervalo e limite; entregas confirmadas não são repetidas. Nenhuma
dessas tentativas recria branch, sessão ou PR. Após corrigir um provider,
`orch resume --issue N --retry-provider` reutiliza seu checkpoint e a mesma
sessão comprovada. Demais divergências continuam exigindo reconciliação humana
ou supersessão explícita conforme o fluxo existente.

A deduplicação usa execução, motivo, fase, HEAD, correções e canal, com reserva
transacional no SQLite. Mudanças materiais permitem outro aviso. Cada entrega
em andamento mantém sua reserva por pelo menos cinco minutos, mesmo quando o
intervalo de retry configurado é menor. Cada tentativa
registra somente canal, chave do evento, estado, contador e horário; falhas de
um canal não impedem os outros nem apagam `HUMAN_REQUIRED`. Atualizações do
Project também têm entrega auditada e independente. Se o processo cair depois
do envio externo e antes da confirmação no SQLite, uma repetição ainda é
possível: SMTP/webhooks não fornecem uma transação conjunta com o banco local.
