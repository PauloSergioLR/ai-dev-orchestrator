"""Observabilidade de operações longas sem acesso à saída do processo."""

from time import sleep

from ai_dev_orchestrator.infrastructure.heartbeat import heartbeat


def test_heartbeat_is_spaced_and_contains_only_phase_and_elapsed_time() -> None:
    messages: list[str] = []

    with heartbeat("Codex", messages.append, interval_seconds=0.01):
        sleep(0.025)

    assert messages[0] == "Codex iniciado"
    assert messages[-1] == "Codex concluído"
    assert any(message == "Codex em execução há 1 min" for message in messages[1:-1])
    assert len(messages) <= 5
