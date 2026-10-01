"""
Verifies that an event's "excerpt" is genuinely copy-pasted from the
source text, not an AI paraphrase or a silent splice (e.g. joining two
non-adjacent sentences with "..."). Pure text matching, no LLM calls.

The extraction prompt asks for a verbatim quote, but models sometimes
"clean up" a quote by cutting out a middle section and joining the ends
with an ellipsis — which is not a real quote anymore. This catches that
and falls back to the longest portion that's actually a real substring.
"""

NOT_VERIFIED_MESSAGE = "[Could not verify exact wording against the source — open the source to confirm.]"
MIN_FALLBACK_LENGTH = 15


def fix_excerpt(excerpt: str | None, source_text: str | None) -> str:
    excerpt = (excerpt or "").strip()
    source_text = source_text or ""

    if not excerpt:
        return excerpt
    if not source_text:
        return excerpt  # nothing to verify against — leave as-is

    if excerpt in source_text:
        return excerpt

    # Likely a splice: two real fragments joined by "..." — try the part
    # before the splice marker first, since that's usually the anchor quote.
    for sep in ("...", "…"):
        if sep in excerpt:
            head = excerpt.split(sep)[0].strip()
            if len(head) >= MIN_FALLBACK_LENGTH and head in source_text:
                return head

    # Otherwise, shrink from the end word-by-word until we find a real substring.
    words = excerpt.split()
    for cut in range(len(words) - 1, 0, -1):
        candidate = " ".join(words[:cut]).strip()
        if len(candidate) >= MIN_FALLBACK_LENGTH and candidate in source_text:
            return candidate

    return NOT_VERIFIED_MESSAGE
