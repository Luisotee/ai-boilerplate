"""
Images in bot replies.

The model asks for an image by writing `![caption](https://…)` in its reply
(the rule is in the `formatting_guidance` hook). Tools can also append a
`ReplyImage` to `AgentDeps.reply_media`. The stream processor calls
`collect_reply_media` BEFORE `markdown_to_whatsapp`, which would otherwise
flatten the directive into `caption: url`:

- each image is downloaded once through `safe_fetch.fetch_public_image` (the
  only place a model-chosen URL is fetched) and its bytes stored in Redis;
- the directives of images that arrived are removed from the text; a failed
  one stays and is flattened to a plain link, as before this feature;
- the job metadata lists the images, and each client fetches the bytes from
  `GET /chat/job/{job_id}/media/{index}` and sends them after the text.

There is no target parameter anywhere: the images go to the chat the job came
from, so a directive can never reach another conversation.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from redis.asyncio import Redis

from .formatting import _CODE_FENCE_RE, _INLINE_CODE_RE, markdown_to_whatsapp
from .logger import logger
from .queue.utils import MAX_JOB_MEDIA, save_job_media
from .safe_fetch import FetchedImage, ImageFetchError, fetch_public_image

#: WhatsApp and Telegram both cap media captions at 1,024 characters.
MAX_CAPTION_CHARS = 1024

# `![caption](https://url)`. https only: anything else stays text. The URL may
# not hold whitespace, parentheses or angle brackets.
_IMAGE_DIRECTIVE_RE = re.compile(r"!\[([^\]\n]*)\]\(\s*(https://[^\s()<>]+)\s*\)", re.IGNORECASE)

# A directive wrapped in a link, `[![badge](img)](https://site)`: the outer
# link's target is group 1.
_LINK_WRAPPER_RE = re.compile(r"\[\s*$")
_LINK_TAIL_RE = re.compile(r"\s*\]\(\s*(https?://[^\s()<>]+)\s*\)", re.IGNORECASE)

# A line prefix that is only a list marker: `-`, `*`, `+`, `1.` or `1)`.
_LIST_MARKER_RE = re.compile(r"\s*(?:[-*+]|\d+[.)])?\s*")

#: How many images are tried for MAX_JOB_MEDIA slots, so a few dead links
#: don't use up the cap while a later image would have worked.
MAX_CANDIDATES = 2 * MAX_JOB_MEDIA


@dataclass(frozen=True)
class ReplyImage:
    url: str
    caption: str = ""


@dataclass(frozen=True)
class ImageDirective:
    image: ReplyImage
    start: int
    end: int
    #: Text left in place of the directive once its image is delivered: the
    #: outer link's URL for a linked image, otherwise nothing.
    replacement: str = ""


@dataclass(frozen=True)
class ReplyMedia:
    """Result of `collect_reply_media`."""

    #: Reply text with the delivered directives removed (still Markdown).
    text: str
    #: Job-metadata entries: {"index", "caption", "mimetype", "url"}.
    media: list[dict]
    #: Lines to append to the saved/embedded history copy of the reply: one
    #: `![caption](url)` per image sent.
    history_lines: list[str]


def _clean_caption(caption: str) -> str:
    # Captions are delivered like reply text, so they get the same conversion.
    caption = markdown_to_whatsapp(" ".join(caption.split()))
    return caption[:MAX_CAPTION_CHARS]


def _mask_code(text: str) -> str:
    """Blank out code fences and spans, keeping every index in place."""

    def blank(m: re.Match[str]) -> str:
        return " " * len(m.group(0))

    text = _CODE_FENCE_RE.sub(blank, text)
    return _INLINE_CODE_RE.sub(blank, text)


def extract_image_directives(text: str) -> list[ImageDirective]:
    """Every `![caption](https://…)` outside code, in order (duplicates kept)."""
    if not text or "![" not in text:
        return []
    masked = _mask_code(text)
    directives = []
    for m in _IMAGE_DIRECTIVE_RE.finditer(masked):
        start, end, replacement = m.start(), m.end(), ""
        head = _LINK_WRAPPER_RE.search(masked, 0, start)
        tail = _LINK_TAIL_RE.match(masked, end)
        if head and tail and head.end() == start:
            # Linked image: drop the wrapper, keep its target as a bare link.
            start, end, replacement = head.start(), tail.end(), tail.group(1)
        directives.append(
            ImageDirective(
                image=ReplyImage(url=m.group(2), caption=_clean_caption(m.group(1))),
                start=start,
                end=end,
                replacement=replacement,
            )
        )
    return directives


def _whole_line(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a directive alone on its line (a list marker aside) to the whole
    line, newline included."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    if not _LIST_MARKER_RE.fullmatch(text[line_start:start]) or text[end:line_end].strip():
        return start, end
    return line_start, min(line_end + 1, len(text))


def _tidy(text: str) -> str:
    """Drop the trailing spaces and extra blank lines removals leave behind,
    outside code fences only."""
    out: list[str] = []
    pos = 0
    for fence in _CODE_FENCE_RE.finditer(text):
        out.append(_tidy_prose(text[pos : fence.start()]))
        out.append(fence.group(0))
        pos = fence.end()
    out.append(_tidy_prose(text[pos:]))
    return "".join(out).strip()


def _tidy_prose(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _remove_spans(text: str, spans: list[tuple[int, int, str]]) -> str:
    out: list[str] = []
    pos = 0
    widened = sorted((*_whole_line(text, s, e), r) if not r else (s, e, r) for s, e, r in spans)
    for start, end, replacement in widened:
        if start < pos:  # two directives on one line, already removed
            start = pos
        out.append(text[pos:start])
        out.append(replacement)
        pos = max(pos, end)
    out.append(text[pos:])
    return _tidy("".join(out))


async def collect_reply_media(
    redis: Redis,
    job_id: str,
    text: str,
    extra: list[ReplyImage] | None = None,
) -> ReplyMedia:
    """Fetch and store the reply's images; return the text to deliver.

    Never raises for a bad image: it is logged and left out (a directive's
    text then stays in the reply as a link).
    """
    directives = extract_image_directives(text)

    # Unique URLs in order: tool-added images first, then directives. Up to
    # MAX_CANDIDATES are tried and the first MAX_JOB_MEDIA that arrive are
    # sent; the rest are left alone (a directive then stays as a link).
    wanted: list[ReplyImage] = []
    seen: set[str] = set()
    for image in [*(extra or []), *(d.image for d in directives)]:
        if image.url in seen or len(wanted) >= MAX_CANDIDATES:
            continue
        seen.add(image.url)
        wanted.append(ReplyImage(url=image.url, caption=_clean_caption(image.caption)))

    if not wanted:
        return ReplyMedia(text=text, media=[], history_lines=[])

    results = await asyncio.gather(
        *(fetch_public_image(image.url) for image in wanted), return_exceptions=True
    )

    media: list[dict] = []
    history_lines: list[str] = []
    delivered: set[str] = set()
    for image, result in zip(wanted, results, strict=True):
        if len(media) >= MAX_JOB_MEDIA:
            break
        if isinstance(result, ImageFetchError):
            logger.warning(f"[Job {job_id}] Reply image skipped ({result}): {image.url[:80]}")
            continue
        if not isinstance(result, FetchedImage):
            logger.error(
                f"[Job {job_id}] Reply image failed: {image.url[:80]}",
                exc_info=result if isinstance(result, BaseException) else None,
            )
            continue
        index = len(media)
        try:
            await save_job_media(redis, job_id, index, result.data)
        except Exception:
            logger.error(f"[Job {job_id}] Could not store reply image {index}", exc_info=True)
            continue
        media.append(
            {
                "index": index,
                "caption": image.caption,
                "mimetype": result.mimetype,
                "url": image.url,
            }
        )
        delivered.add(image.url)
        # The directive itself, so the model's history shows the form that works.
        history_lines.append(f"![{image.caption.replace(']', '')}]({image.url})")

    spans = [(d.start, d.end, d.replacement) for d in directives if d.image.url in delivered]
    new_text = _remove_spans(text, spans) if spans else text
    logger.info(f"[Job {job_id}] Reply images: {len(media)} of {len(wanted)} ready")
    return ReplyMedia(text=new_text, media=media, history_lines=history_lines)
