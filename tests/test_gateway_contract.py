from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def test_gateway_returns_response_with_route_reason() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_seconds=1)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, ResponseCache(60, 0.5))
    result = gateway.complete("hello world")
    assert result.text
    assert result.route == "primary:primary"
    assert result.latency_ms > 0


def test_gateway_uses_fallback_when_primary_circuit_opens() -> None:
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    backup = FakeLLMProvider("backup", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breakers = {
        "primary": CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=60),
        "backup": CircuitBreaker("backup", failure_threshold=1, reset_timeout_seconds=60),
    }
    gateway = ReliabilityGateway([primary, backup], breakers, None)
    result = gateway.complete("hello world")
    assert result.route == "fallback:backup"
    assert result.provider == "backup"
    assert breakers["primary"].state.value == "open"


def test_gateway_returns_static_fallback_when_all_providers_fail() -> None:
    primary = FakeLLMProvider("primary", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    backup = FakeLLMProvider("backup", fail_rate=1.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breakers = {
        "primary": CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=60),
        "backup": CircuitBreaker("backup", failure_threshold=1, reset_timeout_seconds=60),
    }
    gateway = ReliabilityGateway([primary, backup], breakers, None)
    result = gateway.complete("hello world")
    assert result.route == "static_fallback"
    assert result.error is not None
