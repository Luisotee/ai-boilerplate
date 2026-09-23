"""
Markdown → WhatsApp markup conversion for model-authored text.

LLMs keep answering in Markdown (`**bold**`, `[label](url)`, `# headings`)
despite the prompt forbidding it, and WhatsApp renders those as literal
characters. The prompt can only make that less likely; this module makes the
output correct regardless, as a deterministic pass over every reply.

The conversion is idempotent — text already in WhatsApp markup (`*bold*`,
`_italic_`, `~strike~`) passes through unchanged — so it is safe to apply to
anything. The Telegram client converts WhatsApp markup to Telegram HTML at send
time, so the full pipeline is: model Markdown → WhatsApp markup (here) → HTML.

Lines the clients treat as structure are left exactly as they are: a `---` line
is the burst delimiter the TS clients split replies on, so it must survive
byte-for-byte (it is never read as a Setext heading or a horizontal rule).

It runs synchronously on the worker's event loop for every reply, and its
input can be user-influenced (the model echoes pasted text), so every pattern
must stay linear-ish and the placeholder scheme must be immune to
placeholder-looking characters in the input.
"""

import itertools
import re

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Optional `**`/`__` wrapper, optional image `!`, then [label](target). The
# target may not contain whitespace; one level of balanced parentheses is
# allowed (Wikipedia-style links).
_LINK_RE = re.compile(
    r"(\*\*|__)?!?\[([^\]\n]+)\]\(\s*"
    r"((?:https?://|mailto:|tel:)(?:[^\s()]|\([^\s()]*\))+)"
    r"\s*\)(\*\*|__)?",
    re.IGNORECASE,
)
_SCHEME_RE = re.compile(r"^(?:https?://|mailto:|tel:)", re.IGNORECASE)
# mailto:/tel: are shown as the bare address — WhatsApp links those itself.
_ADDRESS_SCHEME_RE = re.compile(r"^(?:mailto:|tel:)", re.IGNORECASE)

# Bare URL. `*`, `~` and backticks are excluded so a wrapping delimiter is
# never swallowed into the URL; trailing sentence punctuation is trimmed below.
_URL_RE = re.compile(r"https?://[^\s<>\"'`*~\[\]]+")
_URL_TRAILING = ".,;:!?)"

# Deliberately just "hashes, whitespace, rest of line": an optional closing
# `###` is stripped in Python, because expressing it in the pattern
# (`(.+?)[ \t]*#*[ \t]*$`) backtracks cubically on long whitespace runs.
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.*)$", re.MULTILINE)


def _emphasis(delim: str) -> re.Pattern[str]:
    """Delimited span with non-whitespace just inside both delimiters, on one line."""
    d = re.escape(delim)
    return re.compile(rf"(?<![\w*_~]){d}(?=\S)([^\n]*?\S){d}(?![\w*_~])")


_BOLD_ITALIC_RE = _emphasis("***")
_BOLD_STAR_RE = _emphasis("**")
_BOLD_UNDERSCORE_RE = _emphasis("__")
_STRIKE_RE = _emphasis("~~")

# Single-asterisk span. Inside a bold span or heading (which becomes `*…*`) it
# can only be italic, and must become `_…_` or it would close the bold early.
# The closing `*` may run into a word (`*italic*ized`): left as `*`, it would
# split the surrounding bold in two. The opening side still needs a boundary,
# so a lone `2*3` is left alone.
_INNER_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]*?\S)\*(?!\*)")

# Restores are bounded, not "until no placeholder is left": a URL can contain
# a code placeholder, so values nest — but only a few levels deep, and a
# bounded loop cannot hang whatever the input.
_MAX_RESTORE_PASSES = 4


def _sentinels(text: str) -> tuple[str, str] | None:
    """Two private-use code points absent from `text`, for placeholders.

    Choosing them per call means a placeholder-looking sequence already in the
    input can never be mistaken for one of ours (no hang, no IndexError, no
    content swapped in).
    """
    present = set(text)
    free = (ch for ch in map(chr, range(0xE000, 0xF900)) if ch not in present)
    pair = list(itertools.islice(free, 2))
    return (pair[0], pair[1]) if len(pair) == 2 else None


def _bare(value: str) -> str:
    """Target without scheme, leading `www.` and trailing `/`, lower-cased."""
    s = _SCHEME_RE.sub("", value.strip())
    s = re.sub(r"^www\.", "", s, flags=re.IGNORECASE)
    return s.rstrip("/").lower()


def _same_target(label: str, target: str) -> bool:
    """Whether the link label is just the target (or its domain) restated."""
    # Only strip WRAPPING delimiters — URLs routinely contain `_` themselves.
    plain = label.strip("*_~` ").rstrip(":").strip()
    if target.lower().startswith("tel:"):
        return re.sub(r"\D", "", plain) == re.sub(r"\D", "", target)
    bare_label, bare_target = _bare(plain), _bare(target)
    if bare_label == bare_target:
        return True
    # [www.example.com](https://www.example.com/docs/page.html)
    return "." in bare_label and bare_label == bare_target.split("/", 1)[0]


