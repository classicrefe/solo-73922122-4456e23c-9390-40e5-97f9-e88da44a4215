"""核心收录规则的三值逻辑求值。

业务背景：
- 已提交（reported）的名单，其中卷号归属完全确定（在 / 不在）。
- 缺报名单（从未提交，或已撤回）中的每个卷号归属未知，可能在也可能不在。
  同一缺报名单在规则中被引用多少次，同一个卷号在该名单中的归属都保持一致。
- 对每种“缺报归属组合”做一次规则求值，得到该卷号的可能取值集合：
  始终为真 -> 确定入选；始终为假 -> 确定落选；否则 -> 待补。
- 对“待补”卷号，列出仅翻转该缺报名单的归属就可能翻转结果的名单
  （即布尔函数对该变量敏感的名单）。

实现方式：把规则在所有缺报变量上的真值表（2^K 位，K <= 14，即 <= 16384 位、
约 2 KiB 位掩码）预计算一次，再用“已报名单的归属签名”逐卷查表。
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_LISTS = 14
MAX_RULE_DEPTH = 32
MAX_NODE_COUNT = 10_000

# 操作的中、英文别名
_OP_ALIASES = {
    "并": "union",
    "交": "intersect",
    "差": "diff",
    "union": "union",
    "intersect": "intersect",
    "diff": "diff",
    "or": "union",
    "and": "intersect",
    "subtract": "diff",
}


class RuleError(ValueError):
    """规则 JSON 不合法。"""


# ---------------------------------------------------------------- 规则解析

def _normalize(node, depth: int, counter: list[int], path: str) -> dict:
    if depth > MAX_RULE_DEPTH:
        raise RuleError(f"规则嵌套深度超过 {MAX_RULE_DEPTH} 层（{path}）")
    counter[0] += 1
    if counter[0] > MAX_NODE_COUNT:
        raise RuleError(f"规则节点数超过 {MAX_NODE_COUNT}")

    if isinstance(node, str):
        if not node.strip():
            raise RuleError("规则中存在空名单名")
        return {"type": "ref", "name": node.strip()}

    if not isinstance(node, dict):
        raise RuleError(f"规则节点必须是名单名字符串或操作对象（{path}）")

    op_keys = [k for k in node if k in _OP_ALIASES]
    if not op_keys:
        raise RuleError(f"缺少操作键（并/交/diff 等）：{path or '根节点'}")
    if len(op_keys) > 1:
        raise RuleError(f"同一节点出现多个操作键 {op_keys}（{path}）")
    raw_op = op_keys[0]
    op = _OP_ALIASES[raw_op]
    args = node[raw_op]
    if not isinstance(args, list) or not args:
        raise RuleError(f"操作 {raw_op!r} 必须是非空数组（{path}）")
    if op == "diff" and len(args) != 2:
        raise RuleError(f"差集操作 {raw_op!r} 必须恰好含两个参数（{path}）")
    if len(args) > MAX_LISTS * 4:
        raise RuleError(f"操作 {raw_op!r} 参数过多（{path}）")

    children = [
        _normalize(child, depth + 1, counter, f"{path}.{raw_op}[{i}]" if path else f"{raw_op}[{i}]")
        for i, child in enumerate(args)
    ]
    return {"type": "op", "op": op, "children": children}


def parse_rule(doc, list_names) -> dict:
    """校验并规范化规则 JSON。

    list_names: 当前登记的全部名单名（容器，支持 in 判断）。
    返回规范化后的内部树。引用不存在的名单、空名单名等均抛 RuleError。
    """
    names = set(list_names)
    tree = _normalize(doc, 0, [0], "")

    referenced: set[str] = set()

    def walk(n: dict) -> None:
        if n["type"] == "ref":
            if n["name"] not in names:
                raise RuleError(f"规则引用了未登记的名单：{n['name']}")
            referenced.add(n["name"])
        else:
            for c in n["children"]:
                walk(c)

    walk(tree)
    return tree


def referenced_names(tree: dict) -> set[str]:
    out: set[str] = set()

    def walk(n: dict) -> None:
        if n["type"] == "ref":
            out.add(n["name"])
        else:
            for c in n["children"]:
                walk(c)

    walk(tree)
    return out


# ---------------------------------------------------------------- 真值表求值

def _bit_count(x: int) -> int:
    return x.bit_count()


@dataclass
class _ListRef:
    """规则中名单引用被解析后的结果。"""
    name: str
    missing_index: int | None  # 缺报名单的变量下标；已报名单为 None


class RuleEvaluator:
    """把规则编译为缺报变量上的真值表，并按卷号归属签名求值。

    参数
    ----
    tree: parse_rule 返回的规范化规则树。
    missing: 缺报名单名列表，顺序决定变量位序（第 i 个 -> 真值表第 i 位）。

    每个卷号用一个整数“已报名单归属签名”表示它在哪些已报名单中；
    同一签名的卷号结论完全相同，求值结果（含敏感性）只算一次并缓存。
    """

    def __init__(self, tree: dict, missing: list[str]):
        # 只有规则实际引用到的缺报名单才是未知变量；未被引用的名单不影响结果
        used = referenced_names(tree)
        self.missing = [name for name in missing if name in used]
        self.k = len(self.missing)
        self.rows = 1 << self.k
        self._missing_pos = {name: i for i, name in enumerate(self.missing)}

        # 规则引用到的已报名单 -> 归属签名中的固定位
        self.reported_bits: dict[str, int] = {}
        bit = 0
        for name in sorted(used):
            if name not in self._missing_pos:
                self.reported_bits[name] = 1 << bit
                bit += 1

        self._resolve_refs(tree)
        # 预计算各缺报变量的列掩码，避免逐卷重复计算
        self._var_masks = [self._var_mask(i) for i in range(self.k)]
        # 所有赋值位均为 1 的掩码（rows=1 时为 1）
        self._all_one = (1 << self.rows) - 1
        # 归属签名 -> (结果掩码, 结论, 敏感变量下标)
        self._cache: dict[int, tuple[int, str, tuple[int, ...]]] = {}

    def _resolve_refs(self, tree: dict) -> None:
        def walk(n: dict):
            if n["type"] == "ref":
                return _ListRef(n["name"], self._missing_pos.get(n["name"]))
            return {"type": "op", "op": n["op"],
                    "children": [walk(c) for c in n["children"]]}
        self.tree = walk(tree)

    # 预计算缺报变量 i 的列掩码：第 r 位为 1 当且仅当赋值 r 中变量 i 为真。
    # 赋值 r 按位序排列（变量 i 对应 r 的第 i 位），因此列 i 的位模式为
    # block=2^i 个 0、block 个 1 交替重复。
    def _var_mask(self, i: int) -> int:
        block = 1 << i
        mask = 0
        ones = (1 << block) - 1
        for start in range(0, self.rows, 2 * block):
            mask |= ones << (start + block)
        return mask

    def _eval(self, node, signature: int) -> int:
        """对当前卷号求节点的完整结果掩码。

        缺报名单 -> 变量列掩码；已报名单 -> 0 或全 1（对应该卷号在不在该名单）。
        signature 为整数，按值传递，同一名单被多次引用时取值天然一致。
        """
        if isinstance(node, _ListRef):
            if node.missing_index is not None:
                return self._var_masks[node.missing_index]
            bit = self.reported_bits.get(node.name)
            return self._all_one if (bit is not None and signature & bit) else 0
        children = node["children"]
        if node["op"] == "union":
            m = 0
            for c in children:
                m |= self._eval(c, signature)
            return m
        if node["op"] == "intersect":
            m = self._all_one
            for c in children:
                m &= self._eval(c, signature)
            return m
        return (self._eval(children[0], signature)
                & ~self._eval(children[1], signature)
                & self._all_one)

    def _sensitivity(self, mask: int) -> tuple[int, ...]:
        """返回对结果掩码敏感的缺报变量下标。

        判定：存在某组“其他变量”赋值，仅翻转变量 i 时函数值翻转。
        赋值按位序排列，变量 i 的对称行相差 block=2^i 位、每 2*block 位一周期。
        """
        relevant: list[int] = []
        for i in range(self.k):
            block = 1 << i
            low_mask = (1 << block) - 1
            for start in range(0, self.rows, 2 * block):
                low = (mask >> start) & low_mask
                high = (mask >> (start + block)) & low_mask
                if low != high:
                    relevant.append(i)
                    break
        return tuple(relevant)

    def signature_of(self, membership) -> int:
        """把“卷号所在的已报名单名集合”转换为整数归属签名。"""
        sig = 0
        for name in membership:
            bit = self.reported_bits.get(name)
            if bit is not None:
                sig |= bit
        return sig

    def classify_signature(self, signature: int) -> tuple[str, list[int]]:
        """按整数归属签名判定（服务层逐卷调用的热路径，避免构造集合）。"""
        cached = self._cache.get(signature)
        if cached is None:
            mask = self._eval(self.tree, signature) & self._all_one
            ones = _bit_count(mask)
            if ones == self.rows:
                verdict, rel = "in", ()
            elif ones == 0:
                verdict, rel = "out", ()
            else:
                verdict, rel = "pending", self._sensitivity(mask)
            cached = (mask, verdict, rel)
            if len(self._cache) <= self.rows:
                self._cache[signature] = cached
        _, verdict, rel = cached
        return verdict, list(rel)

    def classify(self, membership) -> tuple[str, list[int]]:
        """判断一个卷号的结论。

        membership: 该卷号当前出现于其中的“已提交名单”名集合。
        返回 (verdict, relevant_missing_indices)。
        verdict 为 'in'（确定入选）/ 'out'（确定落选）/ 'pending'（待补）。
        """
        return self.classify_signature(self.signature_of(membership))
