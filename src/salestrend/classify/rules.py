"""标题归一化 + 品类规则引擎（读 rules/*.yaml）。

输出三态：match / exclude / review，并带命中规则 id，保证每条判定都能指到规则文件的第几条。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_NON_WORD = re.compile(r"[^a-z0-9]+")


def _strip_possessive(s: str) -> str:
    s = re.sub(r"['’]s\b", "", s)  # women's -> women
    # womens -> women：丢了撇号的所有格只处理已知词，泛化成「去词尾 s」会把 dress 变成 dres
    return re.sub(r"\b(women|men|girl|kid)s\b", r"\1", s)


_STEPS = {
    "lowercase": str.lower,
    "strip_possessive": _strip_possessive,
    "punct_to_space": lambda s: _NON_WORD.sub(" ", s),
    "collapse_spaces": lambda s: " ".join(s.split()),
}


def normalize(title: str, steps: list[str] | None = None) -> str:
    s = title
    for step in steps or list(_STEPS):
        s = _STEPS[step](s)
    return s


def tokens(title: str, steps: list[str] | None = None) -> frozenset[str]:
    return frozenset(normalize(title, steps).split())


@dataclass(frozen=True)
class Decision:
    decision: str  # match | exclude | review
    rule_id: str
    warnings: tuple[str, ...] = ()


def _cond_ok(when: dict, toks: frozenset[str], category: str | None) -> bool:
    if "all_token" in when and not set(when["all_token"]) <= toks:
        return False
    if "any_token" in when and not (set(when["any_token"]) & toks):
        return False
    if "none_token" in when and set(when["none_token"]) & toks:
        return False
    return not ("category_not_in" in when and (category or "").lower() in when["category_not_in"])


@dataclass
class RuleSet:
    class_slug: str
    rule_version: str
    default: bool
    description: str
    normalize_steps: list[str]
    rules: list[dict]
    checks: list[dict] = field(default_factory=list)
    source_file: str = ""

    @classmethod
    def load(cls, path: Path) -> RuleSet:
        d = yaml.safe_load(path.read_text())
        return cls(
            class_slug=d["class_slug"],
            rule_version=d["rule_version"],
            default=bool(d.get("default", False)),
            description=d.get("description", ""),
            normalize_steps=d["normalize"],
            rules=d["rules"],
            checks=d.get("consistency_checks", []),
            source_file=path.name,
        )

    def classify_title(self, title: str, category: str | None = None) -> Decision:
        toks = tokens(title, self.normalize_steps)
        warnings = tuple(c["id"] for c in self.checks if _cond_ok(c["when"], toks, category))
        for rule in self.rules:
            if _cond_ok(rule.get("when", {}), toks, category):
                return Decision(rule["decision"], rule["id"], warnings)
        return Decision("exclude", "NO_RULE", warnings)

    def classify_product(self, titles: list[str], category: str | None = None) -> Decision:
        """一个 product_id 的全部标题写法一起判：写法之间判定不一致时降级为 review。"""
        decisions = [self.classify_title(t, category) for t in titles]
        kinds = {d.decision for d in decisions}
        warnings = tuple(sorted({w for d in decisions for w in d.warnings}))
        if len(kinds) > 1:
            return Decision("review", "VARIANT_CONFLICT", warnings)
        most_common = Counter(d.rule_id for d in decisions).most_common(1)[0][0]
        return Decision(decisions[0].decision, most_common, warnings)


# ---------- v3：中心词 + 长度槽（rule_type: head_slot）----------
#
# 词集合引擎丢了词序，分不清 "Maxi Dress Belt"（卖腰带）和 "Maxi Dress with Belt"（卖裙子）。
# head_slot 引擎保留 token 顺序，按「截断 → 用途排除 → 中心词 → 长度槽」逐步判。
# 引擎里不出现任何具体单词，词表全在 yaml，加词不改代码。

_NON_WORD_UNICODE = re.compile(r"[\W_]+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿]")

# v3 专用步骤单独放一张表：不能并进 _STEPS，否则 normalize(title) 的默认步骤会变，
# 而 store 写 dim_product.canonical_title 用的正是默认步骤
_STEPS_V3 = {
    **_STEPS,
    "punct_to_space_unicode": lambda s: _NON_WORD_UNICODE.sub(" ", s),
}
_TOKEN_STEPS = {"split_compounds", "cjk_segment"}


def _cjk_segment(tok: str, words: list[str], expand: dict[str, list[str]]) -> list[str]:
    """CJK 串内从左到右取最长匹配；匹配不上的字丢掉。只是子串词表，不是分词器。"""
    out, i = [], 0
    by_len = sorted(words, key=len, reverse=True)
    while i < len(tok):
        hit = next((w for w in by_len if tok.startswith(w, i)), None)
        if hit:
            out += expand.get(hit, [hit])
            i += len(hit)
        else:
            i += 1
    return out


def _has_phrase(toks: list[str], phrase: str) -> bool:
    p = phrase.split()
    return any(toks[i:i + len(p)] == p for i in range(len(toks) - len(p) + 1))


def _drop_phrases(toks: list[str], phrases: list[str]) -> list[str]:
    """从 token 流里删掉命中的短语（长短语优先），用于剔除不该进长度槽的属性短语。"""
    ps = sorted((p.split() for p in phrases), key=len, reverse=True)
    out, i = [], 0
    while i < len(toks):
        hit = next((p for p in ps if toks[i:i + len(p)] == p), None)
        if hit:
            i += len(hit)
        else:
            out.append(toks[i])
            i += 1
    return out


@dataclass(frozen=True)
class Trace:
    """v3 判定的中间结果，单测和排障用：判错时能看出卡在哪一步。"""
    tokens: tuple[str, ...]
    front: tuple[str, ...]
    tail: tuple[str, ...]
    head: str | None
    head_kind: str | None   # target | ambiguous | other_garment | non_wearable | None
    slots: frozenset[str]   # strong / weak / conflict 中命中的档


@dataclass
class HeadSlotRuleSet(RuleSet):
    """rule_type: head_slot。对外接口与 RuleSet 一致（classify_title / classify_product），
    store、报告、API 不用区分版本类型。self.rules 存长度槽判定表，API 展示规则时照样可读。"""
    cfg: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict, source_file: str) -> HeadSlotRuleSet:
        return cls(
            class_slug=d["class_slug"],
            rule_version=d["rule_version"],
            default=bool(d.get("default", False)),
            description=d.get("description", ""),
            normalize_steps=d["normalize"],
            rules=d["rules"],
            checks=d.get("consistency_checks", []),
            source_file=source_file,
            cfg=d,
        )

    def tokenize(self, title: str) -> list[str]:
        # 字符串级步骤在前、token 级步骤（split_compounds / cjk_segment）在后；
        # 拼错的步骤名直接 KeyError，不静默跳过——规则文件写错要在加载时就暴露
        s = title
        for step in self.normalize_steps:
            if step not in _TOKEN_STEPS:
                s = _STEPS_V3[step](s)
        toks = s.split()
        if "split_compounds" in self.normalize_steps:
            comp = self.cfg.get("compounds", {})
            toks = [x for t in toks for x in comp.get(t, t).split()]
        if "cjk_segment" in self.normalize_steps:
            words, expand = self.cfg.get("cjk_words", []), self.cfg.get("cjk_expand", {})
            toks = [x for t in toks
                    for x in (_cjk_segment(t, words, expand) if _CJK.search(t) else [t])]
        return toks

    def trace(self, title: str) -> Trace:
        toks = self.tokenize(title)
        head_cfg = self.cfg["head"]
        kinds = ("target", "ambiguous", "other_garment", "non_wearable")
        lex = {kind: set(head_cfg.get(kind, [])) for kind in kinds}
        any_noun = set().union(*lex.values())

        # 截断词前面已经出现过品类词才截：否则 "Mother and Daughter Maxi Dress" 会在 and 处
        # 截出一个没有中心词的前半段。前面还没有品类词时跳过这个截断词，继续往后找
        cut = set(self.cfg.get("cut_words", []))
        k = next((i for i, t in enumerate(toks) if t in cut and any_noun & set(toks[:i])), len(toks))
        front, tail = toks[:k], toks[k:]

        head, head_kind = None, None
        for t in reversed(front):  # 中心词 = 前半段里最后一个品类词（英文修饰语在前、中心词在后）
            kind = next((kd for kd in kinds if t in lex[kd]), None)
            if kind:
                head, head_kind = t, kind
                break

        # 袖长等属性短语先从长度槽里拿掉：short sleeve 的 short 修饰袖子，不是裙长
        slot_toks = _drop_phrases(front, self.cfg.get("attribute_phrases", []))
        slots = frozenset(band for band, phrases in self.cfg.get("length", {}).items()
                          if any(_has_phrase(slot_toks, p) for p in phrases))
        return Trace(tuple(toks), tuple(front), tuple(tail), head, head_kind, slots)

    def _use_excluded(self, tail: tuple[str, ...]) -> bool:
        ue = self.cfg.get("use_exclusion")
        if not ue or not tail:
            return False
        cut, lead, words = set(self.cfg.get("cut_words", [])), set(ue["tail_lead"]), set(ue["words"])
        # 尾巴里可能有多段（"... with Belt for Dolls"），按截断词再切开，只看以 for 开头的段
        segs: list[list[str]] = []
        for t in tail:
            if t in cut or not segs:
                segs.append([t])
            else:
                segs[-1].append(t)
        return any(seg[0] in lead and words & set(seg[1:]) for seg in segs)

    def _warnings(self, tr: Trace, category: str | None) -> tuple[str, ...]:
        out = []
        for c in self.checks:
            w = c["when"]
            if "head_in" in w and tr.head_kind != w["head_in"]:
                continue
            if "category_not_in" in w and (category or "").lower() in w["category_not_in"]:
                continue
            out.append(c["id"])
        return tuple(out)

    def classify_title(self, title: str, category: str | None = None) -> Decision:
        tr = self.trace(title)
        warnings = self._warnings(tr, category)
        if self._use_excluded(tr.tail):
            ue = self.cfg["use_exclusion"]
            return Decision(ue.get("decision", "exclude"), ue["id"], warnings)
        hc = self.cfg["head"]
        if tr.head_kind != "target":
            kind = tr.head_kind or "missing"
            return Decision(hc["decisions"][kind], hc["ids"][kind], warnings)
        for rule in self.rules:
            when = rule.get("when", {})
            if all((band in tr.slots) == want for band, want in when.items()):
                return Decision(rule["decision"], rule["id"], warnings)
        return Decision("exclude", "NO_RULE", warnings)


def load_ruleset(path: Path) -> RuleSet:
    """按 rule_type 分派引擎；没写 rule_type 的旧 yaml（v1/v2）原样走 RuleSet。"""
    d = yaml.safe_load(path.read_text())
    if d.get("rule_type") == "head_slot":
        return HeadSlotRuleSet.from_dict(d, path.name)
    return RuleSet.load(path)


def load_rulesets(rules_dir: Path) -> dict[tuple[str, str], RuleSet]:
    out = {}
    for p in sorted(rules_dir.glob("*.yaml")):
        rs = load_ruleset(p)
        out[(rs.class_slug, rs.rule_version)] = rs
    return out


def default_versions(rulesets: dict[tuple[str, str], RuleSet]) -> dict[str, str]:
    out: dict[str, str] = {}
    for (slug, ver), rs in rulesets.items():
        if rs.default or slug not in out:
            out[slug] = ver
    return out
