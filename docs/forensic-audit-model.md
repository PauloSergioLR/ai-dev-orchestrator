# Auditoria forense: pesquisa, modelo e plano

Base de pesquisa: `df6de8d4114fb7a958e639e202d676fcb5d4788d`.
Pesquisa realizada antes das alterações de implementação, em checkout isolado.
SQLite operacional, execução #96, providers live e notificações reais estão
fora do escopo de execução. Testes usam bancos, processos e remotos locais sintéticos.

## Arquitetura observada

CLI Typer carrega configuração Pydantic/TOML/ORCH_; services coordenam portas
de providers; adapters Git/GitHub/Codex/Antigravity implementam efeitos. SQLite
persiste RunRecord, journal, findings e entregas. Pipeline inicial é imperativo;
resume usa Observer → Planner puro → Executor → Effects e volta a observar.
Work seleciona Ready ou retoma; watch agenda e limita paralelismo. Há recuperação
explícita de contrato, publicação, gates, provider, FAILED e supersessão.

A revisão do histórico incluiu os fixes de Project/preflight/timeout (#95),
permissões (#93), recovery forense (#91), publicação (#90), gates (#89/#88/#86),
notificações (#87), contrato (#79), E2E (#77), paralelismo (#76), CI/recovery
(#75), JSONL (#71), supersessão (#69) e runtime de providers (#68).
As regressões existentes são reaproveitadas; não há reimplementação automática
de correções históricas.

## Máquina de estados real e fronteiras de crash

Em todas as linhas, repositório, Issue, execution_id, branch, caminho, base,
sessão e PR já atribuídos precisam permanecer estáveis. Um SHA atual só avança
por commit comprovado, invalidando CI/review/merge anteriores. UNKNOWN bloqueia.

| Fase persistida | Recursos e efeitos já possíveis | Prova e ação segura após restart | Ação insegura |
| --- | --- | --- | --- |
| PREPARING | Registro existe; branch/worktree podem ter sido criados | Observar registro Git, branch, caminho, base SHA e ausência de divergência antes de preparar/adotar | Adotar qualquer HEAD só porque o caminho existe |
| CODEX_RUNNING | Project pode estar In Progress; chamada pode ter iniciado; diff parcial possível | Sessão explícita comprovada; início previamente tentado sem ID bloqueia | Criar sessão substituta ou usar última sessão |
| TESTING | Diff parcial/final e contrato congelado; gate pode ter terminado sem checkpoint | Revalidar identidade, executar baseline, distinguir erro ambiental/código/drift | Executar contrato candidato; cobrar correção por falha ambiental |
| COMMIT_PENDING | Gates executados; commit pode já existir | HEAD igual ao checkpoint com diff, ou commit limpo com pai direto igual ao checkpoint | Adotar commit arbitrário ou descartar staging |
| PUSH_PENDING | Commit local persistido; push pode estar aplicado | Comparar HEAD remoto: ausente, pai direto ou SHA esperado; confirmar depois | Force push ou reutilizar remoto divergente |
| PR_PENDING | Push aplicado; PR pode existir sem número local | Observar PR único do mesmo repo/base/branch/HEAD/Issue; adotar ou criar | Criar segundo PR ou adotar PR de outra Issue |
| PUBLISHING (legado) | Commit/push/PR parcialmente representados | Reconstruir checkpoints granulares com relações Git diretas | Inferir publicação só pelo SQLite |
| WAITING_CI | PR e HEAD persistidos; Project pode ainda estar stale | Consultar checks obrigatórios do HEAD exato | Tratar falha de transporte como falha de código; aprovar HEAD antigo |
| GEMINI_REVIEWING | CI aprovada; chamada de review pode terminar antes de persistir | Revalidar HEAD; review/findings atômicos; repetir review sem checkpoint | Reaproveitar review de outro HEAD |
| NEEDS_CHANGES | Review rejeitada e findings persistidos | Incrementar tentativa antes de retomar sessão original | Nova sessão; perder findings ou duplicar tentativa de transporte |
| WAITING_CODEX_QUOTA / WAITING_GEMINI_QUOTA / WAITING_PROVIDER | Fase suspensa, classificação e reset/retry persistidos | Respeitar política, horário confiável e mesmas identidades | Recomeçar Issue ou inferir reset sem fuso/data |
| BLOCKED_PROVIDER | Falha exige intervenção | Retry explícito, depois mesmas provas do planner | Retry automático sem limite |
| HUMAN_REQUIRED | Recursos preservados; fase/motivo explicam bloqueio | Flags específicas; merge externo só com identidade completa e commit comprovado | Reinterpretar qualquer bloqueio como autorização |
| MERGE_PENDING / MERGING (legado) | Merge pode ter sido aplicado remotamente | Observar PR/CI/review/local HEAD; adotar merge comprovado ou mutação condicionada ao SHA | Repetir merge sem observar; marcar Done sem prova |
| PROJECT_DONE_PENDING | Merge HEAD e commit persistidos; Project pode já estar Done | Ler status, atualizar somente se necessário, confirmar e completar | Tratar Project inacessível como Not Done |
| APPROVED_AWAITING_ACTION | Review aprovada; auto-merge desativado | Observar merge externo em reconciliação | Habilitar auto-merge implicitamente |
| COMPLETED | Merge e Project comprovados | Cleanup explícito/configurado com provas locais/remotas atuais | Remover branch reutilizada ou caminho externo |
| FAILED | Histórico preservado; pode haver PR publicado | Recuperação histórica explícita quando elegível; não duplicar run publicado | Novo run ignorando publicação antiga |
| SUPERSEDED | Histórico e recursos preservados | Cleanup seguro e auditado; novo run conforme política | Supersessão automática ou prova antiga aplicada a record alterado |

## Modelo de falhas e ameaças

Cada efeito e checkpoint têm duas janelas: antes e depois. Sucesso remoto com
resposta perdida exige observação; não é prova de ausência. Requisições externas
e SQLite não participam de transação conjunta. Entregas de notificação podem
repetir depois de envio sem confirmação; exatamente uma entrega não é garantida.

Entradas não confiáveis incluem Issue/PR, findings, JSON/JSONL, stdout/stderr,
nomes de arquivo, contratos candidatos e ambiente do shell. Limites finitos,
argv sem shell implícito, cwd/PATH corretos, validação de identidade, contenção
de caminhos e diagnósticos sanitizados são requisitos nas fronteiras.

Concorrência deve ser exercitada com barreiras determinísticas: dois resumes,
claim da mesma Issue, review versus mudança de HEAD, supersessão versus checkpoint,
cleanup versus avanço de branch. Heartbeat informativo não é ownership.
Arquivo de PID por si só não prova processo vivo nem libera lock após crash.

Windows exige processos filhos encerrados, resolução .exe/.cmd/.bat, encoding
explícito, paths com espaço/Unicode e locks liberados pelo kernel. Linux deve
exercitar a mesma política; disponibilidade de runtime será registrada, sem
alegar resultado de plataforma não executada.

## Achados que orientam o plano

1. CI inverte exceção de consulta e reprovação comprovada.
2. Ownership não abrange efeitos de resumes concorrentes; lock watch pode ficar órfão.
3. Identidades persistidas, review transacional e provas PREPARING/PR/supersession
   têm lacunas que permitem snapshots antigos ou associação indevida.
4. Cleanup confia em caminhos/SHAs históricos e pode apagar branch remota avançada.
5. Codex tem teto fixo sem configuração e sem distinguir atividade de silêncio.
6. Resolução de executável ignora cwd/PATH fornecidos; gates herdam ambiente
   pytest/Python inadequado; classificação de risco mistura nome e comando.
7. Doctor observa cwd errado e não prova campo/opções de Status do Project.
8. Serialização TOML perde configurações; diagnósticos podem propagar saída bruta.
9. Política de review é lida fora do pacote instalado; verificar wheel real.
10. Proteção de testes contra rede/providers depende de cobertura parcial.

## Plano antes da implementação

Corrigir causas com mudanças delimitadas nas fronteiras existentes, sem novos
providers ou dependências. Cada defeito confirmado recebe regressão sintética.
Preservar schemas antigos e defaults seguros; operações sem prova falham fechado.
Adicionar configuração apenas para limites operacionais justificados. Expandir
CI de Linux/Windows com instalação do artefato quando houver falha confirmada.

Validar baseline, suites focadas, repetições de concorrência/processos, suíte
completa, Ruff, diff --check, build/install local e doctor sem deep em ambiente
sintético isolado. Revisar o diff inteiro novamente antes do relatório final.
O relatório final distinguirá defeitos corrigidos, limitações comprovadas e
validação indisponível; esta modelagem não afirma ausência universal de bugs.
