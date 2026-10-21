"""规则 v3（中心词 + 长度槽）的对抗测试。

32 条标题专挑 v1 词集合引擎的盲区：词序（Maxi Dress Belt）、截断（with / for）、
没写 maxi 的长款（Floor-Length Gown）、粘连词（Maxikleid）、长度冲突（Midi to Maxi）、CJK。
"""

from collections import defaultdict

import pytest

from salestrend.classify.rules import HeadSlotRuleSet
from salestrend.ingest.pipeline import clean

# (标题, 原始类目, 期望判定, 期望中心词, 期望规则 id)
CASES = [
    # ---- match ----
    ("Maxi Denim Dress", "dress", "match", "dress", "S1_strong"),
    ("Women's Maxi-Dress Floral", "dress", "match", "dress", "S1_strong"),
    ("MAXI DRESS WHITE", "dress", "match", "dress", "S1_strong"),
    ("Long Maxi Summer Dress", "dress", "match", "dress", "S1_strong"),        # strong 压过 weak
    ("Floor-Length Chiffon Evening Gown", "dress", "match", "gown", "S1_strong"),
    ("Boho Floral Maxi Sundress", "dress", "match", "sundress", "S1_strong"),
    ("Ankle-Length Wrap Dress", "dress", "match", "dress", "S1_strong"),
    ("Sommer Maxikleid Blumen", "dress", "match", "kleid", "S1_strong"),      # 粘连词拆分
    ("Maxi Dress with Belt", "dress", "match", "dress", "S1_strong"),         # with 之后不找中心词
    ("Floral Pattern Maxi Dress", "dress", "match", "dress", "S1_strong"),    # pattern 不是中心词
    ("Doll Collar Maxi Dress", "dress", "match", "dress", "S1_strong"),       # doll 不在 for 尾段
    # ---- exclude ----
    ("Maxi Skirt", "skirt", "exclude", "skirt", "H1_head_other_garment"),
    ("Long Floral Skirt", "skirt", "exclude", "skirt", "H1_head_other_garment"),
    ("Midi Dress Blue", "dress", "exclude", "dress", "S4_conflict_only"),
    ("Summer Mini Dress", "dress", "exclude", "dress", "S4_conflict_only"),
    ("Casual Dress", "dress", "exclude", "dress", "S0_no_length"),
    ("T-shirt Dress", "shirt", "exclude", "dress", "S0_no_length"),
    ("White Oxford Dress Shirt", "shirt", "exclude", "shirt", "H1_head_other_garment"),
    ("Maxi Dress Belt", "dress", "exclude", "belt", "H3_head_non_wearable"),
    ("Maxi Dress Sewing Pattern", "dress", "exclude", "pattern", "H3_head_non_wearable"),
    ("Maxi Dress for 18 Inch Doll", "dress", "exclude", "dress", "U1_non_wearable_use"),
    # ---- review ----
    ("Elegant Long Dress", "dress", "review", "dress", "S3_weak_only"),
    ("Midi to Maxi Convertible Dress", "dress", "review", "dress", "S2_strong_with_conflict"),
    ("Floral Kaftan", "dress", "review", "kaftan", "H2_head_ambiguous"),
    ("碎花长裙", "dress", "review", "裙", "H2_head_ambiguous"),                 # 长=weak，裙不分连衣裙/半身裙
    # ---- 第二轮：袖长不进长度槽 ----
    ("Short Sleeve Maxi Dress", "dress", "match", "dress", "S1_strong"),
    ("Long Sleeve Maxi Dress", "dress", "match", "dress", "S1_strong"),
    ("Long Sleeve Dress", "dress", "exclude", "dress", "S0_no_length"),      # 去掉袖长后没有长度词
    ("长袖连衣裙", "dress", "exclude", "连衣裙", "S0_no_length"),
    ("Short Maxi Dress", "dress", "review", "dress", "S2_strong_with_conflict"),  # short 修饰裙子本身
    # ---- 第二轮：截断词前面没有品类词时不截 ----
    ("Mother and Daughter Maxi Dress", "dress", "match", "dress", "S1_strong"),
    ("Maxi Dress and Cardigan Set", "dress", "match", "dress", "S1_strong"),  # and 前已有中心词，截断
]


@pytest.fixture(scope="module")
def v3(rulesets):
    return rulesets[("maxi-dress", "v3")]


