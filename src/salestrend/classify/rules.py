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


def load_rulesets(rules_dir: Path) -> dict[tuple[str, str], RuleSet]:
    out = {}
    for p in sorted(rules_dir.glob("*.yaml")):
        rs = RuleSet.load(p)
        out[(rs.class_slug, rs.rule_version)] = rs
    return out


def default_versions(rulesets: dict[tuple[str, str], RuleSet]) -> dict[str, str]:
    out: dict[str, str] = {}
    for (slug, ver), rs in rulesets.items():
        if rs.default or slug not in out:
            out[slug] = ver
    return out
