"""核心三值逻辑测试：用朴素枚举器对 RuleEvaluator 做交叉验证。"""

import itertools
import random

import pytest

from app.logic import (MAX_LISTS, RuleError, RuleEvaluator, parse_rule,
                       referenced_names, rules_equivalent)


# --------------------------------------------------------------- 朴素参考实现

def brute_force(tree, missing_names, reported_membership):
    """枚举缺报变量的所有 2^K 种归属，返回 (结果集合, 敏感变量下标集合)。"""
    k = len(missing_names)
    results = set()
    sensitivity = set()

    def eval_node(node, assignment):
        if isinstance(node, str):
            if node in missing_names:
                return assignment[missing_names.index(node)]
            return 1 if node in reported_membership else 0
        op = node[0]
        vals = [eval_node(a, assignment) for a in node[1]]
        if op == "union":
            return 1 if any(vals) else 0
        if op == "intersect":
            return 1 if all(vals) else 0
        return 1 if vals[0] and not vals[1] else 0

    for bits in itertools.product((0, 1), repeat=k):
        results.add(eval_node(tree, bits))

    for i in range(k):
        for others in itertools.product((0, 1), repeat=max(0, k - 1)):
            bits = list(others[:i]) + [0] + list(others[i:])
            flipped = bits.copy()
            flipped[i] = 1
            if eval_node(tree, bits) != eval_node(tree, flipped):
                sensitivity.add(i)
                break
    return results, sensitivity


def normalize_ref(tree):
    """把规范化树转回 (op, children) / 名字 的简单形式，供参考实现求值。"""
    if tree["type"] == "ref":
        return tree["name"]
    return (tree["op"], [normalize_ref(c) for c in tree["children"]])


# ---------------------------------------------------------------- 规则解析

def test_parse_accepts_chinese_and_english_aliases():
    names = ["A", "B", "C"]
    t1 = parse_rule({"并": ["A", {"差": ["B", "C"]}]}, names)
    t2 = parse_rule({"union": ["A", {"diff": ["B", "C"]}]}, names)
    assert normalize_ref(t1) == normalize_ref(t2)
    assert referenced_names(t1) == {"A", "B", "C"}


def test_parse_rejects_unknown_list():
    with pytest.raises(RuleError):
        parse_rule({"并": ["A"]}, ["B"])


def test_parse_rejects_diff_with_wrong_arity():
    with pytest.raises(RuleError):
        parse_rule({"差": ["A", "B", "C"]}, ["A", "B", "C"])
    with pytest.raises(RuleError):
        parse_rule({"差": ["A"]}, ["A"])


def test_parse_rejects_bad_shapes():
    names = ["A"]
    with pytest.raises(RuleError):
        parse_rule(123, names)
    with pytest.raises(RuleError):
        parse_rule({"并": []}, names)
    with pytest.raises(RuleError):
        parse_rule({"并": ["A"], "交": ["A"]}, names)
    with pytest.raises(RuleError):
        parse_rule("  ", names)
    with pytest.raises(RuleError):
        parse_rule({"并": [["A"]]}, names)  # 数组不是合法节点


# ---------------------------------------------------------------- 判定正确性

RULES = [
    {"并": ["A", "B"]},
    {"交": ["A", "B"]},
    {"差": ["A", "B"]},
    {"差": [{"并": ["A", "B"]}, {"交": ["C", "D"]}]},
    {"并": [{"交": ["A", "C"]}, {"差": ["B", "D"]}]},
    # 同一名单重复出现：归属必须始终一致
    {"差": [{"并": ["A", "B"]}, {"交": ["A", "C"]}]},
    {"并": ["A", {"交": ["A", "B"]}]},  # 幂等形态
    {"差": ["A", "A"]},                  # 恒为空集
    {"并": ["A", {"差": ["B", "A"]}]},   # 等价 A∪B
]

ALL_LISTS = ["A", "B", "C", "D"]


