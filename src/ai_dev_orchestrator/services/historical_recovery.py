"""Coordena a observação antes da reativação histórica transacional."""

from ai_dev_orchestrator.domain.historical import historical_target, validate_historical


def recover_historical(store, run, observer, planner):
    if store.list_active():
        raise ValueError("Outra execução ativa impede recovery histórico")
    target = historical_target(run, store.events(run.id))
    observed = observer.observe(run)
    validate_historical(run, target, observed, planner)
    # Store repete as validações sob transação e compara o record observado.
    return store.reactivate_historical(run, observed, planner)
