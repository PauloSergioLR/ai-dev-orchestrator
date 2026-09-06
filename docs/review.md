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
flag causava a perda do objeto. A investigação atual não reproduziu o erro
histórico na versão 1.1.27 e não estabelece essa causalidade. Não reintroduzimos
a flag. As evidências e os limites estão em [investigação da CLI](antigravity-investigation.md).

A política estável em `prompts/gemini/review_policy.md` separa instruções de
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

Falha de protocolo mantém a execução em `GEMINI_REVIEWING`, sem review persistida
nem merge. Após corrigir a instalação/configuração, `orch watch` pode repetir
o reviewer para a mesma execução, sessão Codex, worktree, PR e HEAD, sujeito às
revalidações normais. Não é necessário editar o SQLite. O watch pode continuar
outros efeitos configurados após a aprovação; não é um comando de diagnóstico.

O teste `tests/integration/test_antigravity_live.py` é opt-in via
`ORCH_TEST_ANTIGRAVITY_LIVE=1`: faz duas chamadas sintéticas em diretório temporário,
consome quota e valida os schemas reais sem tocar no pipeline. No Windows, o
daemon da CLI pode manter o diretório temporário aberto após terminar o processo.
