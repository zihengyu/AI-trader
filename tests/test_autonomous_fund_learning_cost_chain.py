"""A shared critic includes its paid correction chain, never another manager's bill."""
from scripts.autonomous_fund.learning import COST_SCOPE, pair_costs
from scripts.autonomous_fund.risk import number
from scripts.autonomous_fund.strategy import Critique
from .test_autonomous_fund_research_recovery import answer, critic, critique, good


def test_corrected_critic_retains_complete_shared_call_chain_across_cache_reuse(tmp_path):
    fake, budget, now, calls = critic(tmp_path, [answer("No JSON", Critique), good()])
    scope = []
    expected = critique(fake, budget, now, scope)
    source = fake._current_critique_source
    assert source["call_ids"] == scope and len(scope) == 2
    links = {"comparison_cost_scope": COST_SCOPE, "comparison_calls": {
        "memo": {"own": ["manager"], "shared": scope},
        "baseline": {"own": ["shadow"], "shared": scope}}}
    assert pair_costs(links, {"manager": 1, "shadow": 2, scope[0]: 4, scope[1]: 2}, set()) == {
        "memo": number(4), "baseline": number(5)}
    assert critique(fake, budget, now) == expected and fake._current_critique_source["reused"]
    assert fake._current_critique_source["call_ids"] == scope and len(calls) == 2
