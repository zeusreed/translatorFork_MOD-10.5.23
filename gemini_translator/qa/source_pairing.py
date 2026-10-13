"""Which source paragraph stands behind each translated paragraph.

Block ids are DOM paths: the position of a node among its siblings, whitespace
nodes included.  The source and the translation share them only when they
share their markup.  A source laid out as bare text between ``<br />`` and a
translation in ``<p>`` part ways from the first paragraph on: measured on the
books in use, 2 % of translated blocks of «Клинок Фаэруна» found any source by
id, and in other books a third did, mostly the wrong paragraph.

A translation keeps the order of paragraphs, splitting or merging a few.  A
monotonic alignment by length pairs them (the Gale–Church idea): no embeddings,
a few milliseconds a chapter.  Measured against the embedding alignment on 300
chapters of five books (24 374 paragraphs): the right source paragraph is in
the pair for 86.7 % of translated ones, against 4.9 % by id, and every
paragraph gets a source instead of 61 % getting none.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math

from .semantic_units import flatten_visible_text


# (source blocks, translated blocks, penalty).  A split or a merge costs a
# little, a paragraph without a counterpart costs more than a merge: an unpaired
# block shows the check no source at all, a merged one shows a bit too much.
_MOVES = (
    (1, 1, 0.0),
    (1, 2, 0.4),
    (2, 1, 0.4),
    (2, 2, 0.8),
    (1, 3, 0.9),
    (3, 1, 0.9),
    (1, 0, 1.5),
    (0, 1, 1.5),
)
# Only cells this close to the proportional diagonal are explored: paragraphs
# drift from it locally, never across the chapter, and a full table would cost
# a second of pure Python on a long chapter.
_BAND_MIN = 10
_BAND_SHARE = 0.15


def payload_block_texts(payload: Mapping[str, object]) -> list[tuple[str, str]]:
    """``(block id, visible text)`` of every block of a payload, in order."""

    blocks = payload.get("blocks") if isinstance(payload, Mapping) else None
    # A payload frozen by CoverageRequest keeps its blocks in a tuple.
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        return []
    texts: list[tuple[str, str]] = []
    for block in blocks:
        if not isinstance(block, Mapping) or not block.get("id"):
            continue
        text = flatten_visible_text(block.get("inlines") or [])[0]
        texts.append((str(block["id"]), text.strip()))
    return texts


def pair_source_text(
    source_blocks: Sequence[tuple[str, str]],
    target_blocks: Sequence[tuple[str, str]],
) -> dict[str, str]:
    """Map each translated block id to the text of its source paragraph(s)."""

    source = [(block_id, text.strip()) for block_id, text in source_blocks if text.strip()]
    target = [(block_id, text.strip()) for block_id, text in target_blocks if text.strip()]
    if not source or not target:
        return {}
    # Even with as many paragraphs on both sides the alignment runs: one split
    # and one merge elsewhere leave the counts equal and the order shifted
    # (pairing by position alone was right 2 points less often).
    pairs: dict[str, str] = {}
    spans = _align_by_length(
        [_visible_length(text) for _, text in source],
        [_visible_length(text) for _, text in target],
    )
    for source_start, source_end, target_start, target_end in spans:
        if source_start == source_end or target_start == target_end:
            continue
        # One line: the prompt keeps every field of a block on its own line.
        text = " ".join(text for _, text in source[source_start:source_end])
        for block_id, _ in target[target_start:target_end]:
            pairs[block_id] = text
    return pairs


def _visible_length(text: str) -> int:
    return max(1, len("".join(text.split())))


def _align_by_length(
    source_lengths: Sequence[int], target_lengths: Sequence[int]
) -> list[tuple[int, int, int, int]]:
    """Cheapest monotonic path of ``(source span, target span)`` steps."""

    n, m = len(source_lengths), len(target_lengths)
    ratio = sum(target_lengths) / max(1, sum(source_lengths))
    band = max(_BAND_MIN, int(_BAND_SHARE * max(n, m)))
    source_prefix = [0]
    for length in source_lengths:
        source_prefix.append(source_prefix[-1] + length)
    target_prefix = [0]
    for length in target_lengths:
        target_prefix.append(target_prefix[-1] + length)

    cost: dict[tuple[int, int], float] = {(0, 0): 0.0}
    back: dict[tuple[int, int], tuple[int, int]] = {}
    for i in range(n + 1):
        center = i * m / n
        low = max(0, math.floor(center) - band)
        high = min(m, math.ceil(center) + band)
        for j in range(low, high + 1):
            here = cost.get((i, j))
            if here is None:
                continue
            for source_step, target_step, penalty in _MOVES:
                a, b = i + source_step, j + target_step
                if a > n or b > m:
                    continue
                if source_step and target_step:
                    source_size = source_prefix[a] - source_prefix[i]
                    target_size = target_prefix[b] - target_prefix[j]
                    step = abs(
                        math.log((target_size + 1) / (source_size * ratio + 1))
                    ) + penalty
                else:
                    step = penalty
                total = here + step
                if total < cost.get((a, b), math.inf):
                    cost[(a, b)] = total
                    back[(a, b)] = (i, j)

    if (n, m) not in back:
        return []
    spans: list[tuple[int, int, int, int]] = []
    cell = (n, m)
    while cell != (0, 0):
        previous = back[cell]
        spans.append((previous[0], cell[0], previous[1], cell[1]))
        cell = previous
    spans.reverse()
    return spans
