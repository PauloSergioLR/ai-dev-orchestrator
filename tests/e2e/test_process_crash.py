"""Morte abrupta do controlador não deixa o subprocesso comum continuar no Linux."""

import os
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name == "nt", reason="Lifeline POSIX; Windows usa Job Object coberto por test_process_policies")
def test_sigkill_do_controlador_encerra_grupo_do_comando(tmp_path):
    ready, marker = tmp_path / "ready", tmp_path / "nao-criar"
    child = tmp_path / "child.py"
    child.write_text(
        "import os,pathlib,time,sys\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpgrp()))\n"
        "time.sleep(3)\npathlib.Path(sys.argv[2]).touch()\n", encoding="utf-8",
    )
    controller = subprocess.Popen([
        sys.executable, "-c",
        "import sys; from ai_dev_orchestrator.infrastructure.process import CommandRunner; "
        "CommandRunner(timeout=30).run([sys.executable,*sys.argv[1:]])",
        str(child), str(ready), str(marker),
    ])
    group = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "comando sintético não iniciou"
        group = int(ready.read_text())
        controller.kill()
        controller.wait(timeout=5)
        time.sleep(3.2)
        assert not marker.exists()
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)
        if group is not None:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
