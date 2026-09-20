# Auditoria forense e hardening — 20/09/2026

> As seções originais registram o encerramento local da auditoria, antes da
> publicação. A entrega e a integração posterior com `main` estão documentadas
> no complemento ao final e no [PR #98](https://github.com/PauloSergioLR/ai-dev-orchestrator/pull/98).

## Resultado e escopo

As correções reforçam identidade, exclusão concorrente, checkpoints, recuperação,
subprocessos, isolamento de gates, cleanup, diagnóstico e distribuição do pacote.
O trabalho foi realizado em worktree isolado, na branch `codex/forensic-hardening`,
sem commit, push ou alteração do checkout operacional. A base e o HEAD final são
`df6de8d4114fb7a958e639e202d676fcb5d4788d`; as correções estão no working tree.

Pesquisa, histórico, máquina de estados, fronteiras de crash e plano anteriores à
implementação estão em [forensic-audit-model.md](forensic-audit-model.md).
O escopo cobre CLI, configuração, domain, adapters, pipeline, work/watch/resume,
recovery, supersession, contrato, cleanup, SQLite, providers, CI/review/merge,
notificações, doctor, testes e documentação. As abstrações por provider foram
preservadas; não foram adicionadas dependências de runtime.

Validação local: Windows, Python 3.13.15, Git 2.54.0.windows.1. Resultado final e
repetições: **1.078 testes aprovados e 3 skipped; três rodadas focadas de 134 aprovados cada**. Linux não estava disponível como ambiente
local utilizável para executar a suíte: havia apenas a distribuição WSL
`docker-desktop`, sem daemon Docker acessível. A matriz da CI mantém Ubuntu e
Windows; execução remota dessa matriz não faz parte dos resultados comprovados.

## Defeitos confirmados, causas e correções

Os caminhos abaixo são relativos à raiz do projeto. A relação completa de arquivos
e estatísticas está no artefato de estado Git indicado ao final.

| Problema e causa raiz | Correção e arquivos principais | Evidência de regressão |
| --- | --- | --- |
| Duas retomadas podiam observar o mesmo checkpoint e iniciar o mesmo efeito; o lock de watch era baseado na existência de um arquivo | `infrastructure/ownership.py`, `services/{pipeline,resume,recovery_executor,supersession,contract_recovery,cleanup,supervisor}.py`: locks do kernel por banco/Issue durante efeitos; watch não depende de PID/idade do arquivo | Barreira de dois resumes: apenas um observa/executa; outro é recusado. Processo com lock termina via `os._exit`; outro adquire depois |
| O limite global podia ser excedido por claims simultâneos de Issues diferentes | `infrastructure/database.py`: contagem e inserção sob `BEGIN IMMEDIATE`; pipeline transmite `max_parallel_runs` | Dois claims sincronizados, capacidade 1, exatamente um record |
| Inicialização simultânea podia disputar WAL/schema | `database.py`: inicialização transacional, retry finito somente para BUSY/LOCKED ao habilitar WAL | Inicializações simultâneas produzem uma linha de versão; repetição da suíte concorrente |
| Context manager de conexão SQLite não fechava necessariamente o handle | `database.py`: commit/rollback seguidos de `close` em `finally` | Fechamento comprovado após sucesso e exceção |
| Identidades atribuídas podiam ser trocadas; review lia antes de reservar escrita; prova antiga podia ser aplicada após checkpoint concorrente | `database.py`: campos de identidade atribuídos uma vez, CAS por snapshot, review/findings/journal em transação; novo HEAD invalida evidência anterior | Matriz checkpoint/transition, troca e remoção de campos; escritor concorrente bloqueado; provas stale de supersessão e contrato recusadas |
| Claim direto podia ignorar FAILED já publicado | `database.py`: criação recusa histórico publicado/transitório que exige reconciliação explícita | FAILED com PR impede novo run até supersessão sintética explícita |
| PREPARING aceitava worktree sem provar base; PR descoberto não comprovava vínculo exclusivo à Issue | `recovery_planner.py`, `recovery_executor.py`, `recovery_observer.py`, `recovery_effects.py`: base SHA imutável, HEAD limpo, ausência de publicação inesperada e `closingIssuesReferences` exato para adoção | Base ausente, HEAD divergente, alterações locais, PR de outra Issue ou de várias Issues bloqueiam |
| Recovery podia usar configuração de outro repositório e Project Done confiava somente no merge salvo | `resume.py`, `recovery_observer.py`, `recovery_planner.py`: identidade verificada antes de retomada; Project exige item da mesma Issue/repo e nova prova do mesmo PR merged/HEAD/commit | Configuração divergente não grava checkpoint; merge remoto ausente/divergente não conclui nem marca Done |
| Provider/gates podiam trocar branch/HEAD sem o pipeline detectar em todas as fronteiras | `pipeline.py`, `publication.py`: observação local antes/depois de gates e review e antes de push; fases publicadas exigem worktree sem alterações pendentes | Mudança de HEAD/branch bloqueia; review antiga não aprova novo HEAD |
| Falha de transporte da CI e reprovação real usavam exceções invertidas | `ci_gate.py`, `pipeline.py`: transporte permanece falha operacional; apenas checks reprovados do HEAD esperado autorizam correção de código | Tests de consulta indisponível, check FAILED e espera da CI |
| Codex tinha timeout fixo de 1800 s, sem distinguir silêncio de atividade; terminal antigo podia aprovar turno novo incompleto | `config.py`, `codex.py`, `process.py`, `heartbeat.py`: total/idle/heartbeat configuráveis; `turn.started` invalida conclusão anterior | Saída periódica reinicia idle, sem estender total; JSONL incompleto após turno concluído falha fechado |
| Resolução do executável podia usar cwd/PATH do pai; scripts batch podiam interpretar metacaracteres; captura sem teto | `process.py`: resolução no contexto do filho, PATH/PATHEXT, recusa de batch inseguro, drenagem simultânea de pipes e teto combinado de 16 MiB | Caminhos relativos, espaços, Unicode, `.exe/.cmd/.bat`, argv nativo, stdin grande, overflow e timeout |
| Morte do controlador no POSIX podia deixar o subprocesso comum vivo | `_process_child.py`, `process.py`: bootstrap com pipe de vida do pai e grupo de processos; Windows preserva Job Object | Teste POSIX específico adicionado, não executado neste Windows; timeout com descendente executado no Windows |
| Gates Python herdavam venv, import paths, opções/cache de pytest e temporários do shell; erro ambiental podia gastar correção de código | `validation.py`: saneamento só para ferramentas Python, temp/cache próprio, preferência pelo venv do worktree; classificação ambiental antes de truncar diagnóstico | Venv estrangeiro, ambiente pai preservado, temp indisponível, PermissionError em pytest cache e traceback após log longo |
| Nome amigável de gate escondia shell; parsing YAML removia aspas internas; evidência podia vir de fora do repo | `project_discovery.py`: risco avalia executável real, mantém quoting e exige contenção de fontes | Shell com nome de step seguro recusado; `python -c` preservado; fonte externa recusada |
| JSON/review admitia duplicatas, excesso de profundidade/tamanho e prova incompleta de identidade | `review.py`, `github.py`: limites, SHA completo, findings válidos, CI aprovada, metadata antes/depois da coleta de diff; AGENTS contido e limitado | JSON inválido/duplicado/profundo/grande, HEAD/base que mudam durante coleta, AGENTS inválido |
| Política de review dependia de um arquivo fora do pacote instalado | Política em `src/ai_dev_orchestrator/resources/`, carregada com `importlib.resources`; `scripts/verify_package.py`; CI constrói e instala wheel | Instalação offline em outro venv/cwd importa CLI e lê política sem checkout |
| Conteúdo de Issue/findings podia fechar delimitadores; erros e entregas podiam propagar credenciais | `review.py`, `pipeline.py`, `redaction.py`, adapters, CLI, inspect/escalation/convergence: JSON reversível com delimitadores escapados, sanitização comum, sem locals no traceback da CLI | Roundtrip de payload adversarial; env secreto sintético, URL com userinfo, Authorization simples e entre aspas |
| Cleanup confiava em caminho/branch/SHA antigos e podia apagar branch avançada ou arquivos ignorados | `cleanup.py`, `git.py`: identidade do repo, contenção e recusa de links/junctions, common-dir/branch/HEAD atuais, status incluindo ignored, CAS em refs local/remota | Bare Git local com avanço após observação preserva branch; caminho externo, branch reutilizada e arquivo ignorado preservados |
| Remote configurado podia apontar para outro repo ou múltiplos destinos | `git.py`, `publication.py`, `recovery_observer.py`, `doctor.py`: fetch/push URLs únicas e identidade GitHub exata, sem substring de hostname | URLs enganosas, pushurl diferente e múltiplos destinos recusados |
| Doctor validava cwd errado e Project acessível sem provar Status operacional | `doctor.py`, `github.py`: repo/cwd e remote configurados, campo Status e opções mapeadas; duplicatas são erro | Configuração fora do cwd, status ausente/duplicado, Project indisponível e temp bloqueado |
| `orch init` perdia campos ao reserializar configuração | `init_project.py`: serialização dos campos tipados, preservando ausência de `required_checks` para descoberta automática | Roundtrip integral incluindo timeouts, reviewer executable, cleanup e configuração de contrato |
| Limites não finitos e infraestrutura de testes pouco protegida | `config.py`, `project_contract.py`, `notifications.py`, `tests/conftest.py`: rejeita NaN/Inf, Retry-After limitado; bloqueia sockets e CLIs externas reais na suíte local | Parametrização de limites; testes das próprias proteções, HTTP/SMTP substituídos por fakes |

## Invariantes e decisões arquiteturais

- SQLite continua sendo checkpoint durável, e o observer continua sendo a fonte
  de prova externa. Estados desconhecidos não equivalem a ausência de recurso.
- Não há transação distribuída entre SQLite e GitHub. Após resposta perdida,
  observa-se o efeito antes de repeti-lo; nenhuma promessa de exatamente uma
  chamada de rede é feita.
- Locks abrangem a operação inteira; heartbeat é informativo. Arquivos de lock
  permanecem no disco para não criar corrida entre inodes. O kernel libera o
  lock ao fechar o handle ou encerrar o processo. Reentrância é por thread.
- O schema permanece na versão 6. As migrações existentes foram mantidas e
  exercitadas por testes; não se abriu nem migrou o banco operacional.
- Registros legados não recebem identidades inventadas. A ausência de prova pode
  bloquear recovery/cleanup e exigir os comandos oficiais de recuperação.
- Defaults de merge automático, cleanup automático e remoção de branches seguem
  desativados. Nenhuma execução irrestrita foi habilitada.
- Cleanup conserva conteúdo ignorado, inclusive venv/cache. Exclusão remota usa
  `--force-with-lease=ref:SHA` com refspec de exclusão, sem reescrever histórico.
  A comparação é feita pelo servidor conforme [git-push](https://git-scm.com/docs/git-push).
- O recurso empacotado usa [importlib.resources](https://docs.python.org/3/library/importlib.resources.html).
  O lock Windows usa [msvcrt.locking](https://docs.python.org/3/library/msvcrt.html),
  e a infraestrutura existente de processos usa Job Objects.

## Inventário de limites e políticas

Todos os tempos configuráveis passam a recusar NaN e infinito. Não se transformou
cada detalhe interno em configuração. Os limites existentes preservados também
estão listados para distinguir política nova de política anterior.

| Operação | Default/limite | Origem e decisão |
| --- | --- | --- |
| Codex total | 7200 s; máximo 86400 | Novo `providers.codex_timeout_seconds` |
| Codex sem bytes em stdout/stderr | 1800 s; máximo 86400 | Novo `providers.codex_idle_timeout_seconds` |
| Heartbeat Codex | 60 s; máximo 3600 | Novo `providers.codex_heartbeat_seconds` |
| Review Antigravity | 900 s; heartbeat 300 s | `review.timeout_seconds`; heartbeat interno existente |
| GitHub Project | 60 s; máximo 300 | `github.project_timeout_seconds`, preservado |
| Merge GitHub | 30 s | `execution.merge_timeout_seconds` |
| Espera de CI | 900 s; polling 5 s | `[ci]`; chamadas individuais de leitura 30 s |
| Consistência eventual | 30 s; polling 1 s | `[convergence]` |
| Gate local | 900 s | Contrato congelado/override por gate |
| Watch | polling 60 s; sono máximo 300 s | `[supervisor]`; quota sem reset exige política explícita |
| Retry transitório de provider | 30, 60 e 120 s; até 3 | Política persistida, separada de correção de código |
| Notificação | 15 s, máximo 60; retry 300 s; 3 tentativas | `[notifications]`; reserva SENDING mínima de 300 s |
| Git geral / publicação | 20 / 30 s | Constantes preservadas dos adapters |
| GitHub Issue / PR / observer | 20 / 30 / 30 s | Constantes preservadas |
| Init/discovery / runner genérico | 15 / 5 s | Operações locais curtas; constantes preservadas |
| SQLite busy e inicialização WAL | 5 s | Parâmetro do store; retry restrito a contenção |
| Encerramento de processos/threads | até 5 s por etapa | Limite interno de limpeza; falha bloqueia retry |
| Captura / JSON review / AGENTS | 16 MiB / 1.048.576 caracteres / 1 MiB | Tetos de memória/protocolo; não configuráveis |
| Code Review Graph | 180 s na configuração | Preservado, mas não executado nesta auditoria |

Overrides seguem `ORCH_PROVIDERS__CODEX_TIMEOUT_SECONDS`,
`ORCH_PROVIDERS__CODEX_IDLE_TIMEOUT_SECONDS` e
`ORCH_PROVIDERS__CODEX_HEARTBEAT_SECONDS`. Aumentar total não altera idle. Bytes
periódicos indicam atividade, não provam progresso semântico. Timeout, quota e
falha de protocolo continuam tendo políticas distintas.

## Testes e fault injection

Novos módulos: `test_recovery_forensic.py`, `test_runtime_forensic.py`,
`test_operations_forensic.py`, `test_test_isolation.py` e o E2E POSIX
`test_process_crash.py`. Regressões foram acrescentadas também às suítes de CI,
pipeline, planner/observer/resume, review e pacote. Fakes antigos passaram a
fornecer URLs, bases, SHAs completos e provas de merge equivalentes ao contrato
real, sem relaxar validação de produção.

Foram executados localmente:

- Claims, inicialização SQLite e dois resumes concorrentes com barreiras;
  review com escritor concorrente; snapshots alterados entre prova e aplicação.
- Crash de processo proprietário por `os._exit`, liberação pelo kernel e nova
  aquisição; timeout de árvore sintética com descendente que tentaria escrever
  um marcador; saída parcial, stdin grande e encoding CP1252/UTF-8.
- Restart nas fronteiras push, PR, review e merge, reaproveitando a mesma sessão,
  branch, execution_id e PR. Sucesso externo sem checkpoint é observado antes de
  nova mutação. Review sem persistência é reexecutada, sem falsa aprovação.
- Merge manual e MERGING legado; Project já Done versus desconhecido/divergente;
  retry de provider, quota com e sem reset confiável e ausência de diff.
- Recovery de gates/publicação, FAILED histórico, recuperação de contrato por
  base SHA, supersessão explícita e cleanup, sempre em fixtures sintéticas.
- Ambiente Python contaminado, temp indisponível, cache bloqueado e traceback
  longo; JSON truncado/inválido/duplicado, resposta malformada e limite de saída.
- Exclusão remota e local com ref avançada entre observação e ação em repositórios
  Git locais; contenção de caminhos e preservação de arquivos ignorados.
- Falhas e retries de notificação mediante fakes: nenhum envio real. O journal
  impede duplicatas confirmadas, mas resposta perdida após envio ainda é risco
  de entrega duplicada, documentado abaixo.

As suítes existentes de incidentes históricos foram mantidas: permissões Windows,
CP1252, `NoneType` no runtime, `unknown owner type`, Project/timeout/preflight,
no-diff, contract drift e identidade. Não houve reimplementação indiscriminada.
Não se afirma ter matado o processo em cada instrução possível: as injeções
cobrem fronteiras e invariantes selecionadas, além da matriz unitária existente.

## Validação reproduzível

Antes de cada comando de testes, foram removidos somente do processo filho os
overrides `ORCH_*`, `VIRTUAL_ENV`, `PYTHONPATH`, `PYTEST_ADDOPTS` e
`PYTEST_DEBUG_TEMPROOT`; `UV_OFFLINE=1` evitou downloads. Isso não altera ambiente
persistente nem arquivos de configuração do usuário.

| Verificação | Resultado |
| --- | --- |
| Baseline antes das correções | 902 aprovados, 2 skipped, 65,42 s |
| Suíte final | 1.078 aprovados, 3 skipped, 72,98 s; 176 aprovações a mais que o baseline |
| Três repetições focadas | 134 aprovados em cada rodada; 11,54 s, 11,46 s e 11,58 s |
| `uv run ruff check .` | Aprovado |
| `git diff --check` | Aprovado |
| `uv build --offline --out-dir .pytest-tmp-audit-dist` | sdist e wheel construídos offline |
| `uv run python scripts/verify_package.py .pytest-tmp-audit-dist/ai_dev_orchestrator-0.1.0-py3-none-any.whl` | 16 dependências instaladas do cache; CLI e política importadas fora do checkout |
| `orch doctor`, sem `--deep` | Exit 0, verificações OK no ambiente isolado |

Comando final de testes: `uv run pytest -q --basetemp=.pytest-tmp-final-all
--junitxml=.pytest-tmp-audit-dist/final-all.xml`. Repetições: os três módulos
`test_recovery_forensic.py`, `test_runtime_forensic.py` e
`test_process_policies.py`, com diretórios temporários separados e XML por rodada.
Uma tentativa de loop PowerShell teve argumento `--basetemp` malformado e foi
recusada antes de coletar testes; o comando foi corrigido e as três rodadas
válidas constam nos XMLs. Rodadas intermediárias com falhas foram investigadas,
não suprimidas; revelaram também a contenção de inicialização WAL corrigida.

Os três skips finais têm justificativa verificável: dois probes Antigravity são
explicitamente live/opt-in e não podem rodar nesta auditoria; um teste SIGKILL/
lifeline é exclusivo POSIX. Testes Windows de Job Object e batch foram executados.
Não houve skip adicionado para contornar falha de teste comum.

O doctor utilizou cópia de configuração com repo/worktrees/state apontando apenas
para a área da auditoria, notificações desativadas e Code Review Graph desativado.
Executou versões/help locais de CLIs e consultas GitHub somente de leitura;
confirmou autenticação, repositório/remote, Project 6 (46 itens), contrato,
checks `test`/`test-windows`, gates e probes de escrita isolados. Não abriu SQLite
operacional nem iniciou sessão de IA. Sua leitura remota é diagnóstico autorizado,
separado dos testes automatizados offline.

## Segunda revisão e limitações residuais

A segunda leitura do diff incluiu implementação, fixtures, configuração, CI e
empacotamento. Foram fechados pontos adicionais de base/HEAD, identidade do
repositório, Project/merge atual, arquivos ignorados, Authorization entre aspas,
delimitadores de prompts e classificação após truncamento. Não restaram falhas
reproduzidas na validação Windows descrita; isso não constitui prova de ausência
universal de defeitos.

1. **Linux pendente de execução:** o bootstrap POSIX novo e os demais testes devem
   passar na matriz Ubuntu antes da integração. Nenhum resultado Linux foi
   inferido do Windows. Não foi disparada CI remota nem criado PR nesta auditoria.
2. **Coordenação local:** todas as instâncias precisam compartilhar banco e locks
   em filesystem local. Bancos independentes, hosts diferentes, NFS e processos
   antigos que não respeitam ownership não são coordenados. Não atualizar uma
   instalação enquanto um controlador antigo ainda executa efeitos.
3. **Processos não são sandbox:** Job Object/grupo POSIX contêm árvores comuns;
   daemonização deliberada, escape de grupo e efeitos remotos iniciados pelo
   provider exigem isolamento de SO adicional. O bootstrap POSIX não foi validado
   localmente. Sua vigilância termina com o bootstrap: existe uma janela residual
   se o comando principal já terminou, um descendente mantém os pipes abertos e
   o controlador sofre SIGKILL antes de encerrar o grupo. A contenção completa
   desse caso exige supervisor residente ou cgroup, com validação Linux; esta
   mudança não declara essa garantia. Locks não impedem um editor/processo
   externo de mudar arquivos.
4. **TOCTOU externo:** checks de filesystem e observações GitHub não formam
   transação com terceiros. Git ref CAS e merge condicionado ao SHA reduzem
   corridas; mudança externa de caminho/configuração entre observar e agir ainda
   exige operação exclusiva do workspace e permissões locais adequadas.
5. **Respostas perdidas:** review/provider pode consumir chamada novamente após
   crash antes de persistir resultado. Notificações podem duplicar após envio
   sem confirmação durável; canais não oferecem idempotência transacional.
6. **Compatibilidade conservadora:** cleanup bloqueia registros antigos sem repo/
   SHA e preserva `.venv`/caches ignorados; branch local pode permanecer se HEAD
   da base estiver atrasado ou merge squash não provar ancestralidade. Não há
   limpeza forçada nem reparação automática de dados insuficientes.
7. **Remotes:** a prova implementada aceita formas canônicas GitHub.com HTTPS/SSH,
   destino único. Aliases SSH, host enterprise, proxy ou múltiplos pushurls ficam
   recusados até haver modelagem explícita; não são inferidos por substring.
8. **Conteúdo não confiável:** JSON escapado e redaction reduzem classes conhecidas
   de injeção/vazamento, mas não são barreira completa contra prompt injection nem
   detector universal de segredos inéditos. Providers mantêm as permissões locais
   já existentes. As proteções dos testes também não substituem sandbox de rede
   para código arbitrário em subprocessos; nenhum teste executado o utilizou.
9. **Diagnóstico finito:** captura de 16 MiB interrompe logs excessivos. Um provider
   saudável silencioso além do idle configurado precisa de limite adequado à
   operação. Git/Issue/init mantêm limites internos curtos listados acima.

## Preservação do ambiente e evidências Git

Nenhum provider live adicional foi consumido, nenhum Code Review Graph foi
executado e nenhuma notificação real foi enviada. Não houve alteração de ACL,
elevação, credenciais/secrets, `~/.codex/config.toml`, SQLite de produção ou da
execução real da Issue #96. Não foram executados reset/clean destrutivos,
supersessão, retomada ou cleanup de execução real, merge/fechamento de PR real ou
mutação de Project real. Operações destrutivas dos testes ficaram restritas a
repositórios, refs, arquivos e bancos sintéticos criados pelas próprias fixtures.

Artefatos locais ignorados pelo Git preservam XMLs, wheel/sdist e
`.pytest-tmp-audit-dist/final-git-state.txt`, com HEAD, branch, `git status --short`,
`git diff --stat` completo e verificação do checkout original. Novos arquivos de
código/documentação têm apenas intent-to-add para aparecer no diff; nenhum conteúdo
foi staged para commit. O checkout original permanece sem alterações tracked.

## Complemento: entrega e integração com main

A auditoria foi preservada no commit
`e0783ad2c51c845de5152922f9b8034c5d599cde`, publicado na branch
`codex/forensic-hardening` e no PR #98. Após a entrega, a integração de
`main` em `78c28ce48ac2a07f95e46e255fe230121091b1df` exigiu resolver conflitos
em `validation.py` e `test_publication.py`. O commit original não foi
reescrito; a integração usa merge de `main` na branch da auditoria.

A resolução preserva o envio explícito do ambiente aos runners, os temporários
controlados dentro do worktree e os ajustes já presentes em `main`. Combina-os
com saneamento de venv/PATH, opções e caches Python, preservação do ambiente do
pai e classificação de falhas antes de truncar o diagnóstico. Os dois motivos
de falha ambiental permanecem compatíveis; `PermissionError` e `WinError 32`
no temporário controlado não consomem tentativa de correção de código.

Validação após a integração, no Windows:

- Suíte focada de gates, publicação, descoberta, runtime e adapters: 130 aprovados.
- Primeira suíte completa: 1.084 aprovados e 3 skips esperados.
- Suíte final, incluindo duas regressões adicionais de acesso ao temporário:
  **1.086 aprovados e 3 skips esperados em 61,67 s**.
- Ruff e verificação de whitespace aprovados; build offline e instalação do
  pacote fora do checkout aprovados durante a integração.
- XML final: `.pytest-tmp-audit-dist/merge-final.xml`. Os três skips continuam
  sendo os dois probes live/opt-in e o teste SIGKILL/lifeline exclusivo POSIX.

Os resultados remotos da matriz Ubuntu/Windows são registrados nos checks e no
corpo do PR #98, por execução e commit. Eles complementam o retrato local original;
a limitação residual de contenção POSIX descrita acima permanece documentada.
O PR permanece aberto para revisão. Esta integração não executa merge do PR,
retomada da Issue #96, mutação do seu worktree ou SQLite operacional, alteração
do Project ou ação sobre o PR #97.
