"""Exclusão de efeitos por locks do kernel, liberados inclusive após crash."""

from __future__ import annotations

import os
from pathlib import Path
from threading import local


class OwnershipError(Exception):
    """Outro processo ou thread já possui o recurso solicitado."""


_owned = local()


class _ExclusiveOwnership:
    """Context manager sem gerador, compatível com exceções dataclass frozen."""

    def __init__(self, path: Path, *, reentrant: bool) -> None:
        self.path = path.resolve()
        self.key = (os.getpid(), os.path.normcase(str(self.path)))
        self.reentrant = reentrant
        self.handle = None

    def __enter__(self) -> None:
        owned = getattr(_owned, "paths", None)
        if owned is None:
            owned = _owned.paths = set()
        if self.key in owned:
            if not self.reentrant:
                raise OwnershipError("Recurso já está sob controle de outra operação")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            # msvcrt exige ao menos um byte, também no primeiro uso do arquivo.
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(bytes([0]))
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise OwnershipError("Recurso já está sob controle de outra operação") from error
        self.handle = handle
        owned.add(self.key)

    def __exit__(self, *_exc) -> None:
        if self.handle is not None:
            try:
                # Fechar o descritor libera o lock em ambas as plataformas.
                self.handle.close()
            finally:
                _owned.paths.remove(self.key)
                self.handle = None


def exclusive_ownership(path: Path, *, reentrant: bool = True) -> _ExclusiveOwnership:
    """Reserva sem espera; reentrância é restrita ao mesmo processo e thread.

    O arquivo permanece no disco: removê-lo permitiria abrir outro inode enquanto
    um concorrente ainda segura o anterior. Existência, PID e idade não são prova
    de ownership; somente o lock do kernel autoriza a seção crítica.
    """
    return _ExclusiveOwnership(path, reentrant=reentrant)
