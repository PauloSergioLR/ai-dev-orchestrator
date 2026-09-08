# Máquina de estados de retomada segura

## Separação de responsabilidades

A retomada segura parte de três entradas distintas: a `RunRecord` persistida,
uma `RecoveryObservation` e uma `RecoveryPolicy`. A observação contém somente
fatos coletados no momento da retomada: worktree, HEADs, PRs, CI, merge e estado
do projeto. A política contém configuração e invariantes esperadas:
repositório, base do Pull Request e autorização de auto-merge.

O `RecoveryPlanner` é puro: recebe essas entradas e devolve uma
`RecoveryDecision`. O `RecoveryExecutor` realiza no máximo a ação decidida,
registra checkpoint e o serviço observa novamente. Assim, decisão e efeito
externo não se misturam.

O `RecoveryExecutor` aplica apenas uma decisão recebida e delega I/O a
`RecoveryEffects`, conectado aos adapters existentes. Cada
efeito bem-sucedido recebe checkpoint no mesmo execution_id. Uma queda entre o
efeito e o checkpoint será reconciliada pela próxima observação e planejamento.
Antes de qualquer efeito, o executor recarrega o record, confere que ele não
mudou desde o planejamento e valida que a ação é compatível com a fase atual.

Na integração final, o observer coleta fatos externos somente por leituras e o
serviço `resume` repete observar, planejar e aplicar uma única ação até haver
checkpoint terminal ou bloqueio seguro. Novas execuções usam as fases explícitas
de commit, push, PR, merge e Project Done; `PUBLISHING` e `MERGING` permanecem
somente para compatibilidade histórica.

## Fases e ações

As fases legadas `PUBLISHING` e `MERGING` permanecem por compatibilidade. Na
retomada, `PUBLISHING` só migra para checkpoints granulares quando a relação
direta do commit e o HEAD remoto permitem provar o próximo passo; `MERGING`
reutiliza as mesmas provas de PR, CI, review e merge de `MERGE_PENDING`.
As novas fases explicitam cada efeito: `COMMIT_PENDING`, `PUSH_PENDING`,
`PR_PENDING`, `MERGE_PENDING` e `PROJECT_DONE_PENDING`.

| Fase | Fato exigido | Ação |
| --- | --- | --- |
| `PREPARING` | worktree ausente ou convergente | preparar ou avançar |
| `CODEX_RUNNING` | sessão Codex persistida | retomar Codex |
| `TESTING` | gates pendentes | executar gates locais |
| `COMMIT_PENDING` | qualquer alteração do worktree ou commit direto comprovado | criar ou registrar commit |
| `PUSH_PENDING` | remoto ausente, pai direto ou igual ao local | push ou registrar push |
| `PR_PENDING` | identidade completa do PR convergente | criar ou adotar PR |
| `WAITING_CI` | CI do HEAD exato | aguardar ou registrar sucesso |
| `GEMINI_REVIEWING` | review persistida no record | revisar ou avançar |
| `NEEDS_CHANGES` | sessão, review rejeitada e findings do mesmo HEAD | retomar correção |
| `MERGE_PENDING` | PR, CI, HEAD local e merge convergentes | merge ou registrar merge |
| `PROJECT_DONE_PENDING` | estado explícito do projeto | marcar Done ou completar |

O planner falha fechado: contradição, ambiguidade, SHA divergente ou estado
`UNKNOWN` produz `BLOCK`. Em especial, `UNKNOWN` nunca é interpretado como
false ou `NOT_DONE`.

Toda fase que depende de worktree exige a identidade persistida completa:
branch, caminho do worktree e ref base. As fases publicadas também exigem HEAD
local idêntico ao checkpoint. A partir de `WAITING_CI`, o número e a URL do PR
devem estar persistidos juntos, e a observação deve conter exatamente um PR com
repositório, base, branch e HEAD convergentes. Uma identidade parcial bloqueia.

## Provas de histórico e review

Para não aceitar divergências silenciosas, commit e push usam relações diretas:
um commit já existente só é aceito quando o pai imediato do HEAD local é o
checkpoint; um push pendente só é permitido para remoto ausente ou SHA igual ao
pai imediato do HEAD local. Relações ancestrais arbitrárias não bastam. O fato
de o worktree estar dirty inclui arquivos novos: a validação de conteúdo cabe
aos gates do executor, não ao planner.

