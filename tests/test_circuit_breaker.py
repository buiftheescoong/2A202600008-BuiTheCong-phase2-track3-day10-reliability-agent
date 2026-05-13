import time

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


def test_circuit_opens_and_fails_fast_after_threshold() -> None:
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=60)
    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    assert not breaker.allow_request()


def test_circuit_half_open_closes_after_success() -> None:
    breaker = CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=0.01)
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    time.sleep(0.02)
    assert breaker.allow_request()
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


def test_call_raises_when_open() -> None:
    breaker = CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=60)
    breaker.record_failure()
    try:
        breaker.call(lambda: "never")
    except CircuitOpenError:
        pass
    else:
        raise AssertionError("open circuit should fail fast")
