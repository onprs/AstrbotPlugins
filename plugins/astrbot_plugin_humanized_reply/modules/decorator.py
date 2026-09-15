"""发送前装饰：把指向决策落到消息链，或在模型未给标记时按概率兜底。

AstrBot 的结果装饰阶段会把 At/Reply 组件当作消息头，分段回复时只随第一段发送，
因此这里只需在链首插入组件，分段发送的「只有第一条带指向」效果由核心保证。

概率兜底按场景分组：被动回复（被 @、唤醒词、@grok）与被 @ 后直接回答的真人习惯
一致，指向概率低；主动回复（概率抽样触发）几乎不引用刚刷过的消息，兜底引用只在
时效范围内抽取。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from astrbot.api.message_components import At, Image, Plain, Reply

from .message_ledger import LedgerEntry


@dataclass
class PointingDecision:
    """一次装饰决策的结果。"""

    at_target: LedgerEntry | None = None
    quote_target: LedgerEntry | None = None
    reason: str = ""

    @property
    def has_pointing(self) -> bool:
        return self.at_target is not None or self.quote_target is not None


def head_has_pointing(chain: list) -> bool:
    """判断消息链中是否已存在 At 或 Reply 组件。"""
    return any(isinstance(comp, At | Reply) for comp in chain)


def can_decorate(chain: list) -> bool:
    """At/Reply 只适用于纯文本或图文消息，与核心的判断保持一致。"""
    return bool(chain) and all(isinstance(item, Plain | Image) for item in chain)


def apply_pointing(chain: list, decision: PointingDecision) -> None:
    """把 @ 与引用组件插入链首。

    插入顺序与核心一致：先 At 后 Reply，最终顺序为 ``[Reply, At, ...]``；
    At 之后的文本前补换行，避免与 @ 昵称粘连。
    """
    if decision.at_target is not None:
        chain.insert(
            0,
            At(qq=decision.at_target.sender_id, name=decision.at_target.nickname),
        )
        if len(chain) > 1 and isinstance(chain[1], Plain):
            chain[1].text = "\n" + chain[1].text
    if decision.quote_target is not None:
        chain.insert(
            0,
            Reply(
                id=decision.quote_target.message_id,
                sender_id=decision.quote_target.sender_id,
                sender_nickname=decision.quote_target.nickname,
                message_str=decision.quote_target.summary,
            ),
        )


def draw_probability(probability: float, rng: random.Random | None = None) -> bool:
    """按概率决定是否命中。"""
    if probability <= 0:
        return False
    if probability >= 1:
        return True
    generator = rng or random
    return generator.random() < probability


def decide_fallback(
    *,
    is_active: bool,
    mention_probability: float,
    quote_probability: float,
    rng: random.Random | None = None,
) -> tuple[bool, bool]:
    """模型没有给出标记时，按场景概率决定是否 @ 或引用。"""
    use_mention = draw_probability(mention_probability, rng)
    use_quote = draw_probability(quote_probability, rng)
    if use_mention and use_quote:
        # 同一条回复同时 @ 和引用会显得刻意，保留引用即可表达指向。
        use_mention = False
    return use_mention, use_quote
