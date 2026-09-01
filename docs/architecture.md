# Arquitetura

O AI Dev Orchestrator começa com uma arquitetura pequena e local-first. A interface de linha de comando é a interface principal, mantendo o trabalho próximo ao repositório e às ferramentas da pessoa desenvolvedora.

As integrações com providers são representadas por fronteiras de adapters para que os providers de implementação permaneçam intercambiáveis. Codex é o provider de implementação pretendido, Gemini é o provider de revisão pretendido e GitHub é o provider de repositório e acompanhamento de trabalho. Essas integrações não são implementadas nesta versão inicial.

A execução futura deve ser isolada e as transições de estado devem ser explícitas. O sistema também deve considerar o uso de tokens e os custos. As versões iniciais não devem realizar merge automático; uma pessoa continua responsável pela decisão final de merge.