`PROJECT_DONE_PENDING` somente pode marcar o projeto depois de merge comprovado
no record: item de projeto, SHA revisado, SHA merged igual ao revisado e commit
de merge precisam estar presentes. Isso impede marcar Done após uma observação
incompleta ou após merge não persistido.

O resultado do Gemini só é recuperável depois que o executor persistir
veredito, SHA revisado e findings no store. Se o processo cair após a chamada
ao Gemini e antes desse checkpoint, a fase `GEMINI_REVIEWING` não tem review
persistida e planeja `REVIEW_HEAD` novamente. Findings estruturados são
associados ao SHA rejeitado para provar que a correção responde à revisão.
Os findings vivem no mesmo SQLite da execução e a persistência do review é
atômica com o evento de journal. A tentativa de correção é incrementada antes
da chamada ao Codex, preservando auditoria mesmo se o provider falhar.

A cobertura tem três níveis: planner puro, executor com doubles dos adapters e
integração controlada do observer, effects, serviço de retomada e CLI.

## Falhas de provider e retomada

A política é centralizada por classificação, compartilhada entre pipeline,
resume e supervisor. Esperas e bloqueios continuam ativos: work/watch não
selecionam outra Issue enquanto existir uma execução suspensa.

| Classificação | Estado e política |
| --- | --- |
| TERMINAL_QUOTA, TRANSIENT_RATE_LIMIT | Espera do provider; reset confiável ou intervenção |
| NETWORK_ERROR, TIMEOUT, LOCAL_TRANSIENT (inclui OS_ERROR local) | WAITING_PROVIDER; até três retries após 30, 60 e 120 segundos |
| AUTH_ERROR, MODEL_UNAVAILABLE | BLOCKED_PROVIDER; corrigir a causa antes de retry explícito |
| UNKNOWN, EXECUTABLE_MISSING, PROTOCOL_ERROR, MALFORMED_JSON, ENCODING_ERROR, PROCESS_CLEANUP_ERROR | Bloqueio conservador, sem retry automático |

TERMINAL_QUOTA descreve a indisponibilidade da quota, não o fim da execução.
O horário de reset exige data e fuso explícitos. “Try again at 5:09 AM” não
basta. Sinais JSONL têm precedência determinística: quota, autenticação,
modelo indisponível, rate-limit, rede, timeout e desconhecido. O exit 126 com
stderr vazio é diagnosticado pelos eventos error/turn.failed; error.code
não é obrigatório. A identidade emitida em thread.started é preservada,
inclusive em falhas com saída parcial confiável.

A fase suspensa e o contador de retries ficam no SQLite e sobrevivem ao
reinício. Uma tentativa bem-sucedida encerra a sequência de retries daquela
etapa. Ao esgotar o limite, a execução permanece bloqueada. Diagnósticos
persistem classificação, origem, exit code e tentativa; mensagens brutas,
prompts e URLs dos providers não são copiados para o journal.

Use orch state --issue N para inspecionar o checkpoint e orch resume --issue N
para a retomada normal. Após corrigir a causa, orch resume --issue N
--retry-provider permite uma tentativa explícita, inclusive quando não há
reset confiável. Essa opção não dispensa as provas do planner. Se o início do
Codex foi tentado sem que um ID confiável chegasse ao checkpoint, o sistema
bloqueia: não inicia outra sessão nem usa --last como alternativa.

Durante uma correção, execution_id, sessão, PR, branch, worktree, modelos e
findings são preservados. Retry de transporte não incrementa novamente
correction_attempts. Depois do novo commit, as provas antigas de CI/review/merge
são invalidadas; os findings continuam associados ao SHA que os originou.

## Recuperação explícita de FAILED histórico

orch resume --issue N --recover-failed solicita reconciliação pelo domínio/store.
A migração automática para schema 3 preserva registros anteriores e acrescenta
os checkpoints de início de sessão e retry. Nenhuma edição manual do banco é
necessária ou suportada por esse caminho.

