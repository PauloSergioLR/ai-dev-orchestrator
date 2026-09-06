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

## Diagnóstico inicial e limites dos probes sintéticos

O erro `Executável não encontrado: agy` decorre da resolução do nome pelo
`CommandRunner`, antes de enviar o prompt. A mudança para `-p` não corrige essa
etapa. Nesta sessão, o nome foi encontrado; não dispomos do PATH do processo que
falhou anteriormente para atribuir a falha a uma instalação ou sessão específica.
O caminho configurável permite eliminar essa dependência do PATH.

O erro histórico `SUCCESS` sem `structured_output` não foi reproduzido nos primeiros
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

## Reprodução com o dossier real após o PR #54

Após novo relato de falha, a chamada real de revisão foi autorizada pelo usuário
e repetida isoladamente, sem executar o supervisor ou persistir seu resultado.
O SQLite foi aberto com `mode=ro` somente para ler a identidade da execução.

- Issue #45, PR #49, HEAD `47dd5dc88eb2953eb600a3fee74841d2533c9416`.
- O planner recebeu 73.195 caracteres de prompt, no worktree real, usando
  a política anterior e `REVIEW_PLAN_SCHEMA`.
- A falha foi reproduzida duas vezes: exit code 0, `status: SUCCESS`,
  `response` vazia, ausência de `structured_output` e uma ação `command`
  em `denied_actions`. stderr indicava negação de permissão.
- A documentação oficial descreve essa negação em headless: comandos que
  exigem aprovação podem ser negados e a CLI ainda encerrar com código 0.
- A política anterior proibia mutações, mas não proibia executar verificações
  de leitura/testes. Os probes simples pediam expressamente que nenhuma ferramenta
  fosse usada; por isso não cobriam o comportamento do dossier real.

A correção explicita análise exclusivamente do dossier, sem executar comandos
ou consultar ferramentas externas. Evidência insuficiente deve produzir lacuna
e rejeição, nunca sucesso presumido. O adapter identifica `denied_actions`
antes de aceitar o objeto estruturado, sem registrar display_name ou stderr.
Nenhuma configuração de permissão da CLI foi ampliada.

Com essa política, ambas as etapas reais passaram a retornar `structured_output`
sem ações negadas nem stderr. A primeira análise final retornou APPROVED com
finding bloqueante e foi corretamente recusada pelo parser. As severidades
configuradas não eram comunicadas ao modelo; agora o pipeline as inclui nas duas
etapas com a regra explícita de coerência do verdict. A validação permanece estrita.

A repetição final com a política completa concluiu as duas chamadas reais:
exit code 0, SUCCESS, objeto estruturado, nenhuma ação negada e stderr vazio.
`parse_review_plan`, `parse_structured_review` e as revalidações de HEAD passaram;
o resultado foi APPROVED sem findings para o mesmo HEAD. Esse resultado foi
usado somente como diagnóstico, sem ser persistido ou autorizar merge.
Após a chamada, a execução continuava em GEMINI_REVIEWING com `review_verdict`
e `reviewed_head_sha` nulos, e o worktree original permaneceu limpo.

Nenhum diagnóstico executou `orch watch`, alterou SQLite ou retomou a sessão
Codex persistida. A retomada é exercitada pelos testes no banco temporário;
uma retomada real continua sujeita ao estado remoto e às credenciais naquele momento.

## Fontes primárias

- [Instalação oficial](https://antigravity.google/docs/cli/install/).
- [Contrato headless oficial](https://antigravity.google/docs/cli/headless/).
- Help e respostas observados na CLI local 1.1.27. Identificadores de conversa
  e dados de conta não foram incluídos nas fixtures.
