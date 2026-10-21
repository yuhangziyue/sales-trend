from collections import defaultdict

import pytest

from salestrend.classify.rules import normalize, tokens
from salestrend.ingest.pipeline import clean

# 7 个有两种写法的 product_id：每对写法必须得到同一个判定
VARIANTS = {
    2001: ("Maxi Dress Floral", "maxi dress floral"),
    2002: ("Summer Maxi Dress", "SUMMER MAXI DRESS"),
    2003: ("Red Maxi Dress", "Red Maxi-Dress"),
    2004: ("Blue Maxi Dress", "blue maxi  dress"),
    2005: ("Floral Maxi Dress", "Floral long maxi dress"),
    2008: ("Denim Maxi Dress", "Maxi Denim Dress"),
    2010: ("Women's Maxi-Dress Floral", "Womens Maxi Dress Floral"),
}


@pytest.mark.parametrize("a,b", [(a, b) for a, b in VARIANTS.values() if a.lower() != "floral maxi dress"
                                 and a != "Denim Maxi Dress"])
def test_string_normalization_merges_variants(a, b):
    assert normalize(a) == normalize(b)


def test_word_order_handled_by_token_set_not_string():
    assert normalize("Denim Maxi Dress") != normalize("Maxi Denim Dress")
    assert tokens("Denim Maxi Dress") == tokens("Maxi Denim Dress")
    assert "maxi dress" not in normalize("Maxi Denim Dress")  # LIKE '%maxi dress%' 会漏掉这一件


def test_possessive_does_not_break_dress():
    assert normalize("Women's Maxi-Dress Floral") == "women maxi dress floral"
    assert "dress" in tokens("Dress")  # 不能把词尾 s 一律去掉


@pytest.mark.parametrize("pid", sorted(VARIANTS))
def test_variant_pairs_get_same_decision(pid, rulesets):
    rs = rulesets[("maxi-dress", "v1")]
    a, b = VARIANTS[pid]
    assert rs.classify_title(a, "dress").decision == rs.classify_title(b, "dress").decision == "match"


def _decisions(events, rs):
    titles, cats = defaultdict(set), {}
    for e in clean(events):
        titles[e.product_id].add(e.title_raw)
        cats[e.product_id] = e.category_raw
    return {pid: rs.classify_product(sorted(t), cats[pid]) for pid, t in titles.items()}


def test_exactly_12_products_match(events, rulesets):
    d = _decisions(events, rulesets[("maxi-dress", "v1")])
    assert sorted(p for p, x in d.items() if x.decision == "match") == list(range(2001, 2013))


def test_boundary_products(events, rulesets):
    d = _decisions(events, rulesets[("maxi-dress", "v1")])
    assert (d[2027].decision, d[2027].rule_id) == ("exclude", "R2_other_garment")   # Maxi Skirt
    assert (d[2017].decision, d[2017].rule_id) == ("review", "R3_long_ambiguous")   # Elegant Long Dress
    assert d[2018].decision == "exclude" and "C1_category_conflict" in d[2018].warnings  # T-shirt Dress / shirt


def test_v2_shadow_version_only_differs_on_long_dress(events, rulesets):
    v1 = _decisions(events, rulesets[("maxi-dress", "v1")])
    v2 = _decisions(events, rulesets[("maxi-dress", "v2")])
    diff = {p for p in v1 if v1[p].decision != v2[p].decision}
    assert diff == {2017} and v2[2017].decision == "match"


def test_variant_conflict_degrades_to_review(rulesets):
    rs = rulesets[("maxi-dress", "v1")]
    assert rs.classify_product(["Maxi Dress", "Maxi Skirt"], "dress").decision == "review"