Somente FAILED com evidência persistida de falha transitória de rede, timeout ou
falha local transitória pode ser candidato. O journal deve provar a fase anterior
CODEX_RUNNING, GEMINI_REVIEWING ou TESTING. O observer exige worktree e branch
corretos, raiz/base do repositório, HEAD local e remoto iguais ao checkpoint,
PR único aberto e vinculado à Issue aberta, ausência de merge, identidade e
status convergentes do item do Project e metadados locais da sessão Codex
com o mesmo ID e diretório. CI, review e findings também precisam ser coerentes
com a etapa. Metadados ausentes, duplicados ou em formato desconhecido bloqueiam.

Após leituras externas, o store compara novamente o snapshot e revalida as
provas em transação, impedindo reativação concorrente com outro run ativo.
A reativação preserva o ID, registra evento auditável e não flexibiliza a
transição genérica de FAILED. A execução volta ao planner antes de qualquer
efeito. Mudanças externas posteriores continuam sujeitas às validações de cada
etapa; as leituras GitHub não constituem uma transação com o banco local.

work/watch bloqueiam a seleção de nova Issue enquanto houver candidato histórico
pendente de reconciliação explícita. Registros terminais sem evidência suficiente
não são automaticamente reinterpretados como falhas transitórias.

## Processos e encoding no Windows/Linux

CommandRunner captura bytes e declara políticas independentes para stdout e
stderr. SYSTEM_TEXT tenta UTF-8 e então a code page local, preservando bytes
não mapeáveis como escapes visíveis. UTF8_STRICT rejeita corrupção sem fallback;
BINARY mantém os bytes sem interpretação textual. Um erro de decoding preserva
o returncode real e identifica o stream. Codex JSONL, Antigravity JSON e saídas
GitHub de máquina usam UTF8_STRICT; stderr diagnóstico pode usar SYSTEM_TEXT.

Prompts seguem por stdin UTF-8 e shell=False permanece obrigatório. No Windows,
um bootstrap Python aguarda o vínculo a um Job Object privado antes de iniciar
o comando. A árvore herda o job, sem breakaway; timeout termina o job e confirma
que não há processos ativos antes de liberar retry. Fechar o job também encerra
descendentes remanescentes. Não há dependência de taskkill nem enumeração global
de processos. No Linux, o comando inicia em sessão própria e o timeout encerra
seu grupo de processos. Isso não é um sandbox para comandos que tentem escapar
deliberadamente do grupo. Falha em confirmar encerramento bloqueia a retomada
automática (PROCESS_CLEANUP_ERROR).

O mecanismo Windows segue o contrato de
[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
Os testes locais usam processos temporários, stdin grande, caminhos com espaços
ou acentos, CP1252 e timeout com descendente; não executam sessões reais de IA.

## Supersessão explícita

`SUPERSEDED` é terminal e significa que uma pessoa declarou que aquele run não
deve mais ser retomado. Não é `COMPLETED`, não altera o Project para `Done` e não
é uma falha transitória recuperável. O SQLite mantém execution_id, branch,
worktree, sessão Codex, PR, HEAD, findings e journal; o evento final inclui o
motivo sanitizado e a evidência remota observada no momento da decisão.

`orch supersede --issue N --reason "..."` primeiro lê o estado remoto. A decisão
só é possível quando existe exatamente o PR persistido, com número, URL e branch
idênticos, em `CLOSED` sem merge comprovado. Estado remoto desconhecido, múltiplos
PRs, identidade divergente, PR aberto ou mergeado bloqueiam. HEAD remoto diferente
é mostrado no evento como evidência, sem ser adotado. A confirmação interativa é
obrigatória por padrão; `--yes` é a confirmação explícita para automação.

O scheduler bloqueia qualquer `FAILED` que já tenha PR associado, inclusive uma
falha que não se enquadre como candidata histórica transitória. Isso impede que
um PR aberto ou Project ainda em review seja ignorado só porque `list_active()`
está vazio. O run deixa de bloquear apenas após reconciliação segura ou
supersessão explícita. `SUPERSEDED` não aparece em `list_active()` nem em
`list_historical_candidates()`, mas continua em `orch history`. A supersessão não
inicia novo run; uma futura seleção de Issue em `Ready` recebe outro execution_id.