@pytest.mark.parametrize("rule_doc", RULES)
def test_classify_matches_brute_force(rule_doc):
    for reported in itertools.chain.from_iterable(
            itertools.combinations(ALL_LISTS, r) for r in range(len(ALL_LISTS) + 1)):
        reported = set(reported)
        tree = parse_rule(rule_doc, ALL_LISTS)
        # 仅规则引用到的缺报名单才是未知变量（与 RuleEvaluator 的口径一致）
        missing = [n for n in ALL_LISTS
                   if n not in reported and n in referenced_names(tree)]
        ev = RuleEvaluator(tree, missing)
        verdict, rel = ev.classify(reported)

        expected_results, expected_rel = brute_force(
            normalize_ref(tree), missing, reported
        )
        if expected_results == {1}:
            assert verdict == "in" and rel == []
        elif expected_results == {0}:
            assert verdict == "out" and rel == []
        else:
            assert verdict == "pending"
            assert set(rel) == expected_rel


def test_repeated_reference_is_consistent():
    # 规则 A − (A∩C)，A、C 均缺报：
    # 朴素枚举若错误地把两个 A 当独立变量，会认为 C 也可能独立翻转结果；
    # 实际上 A=0 时整式恒 0（C 无效），A=1 时结果 = NOT C，故 A 与 C 都敏感。
    tree = parse_rule({"差": ["A", {"交": ["A", "C"]}]}, ["A", "C"])
    ev = RuleEvaluator(tree, ["A", "C"])
    verdict, rel = ev.classify(set())
    assert verdict == "pending"
    assert set(rel) == {0, 1}
    # 再验证两个 A 引用取值始终一致：真值表行号按 (C,A) 位序排列，
    # 行 0/2 中 A=0，结果恒为 0；行 1/3 中 A=1，结果 = NOT C（行 1=1、行 3=0）
    mask = ev._cache[ev.signature_of(set())][0]
    assert mask & 0b0101 == 0
    assert mask & 0b1010 == 0b0010


def test_no_missing_list_is_always_definite():
    tree = parse_rule({"差": [{"并": ["A", "B"]}, "C"]}, ["A", "B", "C"])
    ev = RuleEvaluator(tree, [])
    assert ev.classify({"A"}) == ("in", [])
    assert ev.classify({"A", "C"}) == ("out", [])
    assert ev.classify(set()) == ("out", [])


def test_empty_reported_list_differs_from_missing():
    # B 已提交但为空（membership 中没有 B），A 缺报：规则 A∪B 只对 A 敏感
    tree = parse_rule({"并": ["A", "B"]}, ["A", "B"])
    ev = RuleEvaluator(tree, ["A"])
    verdict, rel = ev.classify(set())
    assert verdict == "pending" and rel == [0]
    assert ev.rows == 2
    # 对比：B 也缺报时两个变量都在
    ev2 = RuleEvaluator(tree, ["A", "B"])
    verdict2, rel2 = ev2.classify(set())
    assert verdict2 == "pending" and rel2 == [0, 1]
    assert ev2.rows == 4


def test_cache_reuses_signature():
    tree = parse_rule({"并": ["A", "B"]}, ["A", "B"])
    ev = RuleEvaluator(tree, ["B"])
    v1, _ = ev.classify({"A"})
    v2, _ = ev.classify({"A"})
    assert v1 == v2
    assert len(ev._cache) == 1


def test_max_lists_truth_table_size():
    # 14 个缺报变量时真值表为 2^14 位，仍可正常构造
    names = [f"L{i}" for i in range(MAX_LISTS)]
    rule = {"并": names}
    tree = parse_rule(rule, names)
    ev = RuleEvaluator(tree, names)
    assert ev.rows == 1 << MAX_LISTS
    verdict, rel = ev.classify(set())
    assert verdict == "pending"
    assert set(rel) == set(range(MAX_LISTS))


# ---------------------------------------------------------------- 规则等价判定

def eval_ref(tree, membership):
    """朴素求值参考实现：membership 为“在名单中”的名单名集合。"""
    if isinstance(tree, str):
        return 1 if tree in membership else 0
    op, args = tree
    vals = [eval_ref(a, membership) for a in args]
    if op == "union":
        return 1 if any(vals) else 0
    if op == "intersect":
        return 1 if all(vals) else 0
    return 1 if vals[0] and not vals[1] else 0


def equiv_brute_force(tree_a, tree_b):
    """朴素等价判定：枚举引用名单并集的全部归属组合逐一比较。"""
    names = sorted(referenced_names(tree_a) | referenced_names(tree_b))
    for bits in itertools.product((0, 1), repeat=len(names)):
        membership = {n for n, b in zip(names, bits) if b}
        if eval_ref(normalize_ref(tree_a), membership) != \
                eval_ref(normalize_ref(tree_b), membership):
            return False
    return True


