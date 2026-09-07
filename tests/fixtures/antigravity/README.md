# Evidência da CLI 1.1.27

Capturada no Windows em 2026-09-06, usando `CommandRunner` com `shell=False`.

- `help-1.1.27.txt`: saída de `agy --help` (stdout + stderr).
- `review-1.1.27.json`: envelope da chamada sintética com
  `STRUCTURED_REVIEW_SCHEMA`, stdin text, sandbox e slash commands desabilitados.
  Exit code 0 e stderr vazio. Removidos `conversation_id`, `usage` e
  `json_schema` (este último já é definido no código); os campos restantes
  preservam a resposta observada. O SHA é sintético e o resultado é REJECTED.
- `denied-command-1.1.27.json`: projeção sanitizada do erro reproduzido na
  revisão real do PR #49. Exit code 0, status SUCCESS, resposta vazia e ação
  `command` negada, sem `structured_output`. O display_name foi substituído;
  IDs, uso, schema e duração foram omitidos. stderr indicava bloqueio de permissão.

Não representam uma revisão do projeto. O teste opt-in de integração executa
novamente os dois schemas contra a CLI real; os testes unitários reproduzem
o parsing e injetam falhas sem consumir quota.
