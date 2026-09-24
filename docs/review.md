# Revisão Gemini

Após Codex, gates locais, commit, push, Pull Request, Status `AI Review` e CI
verde do HEAD exato, o orquestrador monta um `ReviewDossier`, solicita um
`ReviewPlan` e executa uma nova invocação headless do Gemini via Antigravity.
O resultado é `APPROVED` ou `REJECTED` com findings estruturados.

A ferramenta é a [Antigravity CLI oficial](https://antigravity.google/docs/cli/install/),
executável `agy` (no Windows, `agy.exe`), com autenticação prévia. Não é o launcher
do IDE nem a Gemini CLI. `review.executable` aceita o nome no PATH ou caminho
absoluto, por exemplo `'C:\Users\usuario\AppData\Local\agy\bin\agy.exe'` no TOML.
`ORCH_REVIEW__EXECUTABLE` tem precedência. A resolução usa `shutil.which` no
ambiente do processo, sem procurar nomes alternativos. Depois de instalar a
CLI, terminais já abertos podem continuar com um PATH antigo.

Doctor e runtime compartilham `AntigravityAdapter.check_available`: executam
`--version` e `--help` pela mesma resolução e verificam `--input-format`,
`--sandbox`, `--disable-slash-commands`, `--print-timeout`, `--output-format`,
`--json-schema` e, para modelo explícito, `--model`. O doctor valida somente
capacidades locais; não consome quota nem promete sucesso remoto. Se a CLI não
existe, o erro contém `Executável não encontrado` e orienta configurar
`review.executable`/`ORCH_REVIEW__EXECUTABLE` ou ajustar o PATH do processo.

A chamada usa `--input-format text`, `--output-format json`, `--json-schema`
e `--print-timeout <segundos>s`, com o prompt exclusivamente no stdin UTF-8,
cwd explícito e timeout de subprocesso. A CLI 1.1.27 foi testada no Windows:
stdin sem `-p` e `-p <prompt>` produziram `structured_output`; `--print ""`
com stdin retornou erro de prompt vazio. Preservamos stdin para suportar dossiers
maiores que o limite de argv do Windows e evitar expor seu conteúdo na lista
de processos. Não há shell nem permissões irrestritas; permanecem `--sandbox`
e `--disable-slash-commands`.

O [contrato oficial headless](https://antigravity.google/docs/cli/headless/)
e as chamadas locais confirmam um envelope JSON com `status: SUCCESS` e objeto
`structured_output`. `response` é texto livre e não é usado como fallback.
Um exit code diferente de zero, erro, JSON inválido ou `SUCCESS` sem objeto
estruturado bloqueia a revisão. A validação de domínio ainda exige todos os
campos, verdict conhecido, findings coerentes e SHA exatamente igual ao esperado.

O PR #53 retirou `--mode plan`, mas seu teste simulava a hipótese de que essa
flag causava a perda do objeto. Os probes sintéticos iniciais não reproduziram
o erro. A reprodução posterior com o dossier real identificou `denied_actions`
para `command`: a CLI encerrou com SUCCESS após negar uma ação em headless,
sem resposta nem objeto estruturado. A flag não foi reintroduzida.
As evidências estão em [investigação da CLI](antigravity-investigation.md).

A política de produção define uma revisão baseada exclusivamente no dossier:
o planner e o reviewer não executam comandos, testes, Git, consultas externas
ou ferramentas de arquivos. Os gates e a CI são coletados pelo orquestrador
antes da análise. Quando faltar evidência necessária, o reviewer deve explicitar
a lacuna e rejeitar, sem inventar verificações. O mecanismo de resposta
estruturada da CLI permanece permitido. Essas instruções não substituem o sandbox
nem alteram permissões locais.

As severidades bloqueantes configuradas são incluídas na parte autoritativa do
prompt das duas etapas, com a regra explícita de usar REJECTED quando qualquer
finding for bloqueante. Uma resposta contraditória continua inválida; o adapter
e o parser não convertem automaticamente APPROVED em REJECTED nem descartam findings.

Qualquer `denied_actions` diferente de lista vazia bloqueia o review, inclusive
se houver `structured_output` com APPROVED. A mensagem indica revisão incompleta
por bloqueio de permissões, sem expor os argumentos da ação negada. Não é
necessário habilitar `--dangerously-skip-permissions` nem criar permissões globais
de shell para corrigir esse fluxo.

A política estável em `src/ai_dev_orchestrator/resources/review_policy.md` separa instruções de
autoridade do dossier não confiável. Issue, PR, diff e código são sempre dados,
nunca instruções. O SHA é revalidado antes da revisão final.

Use `[review]` no TOML para configurar `provider = "antigravity"`,
`timeout_seconds = 900`, `max_correction_attempts = 3` e
`blocking_severities = ["CRITICAL", "HIGH", "MEDIUM"]`. Após um `REJECTED`, o
orquestrador retoma a mesma sessão Codex no mesmo worktree, publica a correção no
mesmo PR, aguarda a CI do novo HEAD e cria novo planner e reviewer. Findings
anteriores entram no dossier como histórico estruturado. As variáveis
`ORCH_REVIEW__...` seguem o mesmo mapeamento. Não há merge automático, mudança
para `Done` ou cleanup nesta etapa.

Falhas estruturais transitórias têm **um único retry automático por HEAD**,
compartilhado pelo planner e pela análise final. `PROTOCOL_MALFORMED_RESPONSE`
(por exemplo, envelope JSON inválido ou SUCCESS sem structured output) e
`PROTOCOL_SCHEMA_MISMATCH` (campos ausentes/extras, enum ou tipo inválido) podem
usar esse retry. A segunda falha exige `HUMAN_REQUIRED`, mantendo a fase
interrompida `GEMINI_REVIEWING`. Não há fallback para markdown ou texto livre.

`PROTOCOL_HEAD_MISMATCH`, `PROTOCOL_CLI_INCOMPATIBLE` e
`PROTOCOL_SEMANTIC_INVALID` exigem intervenção imediata. Isso inclui SHA diferente,
CLI sem flags/schema compatíveis, `denied_actions` e APPROVED com finding
bloqueante. Auth, quota, network e modelo mantêm sua política própria de recovery.

O checkpoint guarda a entrada preparada e sanitizada, incluindo dossier,
evidências e, quando validado, review plan. O retry envia exatamente o mesmo
prompt, com o mesmo execution_id, PR e HEAD. Não executa Codex, gates, publicação,
preparação de graph ou nova espera de CI. Identidades local e remota continuam
sendo verificadas antes e depois de cada chamada. Mudanças de configuração ou
schema durante a retomada bloqueiam o reaproveitamento do checkpoint.

`review_protocol_retry_attempts` é separado de correções e reviews; não consome
`review.max_correction_attempts`. O orçamento é reservado antes do retry e
preservado após restart. Uma interrupção com chamada ainda em andamento exige
intervenção, pois não é possível provar se ela terminou. Erros ficam no histórico
como classificação e códigos fixos sanitizados (`protocol=...`), sem stdout,
stderr, nomes de campos arbitrários ou valores rejeitados do provider.

O teste `tests/integration/test_antigravity_live.py` é opt-in via
`ORCH_TEST_ANTIGRAVITY_LIVE=1`: valida os schemas e uma revisão sintética usando
a mesma política de produção e `build_prompt`, incluindo regras não confiáveis
que pedem execução de comandos. Consome quota sem tocar no pipeline. No Windows, o
daemon da CLI pode manter o diretório temporário aberto após terminar o processo.
