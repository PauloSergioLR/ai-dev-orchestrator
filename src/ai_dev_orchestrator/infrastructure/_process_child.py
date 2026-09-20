"""Bootstrap privado: aguarda vínculo ao Job Object antes de iniciar o comando."""

import json
import os
import signal
import subprocess
import sys
from threading import Thread


def main():
    if os.name != "nt" and len(sys.argv) == 2:
        descriptor = int(sys.argv[1])
        os.set_inheritable(descriptor, False)

        def watch_parent():
            try:
                while os.read(descriptor, 1):
                    pass
            finally:
                # Este bootstrap lidera a sessão criada exclusivamente para o comando.
                os.killpg(os.getpgrp(), signal.SIGKILL)

        Thread(target=watch_parent, daemon=True).start()
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
