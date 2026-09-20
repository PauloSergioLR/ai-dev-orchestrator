"""Instala dependências do lock e wheel offline; verifica fora do checkout."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


def main() -> None:
    wheel = Path(sys.argv[1]).resolve()
    uv = shutil.which("uv")
    if uv is None or not wheel.is_file():
        raise SystemExit("Informe um wheel existente e disponibilize uv no PATH")
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("ORCH_")
        and key not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    }
    environment["UV_OFFLINE"] = "1"
    with tempfile.TemporaryDirectory(prefix="orch-package-") as folder:
        root = Path(folder)
        venv = root / "venv"
        subprocess.run([uv, "venv", "--python", sys.executable, str(venv)],
                       cwd=root, env=environment, check=True, timeout=60)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        # O sync já armazena os artefatos do lock, mas pode não consultar o índice.
        # Reutiliza essas URLs exatas sem resolver dependências pelo índice offline.
        subprocess.run([
            uv, "sync", "--frozen", "--no-dev", "--no-install-project",
            "--project", str(Path(__file__).resolve().parents[1]),
            "--python", str(python),
        ], cwd=root, env={**environment, "UV_PROJECT_ENVIRONMENT": str(venv)},
            check=True, timeout=60)
        subprocess.run([uv, "pip", "install", "--no-deps", "--python", str(python), str(wheel)],
                       cwd=root, env=environment, check=True, timeout=60)
        # Verifica o metadata do wheel; --no-deps não pode ocultar requisito ausente.
        subprocess.run([uv, "pip", "check", "--python", str(python)],
                       cwd=root, env=environment, check=True, timeout=60)
        subprocess.run([
            str(python), "-I", "-c",
            "from ai_dev_orchestrator.services.review import load_review_policy; "
            "assert 'Atue somente como reviewer' in load_review_policy(); "
            "from ai_dev_orchestrator.cli import app; "
            "print('Pacote instalado e política de review disponíveis fora do checkout')",
        ], cwd=root, env=environment, check=True, timeout=30)


if __name__ == "__main__":
    main()
