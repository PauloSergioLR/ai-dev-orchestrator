# Arquitetura

As execuções futuras serão isoladas em Git worktrees próprios. A remoção de um worktree não removerá automaticamente a branch correspondente.

O AI Dev Orchestrator começa com uma arquitetura pequena e local-first. A interface de linha de comando é a interface principal, mantendo o trabalho próximo ao repositório e às ferramentas da pessoa desenvolvedora.

As integrações com providers são representadas por fronteiras de adapters para que os providers de implementação permaneçam intercambiáveis. Codex é o provider de implementação pretendido, Gemini é o provider de revisão pretendido e GitHub é o provider de repositório e acompanhamento de trabalho. A leitura de issues e de itens do GitHub Project usa o GitHub CLI autenticado e é estritamente somente leitura.

Os itens do Project são convertidos para um modelo de domínio antes do consumo. Um item é elegível apenas se for uma Issue do repositório configurado e seu status for exatamente igual a `github.ready_status`; essa regra não escolhe nem ordena issues.

A execução futura deve ser isolada e as transições de estado devem ser explícitas. O sistema também deve considerar o uso de tokens e os custos. As versões iniciais não devem realizar merge automático; uma pessoa continua responsável pela decisão final de merge.
