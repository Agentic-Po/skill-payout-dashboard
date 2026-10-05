"""Bound digest parts without dropping lines or splitting HTML tags."""


def units(text):
    # Conservative raw UTF-16 budget includes markup as well as emoji.
    return len(text.encode("utf-16-le")) // 2


def digest_parts(text, budget=3900):
    if units(text) <= budget:
        return [text]
    chunks, lines, size = [], [], 0
    # Each formatter line owns its complete HTML tags. Reserve space for
    # numbering; never truncate a warning, caveat, or evidence line.
    limit = budget - 80
    for line in text.split("\n"):
        n = units(line)
        if n > limit:
            raise ValueError("digest_line_exceeds_budget")
        if lines and size + 1 + n > limit:
            chunks.append("\n".join(lines))
            lines, size = [], 0
        size += n + bool(lines)
        lines.append(line)
    if lines:
        chunks.append("\n".join(lines))
    return [f"<b>Summary · part {i}/{len(chunks)}</b>\n{chunk}"
            for i, chunk in enumerate(chunks, 1)]
