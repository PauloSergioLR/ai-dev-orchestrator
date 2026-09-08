# Evidências sanitizadas

`quota-execute.json` reproduz o probe relatado na Issue #58: exit 126,
stderr vazio e quota em `error.message`/`turn.failed.error.message`, sem code
nem timestamp confiável. URLs e dados de conta foram removidos.

`quota-resume.json` é uma derivação sintética do mesmo incidente para a sessão
existente. Não é uma nova captura da CLI nem usa a sessão real da Issue #47.
Os testes serializam `events` como JSONL UTF-8 na fronteira do subprocesso.
