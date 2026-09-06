# Investigação local do reviewer — 2026-09-06

## Evidência

- Base: `main`, `467da97`, PR #53 mergeado. Única alteração local inicial:
  substituir stdin por `-p <prompt>`. Nenhum outro diff foi descartado.
- `Get-Command` e `uv run python`/`shutil.which` localizaram
  `%LOCALAPPDATA%\agy\bin\agy.exe`; `--version` retornou `1.1.27`.
- O nome `agy` já constava no adapter introduzido no commit `5788b4b`
  (Issue #29). Ele é confirmado pela documentação oficial e pela instalação;
  não há evidência de que tenha sido um nome inventado.
- A CLI instalada declara todas as flags usadas. O help capturado está em
  `tests/fixtures/antigravity/help-1.1.27.txt`.
- Chamadas isoladas com schema `{message: string}`: argv e stdin tiveram
  exit code 0, stderr vazio e `status: SUCCESS` com `structured_output` objeto.
  `--print ""` junto a stdin retornou exit code 1, `status: ERROR`, `error`
  textual indicando prompt vazio e nenhum resultado estruturado.
- Chamadas isoladas com `REVIEW_PLAN_SCHEMA` e `STRUCTURED_REVIEW_SCHEMA`
  tiveram exit code 0, stderr vazio e objetos completos em `structured_output`.
  O teste de review pediu `REJECTED` com SHA sintético; não revisou um PR real.
  `response` incluiu texto adicional e metadados de ferramenta, confirmando
  que esse campo não pode substituir o resultado estruturado.

## Diagnóstico e limites

O erro `Executável não encontrado: agy` decorre da resolução do nome pelo
`CommandRunner`, antes de enviar o prompt. A mudança para `-p` não corrige essa
etapa. Nesta sessão, o nome foi encontrado; não dispomos do PATH do processo que
falhou anteriormente para atribuir a falha a uma instalação ou sessão específica.
O caminho configurável permite eliminar essa dependência do PATH.

O erro histórico `SUCCESS` sem `structured_output` não foi reproduzido nos
probes da versão instalada. Não há evidência suficiente para declarar que
`--mode plan`, stdin ou `--json-schema` foram a causa original. O teste antigo
que fabricava essa causalidade foi removido. Ausência do objeto continua sendo
falha recuperável de protocolo, sem extração de JSON de texto livre.

Doctor verificava um conjunto incompleto de flags, separado do runtime.
Agora ambos usam o mesmo preflight e executável configurado. O preflight não
valida credenciais, quota ou comportamento remoto: essas falhas permanecem
responsabilidade da chamada e da validação fail-closed.

Os testes também expuseram um defeito de recovery: o texto de timeout gerado
pelo `CommandRunner` era classificado como `NETWORK_ERROR` remoto e levava a
execução a `FAILED`. Agora erros locais do runner geram `AntigravityError`,
mantendo `GEMINI_REVIEWING` recuperável. A classificação de quota/rate limit
nas respostas do provider foi preservada, incluindo o campo `error` textual
documentado pela CLI.

Nenhum probe executou `orch watch`, alterou SQLite ou retomou a sessão Codex
persistida. A retomada é exercitada pelos testes de recovery no banco temporário;
uma retomada real continua sujeita ao estado remoto e às credenciais naquele momento.

## Fontes primárias

- [Instalação oficial](https://antigravity.google/docs/cli/install/).
- [Contrato headless oficial](https://antigravity.google/docs/cli/headless/).
- Help e respostas observados na CLI local 1.1.27. Identificadores de conversa
  e dados de conta não foram incluídos nas fixtures.
