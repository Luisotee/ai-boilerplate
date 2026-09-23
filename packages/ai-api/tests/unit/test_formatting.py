"""
Unit tests for the Markdown → WhatsApp markup converter (formatting.py).

Models answer in Markdown despite the prompt; every reply passes through this
converter before delivery. The invariants that matter: Markdown is rewritten,
WhatsApp markup is left alone (idempotence), URLs/code are never mangled, and
the `---` burst delimiter the TS clients split on survives byte-for-byte.
"""

import json
import time
from pathlib import Path

import pytest

from ai_api.formatting import markdown_to_whatsapp

# Private-use code points — what the converter's placeholders are built from.
PUA_OPEN, PUA_CLOSE = chr(0xE000), chr(0xE001)

DOCS_URL = "https://docs.example.com/guides/getting_started/_setup_v2.html"

# A realistic Markdown-heavy reply (the kind that prompted the converter).
MARKDOWN_REPLY = f"""Sorry about that, Alex.

The most reliable way to read the **Getting Started guide, version 2** is through \
the official docs:

1. Open the docs site: **[docs.example.com](https://docs.example.com)**
2. In the search box, type: **setup**
3. The first result will be "Getting Started — Setup".

You can also open it directly here:
**[{DOCS_URL}]({DOCS_URL})**

If that link still fails for you, try this mirror:
**[https://mirror.example.org/setup](https://mirror.example.org/setup)**

The guide separates **local development** from **production deployment**."""

PIPELINE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "markdown_pipeline.json"


class TestEmphasis:
    def test_double_asterisk_bold(self):
        assert markdown_to_whatsapp("a **warning** here") == "a *warning* here"

    def test_double_underscore_bold(self):
        assert markdown_to_whatsapp("a __warning__ here") == "a *warning* here"

    def test_bold_italic(self):
        assert markdown_to_whatsapp("***very important***") == "*_very important_*"

    def test_double_tilde_strikethrough(self):
        assert markdown_to_whatsapp("~~struck~~") == "~struck~"

    def test_bold_followed_by_punctuation(self):
        assert markdown_to_whatsapp("the **Act 14.944**, which") == "the *Act 14.944*, which"


class TestFalsePositives:
    def test_exponent_is_left_alone(self):
        assert markdown_to_whatsapp("2**3 = 8") == "2**3 = 8"

    def test_spaced_asterisks_are_left_alone(self):
        assert markdown_to_whatsapp("a ** b") == "a ** b"

    def test_snake_case_is_left_alone(self):
        assert markdown_to_whatsapp("var_name__with__x") == "var_name__with__x"

    def test_bold_does_not_span_lines(self):
        assert markdown_to_whatsapp("**abc\ndef**") == "**abc\ndef**"

    def test_hashtag_is_not_a_heading(self):
        assert markdown_to_whatsapp("#python #tips") == "#python #tips"


class TestHeadings:
    def test_heading_becomes_bold_line(self):
        assert markdown_to_whatsapp("## Key points\ntext") == "*Key points*\ntext"

    def test_bold_inside_heading_is_not_doubled(self):
        assert markdown_to_whatsapp("# **Title**") == "*Title*"

    def test_closing_hashes_are_dropped(self):
        assert markdown_to_whatsapp("### Section ###") == "*Section*"


class TestBurstDelimiters:
    """The TS clients split replies into separate messages on a line that is
    exactly `---` (`/^---\\s*$/`, outside code fences). The converter must never
    eat, move or merge those lines — not as a Setext heading underline, not as a
    horizontal rule, not as part of the surrounding emphasis."""

    @pytest.mark.parametrize(
        "text",
        [
            "Intro\n---\nAnswer",
            "---",
            "Hi!\n\n---\n\nMore",
            "Trailing\n---",
            "---  \nleading delimiter with trailing spaces",
            "text --- inline dashes stay too",
        ],
    )
    def test_plain_delimiters_are_unchanged(self, text):
        assert markdown_to_whatsapp(text) == text

    def test_delimiter_after_a_line_is_not_a_setext_heading(self):
        # CommonMark would read "Title\n---" as an h2; we must not.
        assert markdown_to_whatsapp("Title\n---\nbody") == "Title\n---\nbody"

    def test_markdown_around_delimiters_converts_and_delimiters_survive(self):
        text = "## Summary\n**Short** answer.\n---\nSee [the docs](https://example.com/docs).\n---\nAnything else?"
        out = markdown_to_whatsapp(text)
        assert out == (
            "*Summary*\n*Short* answer.\n---\nSee the docs: https://example.com/docs.\n---\n"
            "Anything else?"
        )
        assert out.split("\n").count("---") == 2

    def test_delimiter_inside_code_fence_is_untouched(self):
        text = "```\n---\n**x**\n```"
        assert markdown_to_whatsapp(text) == text

    def test_other_rule_styles_are_left_literal(self):
        # Not converted into `---`: that would silently create a burst split.
        for text in ("***", "___", "* * *"):
            assert markdown_to_whatsapp(text) == text


