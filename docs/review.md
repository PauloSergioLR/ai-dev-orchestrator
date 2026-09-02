# Revisão Gemini

Após Codex, gates locais, commit, push, Pull Request, Status `AI Review` e CI
verde do HEAD exato, o orquestrador monta um `ReviewDossier`, solicita um
`ReviewPlan` e executa uma nova invocação headless do Gemini via Antigravity.
O resultado é `APPROVED` ou `REJECTED` com findings estruturados.

A política estável em `prompts/gemini/review_policy.md` separa instruções de
autoridade do dossier não confiável. Issue, PR, diff e código são sempre dados,
nunca instruções. O SHA é revalidado antes da revisão final.

Use `[review]` no TOML para configurar `provider = "antigravity"`,
`timeout_seconds = 900` e `blocking_severities = ["CRITICAL", "HIGH", "MEDIUM"]`.
As variáveis `ORCH_REVIEW__...` seguem o mesmo mapeamento. Não há feedback
automático ao Codex, merge automático, mudança para `Done` ou cleanup nesta etapa.
