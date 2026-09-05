"""Identidade lógica da branch base usada por uma execução."""

from __future__ import annotations


def base_refs_equivalent(
    first: str,
    second: str,
    *,
    remote_name: str,
    base_branch: str,
) -> bool:
    """Compara refs somente pelas formas válidas da base configurada.

    A igualdade literal continua válida para preservar configurações legadas. A
    equivalência entre formatos, porém, fica restrita à branch e ao remote
    configurados; nenhum prefixo de um remote arbitrário é normalizado.
    """
    if first == second:
        return True

    configured_aliases = {
        base_branch,
        f"{remote_name}/{base_branch}",
        f"refs/remotes/{remote_name}/{base_branch}",
        f"refs/heads/{base_branch}",
    }
    return first in configured_aliases and second in configured_aliases
