"""防附和式复读：用字符 n-gram 相似度检测「换一种说法重复对方」。

检测不引入第三方依赖：把文本切成字符 3-gram 集合，用 Jaccard 相似度衡量两条
文本的重合程度。中文短句在字符粒度上比词粒度更稳定，无需分词。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GRAM_SIZE = 3
DEFAULT_THRESHOLD = 0.6
MIN_TEXT_LENGTH = 6

# 确认式短语：单独出现时视为无信息增量的附和。
FILLER_PREFIXES = (
    "是的",
    "没错",
    "确实",
    "对的",
    "同意",
    "说得对",
    "有道理",
    "哈哈哈",
    "哈哈",
    "嗯嗯",
    "好的",
    "收到",
)
_PUNCTUATION_RE = re.compile(r"[\s，。！？；：、,.!?;:~…—\-—\"'“”‘’()（）\[\]【】]+")


def normalize(text: str) -> str:
    """去掉空白与标点，统一为可比较的字符序列。"""
    if not isinstance(text, str):
        return ""
    return _PUNCTUATION_RE.sub("", text)


def char_ngrams(text: str, size: int = GRAM_SIZE) -> set[str]:
    """返回字符 n-gram 集合。文本过短时返回其本身构成的单元素集合。"""
    normalized = normalize(text)
    if not normalized:
        return set()
    if len(normalized) <= size:
        return {normalized}
    return {normalized[idx : idx + size] for idx in range(len(normalized) - size + 1)}


def similarity(left: str, right: str, size: int = GRAM_SIZE) -> float:
    """计算两条文本的字符 n-gram Jaccard 相似度。"""
    left_grams = char_ngrams(left, size)
    right_grams = char_ngrams(right, size)
    if not left_grams or not right_grams:
        return 0.0
    intersection = len(left_grams & right_grams)
    if not intersection:
        return 0.0
    union = len(left_grams | right_grams)
    return intersection / union if union else 0.0


def is_filler_reply(text: str, max_length: int = 12) -> bool:
    """判断是否为单纯的确认式附和。"""
    normalized = normalize(text)
    if not normalized:
        return True
    if len(normalized) > max_length:
        return False
    return any(normalized.startswith(prefix) for prefix in FILLER_PREFIXES)


@dataclass
class EchoVerdict:
    """附和判定结果。"""

    is_echo: bool
    score: float = 0.0
    source: str = ""
    reason: str = ""


def evaluate(
    reply: str,
    sources: list[str],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> EchoVerdict:
    """判断回复是否与候选来源高度重合。

    Args:
        reply: 模型生成的回复正文。
        sources: 候选来源文本，例如触发消息与最近几条群消息。
        threshold: 相似度阈值，达到即判定为附和。
    """
    normalized_reply = normalize(reply)
    if len(normalized_reply) < MIN_TEXT_LENGTH:
        if is_filler_reply(reply) and any(normalize(src) for src in sources):
            return EchoVerdict(
                is_echo=True,
                score=1.0,
                source=next((src for src in sources if normalize(src)), ""),
                reason="确认式回应单独成句",
            )
        return EchoVerdict(is_echo=False)

    best_score = 0.0
    best_source = ""
    for source in sources:
        if not normalize(source):
            continue
        score = similarity(reply, source)
        if score > best_score:
            best_score = score
            best_source = source

    if best_score >= threshold:
        return EchoVerdict(
            is_echo=True,
            score=best_score,
            source=best_source,
            reason="与来源高度重合",
        )

    if is_filler_reply(reply):
        return EchoVerdict(
            is_echo=True,
            score=best_score,
            source=best_source,
            reason="确认式回应单独成句",
        )

    return EchoVerdict(is_echo=False, score=best_score, source=best_source)
