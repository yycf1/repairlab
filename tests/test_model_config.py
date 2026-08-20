"""牌价估算：子串匹配顺序、缓存命中计价、未知模型不估算。"""
from easycode.model_config import cost_of, pricing_for_model


def test_pricing_matches_most_specific_first():
    assert pricing_for_model("deepseek-reasoner")["prompt"] == 4.0
    assert pricing_for_model("deepseek-chat")["prompt"] == 2.0
    assert pricing_for_model("qwen3.8-flash")["prompt"] == 0.8  # 泛 qwen 条目兜底
    assert pricing_for_model("unknown-model") is None


def test_cost_of_bills_cached_tokens_at_cache_rate():
    # 1000 输入（其中 400 命中缓存 @0.2）+ 500 输出 @2.0：0.0006 + 0.00008 + 0.001
    pricing = {"prompt": 2.0, "cached": 0.2, "completion": 2.0}
    cost = cost_of(pricing, 1000, 500, 400)
    assert abs(cost - (600 * 2.0 + 400 * 0.2 + 500 * 2.0) / 1e6) < 1e-12


def test_cost_of_clamps_invalid_cached_tokens():
    pricing = {"prompt": 2.0, "cached": 0.2, "completion": 2.0}
    assert cost_of(pricing, 100, 10, 999) == cost_of(pricing, 100, 10, 100)  # 缓存数不会超过输入数
    assert cost_of(pricing, 100, 10, None) == cost_of(pricing, 100, 10, 0)
