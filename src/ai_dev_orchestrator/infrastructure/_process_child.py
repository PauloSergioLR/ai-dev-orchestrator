"""Bootstrap privado: aguarda vínculo ao Job Object antes de iniciar o comando."""

import json
import subprocess
import sys


def main():
    header = json.loads(sys.stdin.buffer.readline())
    payload = sys.stdin.buffer.read()
    try:
        result = subprocess.run(header["command"], input=payload if header["has_input"] else None,
                                stdin=None if header["has_input"] else subprocess.DEVNULL,
                                shell=False, check=False)
    except OSError:
        # Não expõe argumentos nem entrada do provider em diagnósticos locais.
        sys.stderr.write("Falha local ao iniciar o comando.\n")
        return 127
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