class TestLinks:
    def test_labelled_link_keeps_label_and_url(self):
        assert (
            markdown_to_whatsapp("[Release notes](https://example.com/releases/2)")
            == "Release notes: https://example.com/releases/2"
        )

    def test_link_whose_label_is_the_url_collapses(self):
        assert markdown_to_whatsapp("[https://a.com/x](https://a.com/x)") == "https://a.com/x"

    def test_link_whose_label_is_the_domain_collapses(self):
        assert (
            markdown_to_whatsapp("[www.example.com](https://www.example.com)")
            == "https://www.example.com"
        )

    def test_bolded_link_becomes_bare_url(self):
        # Emphasis around a URL breaks WhatsApp's auto-linking.
        assert markdown_to_whatsapp("**[https://a.com/x](https://a.com/x)**") == "https://a.com/x"

    def test_url_with_parentheses(self):
        assert (
            markdown_to_whatsapp("[Fire](https://en.wikipedia.org/wiki/Fire_(disambiguation))")
            == "Fire: https://en.wikipedia.org/wiki/Fire_(disambiguation)"
        )


class TestUrls:
    def test_underscores_inside_url_are_untouched(self):
        text = "See https://x.com/a__b__c now"
        assert markdown_to_whatsapp(text) == text

    def test_trailing_period_is_not_part_of_url(self):
        text = "See https://x.com/a__b__c."
        assert markdown_to_whatsapp(text) == text

    def test_bold_bare_url_is_unwrapped(self):
        assert markdown_to_whatsapp("**https://a.com/x**") == "https://a.com/x"
        assert markdown_to_whatsapp("*https://a.com/x*") == "https://a.com/x"

    def test_italic_bare_url_is_unwrapped(self):
        assert markdown_to_whatsapp("_https://a.com/x_") == "https://a.com/x"

    def test_url_in_parentheses_keeps_the_paren_outside(self):
        text = "(https://a.com/x)"
        assert markdown_to_whatsapp(text) == text


class TestCode:
    def test_inline_code_is_untouched(self):
        text = "Use `**literal**` here"
        assert markdown_to_whatsapp(text) == text

    def test_code_fence_is_untouched(self):
        text = "```\n# not a heading\n**x**\n```"
        assert markdown_to_whatsapp(text) == text

    def test_bold_around_code_is_converted_not_stripped(self):
        assert markdown_to_whatsapp("**`x`**") == "*`x`*"


class TestIdempotence:
    def test_whatsapp_markup_is_unchanged(self):
        text = (
            "*Hello*, _how are you_? ~wrong~\n- item\n• item\n1. one\n`code`\nhttps://example.com"
        )
        assert markdown_to_whatsapp(text) == text

    def test_converting_twice_is_the_same_as_once(self):
        once = markdown_to_whatsapp(MARKDOWN_REPLY)
        assert markdown_to_whatsapp(once) == once

    def test_empty_string(self):
        assert markdown_to_whatsapp("") == ""


class TestRealReply:
    def test_no_markdown_survives(self):
        out = markdown_to_whatsapp(MARKDOWN_REPLY)
        assert "**" not in out
        assert "](" not in out

    def test_links_become_bare_urls(self):
        out = markdown_to_whatsapp(MARKDOWN_REPLY)
        assert "docs site: https://docs.example.com\n" in out
        assert f"\n{DOCS_URL}\n" in out
        assert "\nhttps://mirror.example.org/setup\n" in out

    def test_bold_uses_single_asterisk(self):
        out = markdown_to_whatsapp(MARKDOWN_REPLY)
        assert "*Getting Started guide, version 2*" in out
        assert "type: *setup*" in out
        assert "*local development*" in out


class TestPlaceholderSafety:
    """Private-use characters in the input used to hang, crash or corrupt the
    restore step — and the converter runs on the worker's event loop."""

    def test_placeholder_lookalike_in_code_does_not_hang(self):
        text = f"`{PUA_OPEN}c0{PUA_CLOSE}`"
        assert markdown_to_whatsapp(text) == text

    def test_out_of_range_placeholder_lookalike_does_not_crash(self):
        text = f"{PUA_OPEN}u9999{PUA_CLOSE} and {PUA_OPEN}c5{PUA_CLOSE}"
        assert markdown_to_whatsapp(text) == text

    def test_placeholder_lookalike_is_not_swapped_for_other_content(self):
        text = f"text {PUA_OPEN}c0{PUA_CLOSE} end `code`"
        assert markdown_to_whatsapp(text) == text

    def test_placeholder_lookalike_next_to_markdown_still_converts(self):
        assert markdown_to_whatsapp(f"{PUA_OPEN} **x**") == f"{PUA_OPEN} *x*"