def test_v3_loaded_as_shadow_version(rulesets, v3):
    assert isinstance(v3, HeadSlotRuleSet) and not v3.default
    assert rulesets[("maxi-dress", "v1")].default        # 默认版本仍是 v1
    assert not isinstance(rulesets[("maxi-dress", "v1")], HeadSlotRuleSet)


@pytest.mark.parametrize("title,cat,decision,head,rule_id", CASES, ids=[c[0] for c in CASES])
def test_adversarial(v3, title, cat, decision, head, rule_id):
    d = v3.classify_title(title, cat)
    assert (d.decision, d.rule_id) == (decision, rule_id)
    assert v3.trace(title).head == head


def test_t_shirt_dress_triggers_c1(v3):
    assert v3.classify_title("T-shirt Dress", "shirt").warnings == ("C1_category_conflict",)
    assert v3.classify_title("Maxi Dress", "dress").warnings == ()
    assert v3.classify_title("Maxi Skirt", "dress").warnings == ()   # 中心词不是 dress 不告警


@pytest.mark.parametrize("title,expected", [
    ("Women's Maxi-Dress Floral", ["women", "maxi", "dress", "floral"]),
    ("Womens Maxidress", ["women", "maxi", "dress"]),
    ("Shirtdress", ["shirt", "dress"]),
    ("Slipdress", ["slip", "dress"]),
    ("Sundress", ["sundress"]),                    # sundress 不拆
    ("碎花长裙", ["长", "裙"]),
    ("长袖连衣裙", ["长袖", "连衣裙"]),
    ("花柄ワンピース", ["ワンピース"]),
])
def test_tokenize(v3, title, expected):
    assert v3.tokenize(title) == expected


def test_truncation_needs_noun_before_cut_word(v3):
    assert v3.trace("Mother and Daughter Maxi Dress").tail == ()            # and 前没有品类词，不截
    assert v3.trace("Maxi Dress and Cardigan Set").front == ("maxi", "dress")


def test_truncation_keeps_tail(v3):
    tr = v3.trace("Maxi Dress with Belt for Dogs")
    assert tr.front == ("maxi", "dress") and tr.tail == ("with", "belt", "for", "dogs")
    assert v3.classify_title("Maxi Dress with Belt for Dogs").rule_id == "U1_non_wearable_use"
    # with 段里的 doll 不触发用途排除：只看 for 开头的段
    assert v3.classify_title("Maxi Dress with Doll Print").decision == "match"


def test_variant_conflict_still_degrades_to_review(v3):
    # classify_product 继承自 RuleSet：同一商品不同写法判定不一致 → review
    assert v3.classify_product(["Maxi Dress", "Maxi Dress Belt"], "dress").decision == "review"


# ---------- 样例回归：复用 conftest 的 events（读 data/sample-sales-data.xlsx）----------

def _decisions(events, rs):
    titles, cats = defaultdict(set), {}
    for e in clean(events):
        titles[e.product_id].add(e.title_raw)
        cats[e.product_id] = e.category_raw
    return {pid: rs.classify_product(sorted(t), cats[pid]) for pid, t in titles.items()}


def test_sample_v3_match_set_equals_v1(events, rulesets, v3):
    d1 = _decisions(events, rulesets[("maxi-dress", "v1")])
    d3 = _decisions(events, v3)
    m1 = {p for p, x in d1.items() if x.decision == "match"}
    m3 = {p for p, x in d3.items() if x.decision == "match"}
    assert m3 == m1 == set(range(2001, 2013))
    assert d3[2017].decision == "review"                      # Elegant Long Dress
    assert d3[2027].decision == "exclude"                     # Maxi Skirt
    assert "C1_category_conflict" in d3[2018].warnings        # T-shirt Dress / shirt


# ---------- v1 对照：只统计、不断言（数字写进 docs/classification-v3.md）----------

def v1_errors(rulesets) -> list[tuple[str, str, str]]:
    v1 = rulesets[("maxi-dress", "v1")]
    return [(t, exp, got) for t, cat, exp, _, _ in CASES
            if (got := v1.classify_title(t, cat).decision) != exp]


def test_report_v1_errors_on_adversarial_set(rulesets):
    errs = v1_errors(rulesets)
    print(f"\nv1 在对抗集上判错 {len(errs)}/{len(CASES)} 条：")
    for t, exp, got in errs:
        print(f"  {t!r}: 期望 {exp}，v1 判 {got}")