def test_rules_equivalent_known_pairs():
    names = ["A", "B", "C"]
    equivalent_pairs = [
        # 吸收律：A ∪ (A ∩ B) ≡ A
        ({"并": ["A", {"交": ["A", "B"]}]}, "A"),
        # 重复引用取值一致：A ∪ (B − A) ≡ A ∪ B
        ({"并": ["A", {"差": ["B", "A"]}]}, {"并": ["A", "B"]}),
        # 恒为空集：A − A ≡ B − B（引用集合不同也算等价）
        ({"差": ["A", "A"]}, {"差": ["B", "B"]}),
        # 德摩根：A − (B ∪ C) ≡ (A − B) ∩ (A − C)
        ({"差": ["A", {"并": ["B", "C"]}]},
         {"交": [{"差": ["A", "B"]}, {"差": ["A", "C"]}]}),
        # 交换律 + 嵌套换位
        ({"并": ["A", {"交": ["B", "C"]}]}, {"并": [{"交": ["C", "B"]}, "A"]}),
    ]
    for doc_a, doc_b in equivalent_pairs:
        tree_a = parse_rule(doc_a, names)
        tree_b = parse_rule(doc_b, names)
        assert rules_equivalent(tree_a, tree_b), (doc_a, doc_b)
        assert rules_equivalent(tree_b, tree_a)  # 对称

    different_pairs = [
        # 并集 vs 交集
        ({"并": ["A", "B"]}, {"交": ["A", "B"]}),
        # 差集方向
        ({"差": ["A", "B"]}, {"差": ["B", "A"]}),
        # 多一份名单：A ∪ B 与 A 在 B=1、A=0 时结论不同
        ({"并": ["A", "B"]}, "A"),
        # A ∪ (B ∩ C) 与 (A ∪ B) ∩ C 不同
        ({"并": ["A", {"交": ["B", "C"]}]},
         {"交": [{"并": ["A", "B"]}, "C"]}),
    ]
    for doc_a, doc_b in different_pairs:
        tree_a = parse_rule(doc_a, names)
        tree_b = parse_rule(doc_b, names)
        assert not rules_equivalent(tree_a, tree_b), (doc_a, doc_b)


def test_rules_equivalent_self_and_textual_variants():
    # 同一规则的不同写法（键别名、空白）规范化后必然等价
    names = ["A", "B"]
    t1 = parse_rule({"并": ["A", {"差": ["B", "A"]}]}, names)
    t2 = parse_rule({"union": ["A", {"diff": ["B", "A"]}]}, names)
    assert rules_equivalent(t1, t2)
    assert rules_equivalent(t1, t1)


def random_rule_doc(names, rng, depth):
    if depth == 0 or rng.random() < 0.35:
        return rng.choice(names)
    op = rng.choice(["并", "交", "差"])
    arity = 2 if op == "差" else rng.randint(1, 3)
    return {op: [random_rule_doc(names, rng, depth - 1) for _ in range(arity)]}


def test_rules_equivalent_matches_brute_force():
    """随机规则对：rules_equivalent 与朴素全组合枚举结论一致。"""
    rng = random.Random(20261123)
    names = ["A", "B", "C", "D"]
    n_equal = 0
    for _ in range(300):
        tree_a = parse_rule(random_rule_doc(names, rng, 3), names)
        tree_b = parse_rule(random_rule_doc(names, rng, 3), names)
        expected = equiv_brute_force(tree_a, tree_b)
        assert rules_equivalent(tree_a, tree_b) == expected
        n_equal += expected
    assert n_equal > 0  # 确保样本里确实出现过等价对，不是恒假通过


def test_rules_equivalent_at_max_lists():
    # 14 个名单的全空间真值表（2^14 位）等价判定仍可快速完成
    names = [f"L{i}" for i in range(MAX_LISTS)]
    tree_a = parse_rule({"并": names}, names)
    tree_b = parse_rule({"并": list(reversed(names))}, names)
    assert rules_equivalent(tree_a, tree_b)
    tree_c = parse_rule({"并": names[:-1]}, names)
    assert not rules_equivalent(tree_a, tree_c)