class TestPerformance:
    def test_heading_with_long_whitespace_run_is_fast(self):
        # A "closing hashes" heading pattern backtracks cubically here.
        start = time.perf_counter()
        out = markdown_to_whatsapp("# a" + " " * 5000 + "b")
        assert time.perf_counter() - start < 1
        assert out == "*a" + " " * 5000 + "b*"


class TestHeadingEdgeCases:
    def test_hash_inside_last_word_is_kept(self):
        assert markdown_to_whatsapp("# C#") == "*C#*"
        assert markdown_to_whatsapp("## Learning C#") == "*Learning C#*"

    def test_italic_inside_heading_does_not_close_the_bold(self):
        assert markdown_to_whatsapp("# Title *important*") == "*Title _important_*"

    def test_strikethrough_inside_heading(self):
        assert markdown_to_whatsapp("# Old API ~~deprecated~~") == "*Old API ~deprecated~*"

    def test_crlf_heading_keeps_its_line_ending(self):
        assert markdown_to_whatsapp("# Title\r\ntext") == "*Title*\r\ntext"


class TestLinkEdgeCases:
    def test_bold_labelled_link_bolds_only_the_label(self):
        assert (
            markdown_to_whatsapp("**[Example Docs](https://docs.example.com)**.")
            == "*Example Docs*: https://docs.example.com."
        )

    def test_word_after_link_does_not_merge_into_url(self):
        assert markdown_to_whatsapp("[here](https://x.com)s") == "here: https://x.com s"

    def test_adjacent_links_stay_separate(self):
        assert (
            markdown_to_whatsapp("[Portal](https://x.com)[Other](https://y.com)")
            == "Portal: https://x.com Other: https://y.com"
        )

    def test_markdown_inside_link_url_is_untouched(self):
        assert markdown_to_whatsapp("[P](https://x.com/?q=**x**)") == "P: https://x.com/?q=**x**"

    def test_label_that_is_the_domain_of_a_deep_link_collapses(self):
        url = "https://www.example.com/docs_v2/guides/intro.htm"
        assert markdown_to_whatsapp(f"[www.example.com]({url})") == url

    def test_label_ending_in_colon_is_not_doubled(self):
        assert markdown_to_whatsapp("[See:](https://x.com)") == "See: https://x.com"

    def test_mailto_link_becomes_the_address(self):
        assert (
            markdown_to_whatsapp("[support@example.com](mailto:support@example.com)")
            == "support@example.com"
        )

    def test_tel_link_keeps_label_and_number(self):
        assert markdown_to_whatsapp("[Call us](tel:+4915700000000)") == "Call us: +4915700000000"

    def test_image_loses_the_bang(self):
        assert markdown_to_whatsapp("![Map](https://x.com/img.png)") == "Map: https://x.com/img.png"


class TestUrlEdgeCases:
    def test_wrapped_url_keeps_its_own_trailing_underscore(self):
        assert markdown_to_whatsapp("*https://a.com/x_*") == "https://a.com/x_"
        assert markdown_to_whatsapp("**https://a.com/foo_bar_**") == "https://a.com/foo_bar_"

    def test_mismatched_wrapper_is_left_alone(self):
        text = "*https://a.com/x_"
        assert markdown_to_whatsapp(text) == text


class TestNestedEmphasis:
    def test_italic_inside_bold(self):
        assert markdown_to_whatsapp("**bold *italic* bold**") == "*bold _italic_ bold*"

    @pytest.mark.parametrize(
        "text",
        ["**bold *italic* bold**", "**bold *italic*ized more**", "## A *b*c"],
    )
    def test_nested_result_is_idempotent(self, text):
        once = markdown_to_whatsapp(text)
        assert markdown_to_whatsapp(once) == once

    def test_italic_running_into_a_word_does_not_split_the_bold(self):
        assert markdown_to_whatsapp("**bold *italic*ized more**") == "*bold _italic_ized more*"

    def test_italic_running_into_a_word_inside_heading(self):
        assert markdown_to_whatsapp("## A *b*c") == "*A _b_c*"

    def test_lone_asterisk_inside_bold_is_left_alone(self):
        assert markdown_to_whatsapp("**2*3 = 6**") == "*2*3 = 6*"


class TestTelegramPipelineFixture:
    """`tests/fixtures/markdown_pipeline.json` pins the whole outbound pipeline:
    model Markdown → WhatsApp markup (this module) → Telegram HTML (the
    telegram-client's `waMarkupToHtml`, asserted in its
    `tests/unit/markdown-pipeline.test.ts` against the SAME file). This side
    checks the first hop, so the two halves can't drift apart silently."""

    CASES = json.loads(PIPELINE_FIXTURE.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
    def test_markdown_to_whatsapp_hop(self, case):
        assert markdown_to_whatsapp(case["markdown"]) == case["whatsapp"]
