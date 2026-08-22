"""Pure, generic statement-state markers for Living Context.

This policy deliberately looks only at the statement text.  It does not know
about Situation categories, source classes, or Need endpoints.  An unresolved
marker always wins over a resolved marker so that text such as ``已确认但尚
未完成`` cannot be admitted as affirmative evidence merely because it also
contains a positive word.
"""

from __future__ import annotations

import re


INFORMATION_STATE_POLICY_VERSION = "veyra.living_context_information_state_policy.v1"
# Keep the selector's existing metric name stable while making the metric
# point at the generic, versioned policy that now owns the implementation.
UNRESOLVED_SUPPORT_POLICY_VERSION = INFORMATION_STATE_POLICY_VERSION

_UNRESOLVED_ZH_MARKERS = (
    "尚未",
    "还没",
    "仍未",
    "还未",
    "未确认",
    "没有确认",
    "未知",
    "待定",
    "待确认",
    "不确定",
    "未完成",
    "未预约",
    "未确定",
    "未解决",
    "待解决",
)

# ``confirmed`` is intentionally separate from the bare unresolved marker
# set: ``not confirmed`` and its common variants are negative forms even
# though ``confirmed`` is a resolved marker below.
_UNRESOLVED_EN_RE = re.compile(
    r"(?:"
    r"\b(?:unknown|unconfirmed|pending|waiting|awaiting|tbd|undecided|uncertain|unresolved)\b"
    r"|\bnot[\s-]+yet\b"
    r"|\b(?:not|never)[\s-]+(?:yet[\s-]+)?(?:confirmed|known|complete|completed|finished|finalized|finalised|booked|resolved|done)\b"
    r"|\b(?:still[\s-]+)?not[\s-]+(?:been[\s-]+)?(?:confirmed|known|complete|completed|finished|finalized|finalised|booked|resolved|done)\b"
    r"|\b(?:has|have|is|are|was|were)[\s-]+not[\s-]+(?:yet[\s-]+)?(?:been[\s-]+)?(?:confirmed|known|complete|completed|finished|finalized|finalised|booked|resolved|done)\b"
    r"|\b(?:hasn['’]t|haven['’]t|isn['’]t|aren['’]t|wasn['’]t|weren['’]t)[\s-]+(?:been[\s-]+)?(?:confirmed|known|complete|completed|finished|finalized|finalised|booked|resolved|done)\b"
    r")",
    re.IGNORECASE,
)

# Negating a state such as "not pending" is affirmative about the state, and
# must not be mistaken for the unresolved marker ``pending`` itself.  The
# negative forms for ``confirmed``/``completed`` remain unresolved above.
_DIRECT_AFFIRMATIVE_NEGATION_RE = re.compile(
    r"\b(?:not|never|no[\s-]+longer)[\s-]+"
    r"(?:unknown|unconfirmed|pending|waiting|awaiting|tbd|undecided|uncertain|unresolved)\b",
    re.IGNORECASE,
)
_DIRECT_AFFIRMATIVE_NEGATION_ZH = (
    "不再未知",
    "不再待定",
    "不再待确认",
    "不再不确定",
    "不是未知",
    "不是待定",
    "不是待确认",
)

_RESOLVED_ZH_RE = re.compile(
    r"(?:"
    r"已(?:经)?(?:确认|完成|预约|确定|解决|安排|落实|处理好|订好)"
    r"|(?:确认|完成|预约|确定|解决|安排|落实)了"
    r"|(?:确认|完成|预约|确定|解决)完毕"
    r")"
)
_RESOLVED_EN_RE = re.compile(
    r"\b(?:confirmed|completed|complete|booked|finalized|finalised|resolved|done|finished|scheduled|settled|arranged|approved|verified)\b",
    re.IGNORECASE,
)

# A resolved word inside a question, condition, future statement, or modal /
# requirement is not affirmative evidence.  The guards are deliberately
# generic: they do not depend on a Situation category or a particular state
# verb.  Chinese modal words are matched next to a bounded set of ordinary
# connective/adverb words so compounds such as ``会议已确认`` do not treat the
# noun ``会`` as a future/modal marker.
_NON_AFFIRMATIVE_ZH_RE = re.compile(
    r"(?:"
    r"是否|吗|？|如果|若"
    r"|(?:需要|应该|可能)[^，。！？；：、]{0,16}(?:确认|完成|预约|确定|解决|安排|落实|处理(?:好)?|订好)"
    r"|(?:将|会)"
    r"(?:(?:被|在|于|到|等到|由|按|按照|根据|去|再|先|最终|尽快|马上|很快|进一步|已经?|会|将)"
    r"|[0-9A-Za-z年月日号周一二三四五六七八九十]){0,8}"
    r"(?:确认|完成|预约|确定|解决|安排|落实|处理(?:好)?|订好)"
    r")"
)
_NON_AFFIRMATIVE_EN_RE = re.compile(
    r"(?:"
    r"\b(?:whether|if|once)\b|\?"
    r"|\b(?:will|shall|may|might|could|can|must|should)\b"
    r"|\bneeds?\s+(?:to\s+)?be\b"
    r"|\brequires?\s+(?:to\s+)?be\b"
    r")",
    re.IGNORECASE,
)


def has_unresolved_statement(statement: str) -> bool:
    """Return whether *statement* contains a generic unresolved assertion.

    The function is intentionally lexical and deterministic.  It accepts only
    text evidence; no scenario vocabulary or similarity score is consulted.
    """

    if not isinstance(statement, str) or not statement:
        return False
    text = statement.strip()
    # Remove only direct affirmative negations before looking for the bare
    # unresolved marker they contain (for example, ``not pending`` contains
    # the marker ``pending``).  Any separate unresolved phrase remains.
    for marker in _DIRECT_AFFIRMATIVE_NEGATION_ZH:
        text = text.replace(marker, "")
    text = _DIRECT_AFFIRMATIVE_NEGATION_RE.sub("", text)
    if any(marker in text for marker in _UNRESOLVED_ZH_MARKERS):
        return True
    if _UNRESOLVED_EN_RE.search(text):
        return True
    return False


def has_clearly_resolved_statement(statement: str) -> bool:
    """Return whether *statement* is clearly affirmative/resolved.

    Unresolved text has precedence.  Thus a mixed statement such as
    ``the result is confirmed but not finalized`` is not considered resolved.
    """

    if not isinstance(statement, str) or not statement:
        return False
    text = statement.strip()
    if has_unresolved_statement(text):
        return False
    if _NON_AFFIRMATIVE_ZH_RE.search(text) or _NON_AFFIRMATIVE_EN_RE.search(text):
        return False
    if any(marker in text for marker in _DIRECT_AFFIRMATIVE_NEGATION_ZH):
        return True
    if _DIRECT_AFFIRMATIVE_NEGATION_RE.search(text):
        return True
    if _RESOLVED_ZH_RE.search(text):
        return True
    return bool(_RESOLVED_EN_RE.search(text))


# Descriptive aliases keep the policy convenient at call sites without
# duplicating any matching logic.
is_unresolved_statement = has_unresolved_statement
is_clearly_resolved_statement = has_clearly_resolved_statement


__all__ = [
    "INFORMATION_STATE_POLICY_VERSION",
    "UNRESOLVED_SUPPORT_POLICY_VERSION",
    "has_unresolved_statement",
    "has_clearly_resolved_statement",
    "is_unresolved_statement",
    "is_clearly_resolved_statement",
]
