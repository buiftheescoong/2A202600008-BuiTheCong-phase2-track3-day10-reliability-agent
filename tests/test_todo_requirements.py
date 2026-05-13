from reliability_lab.cache import ResponseCache


def test_semantic_cache_should_not_false_hit_different_intent() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
    cached, _ = cache.get("Summarize refund policy for 2026 deadline")
    assert cached is None


def test_privacy_query_not_cached_in_memory() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Give me the current account balance for user 123", "Balance: $500")
    cached, _ = cache.get("Give me the current account balance for user 123")
    assert cached is None
