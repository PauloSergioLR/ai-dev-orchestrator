"""Identidade lógica da branch base usada por uma execução."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class PreparedBase:
    """Ref lógica sincronizada e commit imutável que originará o run."""

    ref: str
    sha: str

    def __post_init__(self) -> None:
        if not self.ref or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", self.sha):
            raise ValueError("Base preparada exige ref e SHA de commit válidos")


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