def _strip_bold_markers(text: str) -> str:
    return re.sub(r"\*\*|__", "", text)


def _italicize_inner(text: str) -> str:
    return _INNER_ITALIC_RE.sub(r"_\1_", text)


def _bold(match: re.Match[str]) -> str:
    return f"*{_italicize_inner(match.group(1))}*"


def _heading(match: re.Match[str]) -> str:
    s = match.group(1)
    cr = "\r" if s.endswith("\r") else ""  # CRLF input: `.` matched the `\r`
    s = s.rstrip(" \t\r")
    # A closing `###` only counts after whitespace (CommonMark) — `C#` stays.
    without_hashes = s.rstrip("#")
    if without_hashes != s and (not without_hashes or without_hashes[-1] in " \t"):
        s = without_hashes.rstrip(" \t")
    if not s:
        return match.group(0)
    s = _strip_bold_markers(s)
    s = _STRIKE_RE.sub(r"~\1~", s)
    s = _italicize_inner(s)
    return f"*{s}*{cr}"


def markdown_to_whatsapp(text: str) -> str:
    """Convert Markdown emphasis, links and headings to WhatsApp markup.

    Code spans/fences and URLs are left untouched. Single `*x*` / `_x_` are
    already valid WhatsApp and are not modified (Markdown's single-asterisk
    italic therefore renders as bold — indistinguishable, and harmless).
    """
    if not text:
        return text
    sentinels = _sentinels(text)
    if sentinels is None:  # text holds ~6400 distinct private-use chars
        return text
    ph_open, ph_close = sentinels
    o, c = re.escape(ph_open), re.escape(ph_close)
    placeholder_re = re.compile(rf"{o}[cu](\d+){c}")
    # Emphasis wrapping nothing but one URL placeholder, with matching sides.
    wrapped_url_re = re.compile(rf"(?<![\w*_~])([*_~]{{1,3}})({o}u\d+{c})([*_~]{{1,3}})(?![\w*_~])")

    protected: list[str] = []

    def protect(value: str, kind: str) -> str:
        protected.append(value)
        return f"{ph_open}{kind}{len(protected) - 1}{ph_close}"

    # 1. Code — nothing inside is ever rewritten.
    text = _CODE_FENCE_RE.sub(lambda m: protect(m.group(0), "c"), text)
    text = _INLINE_CODE_RE.sub(lambda m: protect(m.group(0), "c"), text)

    # 2. Links → "label: url", or just the URL when the label restates it.
    # The target is protected here, where its boundaries are known exactly.
    def replace_link(m: re.Match[str]) -> str:
        opener, label, target, closer = m.groups()
        label = label.strip()
        bold = opener is not None and opener == closer
        url = protect(_ADDRESS_SCHEME_RE.sub("", target), "u")
        if _same_target(label, target):
            out = url  # emphasis around a URL breaks auto-linking: drop it
        else:
            label = label.rstrip(":").rstrip() or label
            # Bold goes on the label only, keeping the URL clear of the `*`.
            out = f"*{_strip_bold_markers(label)}*: {url}" if bold else f"{label}: {url}"
        head = "" if bold or opener is None else opener
        tail = "" if bold or closer is None else closer
        following = m.string[m.end() : m.end() + 1]
        if not tail and following and (following.isalnum() or following in "_["):
            tail = " "  # or the next word would merge into the URL
        return head + out + tail

    text = _LINK_RE.sub(replace_link, text)

    # 3. Bare URLs — so `_`/`__` inside them are never read as emphasis.
    def protect_url(m: re.Match[str]) -> str:
        url = m.group(0)
        # A trailing `_` is the URL's own unless an `_` opened right before it.
        trim = _URL_TRAILING + ("_" if m.string[m.start() - 1 : m.start()] == "_" else "")
        trailing = ""
        while url and url[-1] in trim:
            # Keep a closing paren that balances one inside the URL.
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            trailing = url[-1] + trailing
            url = url[:-1]
        return protect(url, "u") + trailing

    text = _URL_RE.sub(protect_url, text)

    # 4. Headings → bold line.
    text = _HEADING_RE.sub(_heading, text)

    # 5. Emphasis. Order matters: *** before **.
    text = _BOLD_ITALIC_RE.sub(r"*_\1_*", text)
    text = _BOLD_STAR_RE.sub(_bold, text)
    text = _BOLD_UNDERSCORE_RE.sub(_bold, text)
    text = _STRIKE_RE.sub(r"~\1~", text)

    # 6. Emphasis around a lone URL breaks WhatsApp's auto-linking — drop it.
    def unwrap(m: re.Match[str]) -> str:
        return m.group(2) if m.group(3) == m.group(1)[::-1] else m.group(0)

    text = wrapped_url_re.sub(unwrap, text)

    # 7. Restore.
    for _ in range(_MAX_RESTORE_PASSES):
        if not placeholder_re.search(text):
            break
        text = placeholder_re.sub(lambda m: protected[int(m.group(1))], text)

    return text
