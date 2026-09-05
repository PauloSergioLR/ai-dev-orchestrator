# Revisão Gemini

Após Codex, gates locais, commit, push, Pull Request, Status `AI Review` e CI
verde do HEAD exato, o orquestrador monta um `ReviewDossier`, solicita um
`ReviewPlan` e executa uma nova invocação headless do Gemini via Antigravity.
O resultado é `APPROVED` ou `REJECTED` com findings estruturados.

A chamada usa `--output-format json` e `--json-schema`, com o prompt no stdin.
O reviewer roda com `--sandbox` e expansão de slash commands desabilitada, mas
sem `--mode plan`: quando esse modo passou a ser efetivamente aplicado pela CLI
em execuções headless, foram observados envelopes `SUCCESS` sem
`structured_output`. O modo de resposta padrão preserva o contrato estruturado;
o sandbox preserva a contenção e nenhuma permissão irrestrita é concedida.
Um `SUCCESS` sem objeto `structured_output` é falha de protocolo e nunca é
interpretado como aprovação.

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
