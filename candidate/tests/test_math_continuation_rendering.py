r"""Rendering of answers cut off inside mathematics, in a real browser.

The canonical /app/ chat is served from loopback by the existing harness and
driven in headless Edge. window.fetch is replaced so /api/chat returns a fixed
reply; nothing leaves the machine.

- Completed \[...\], \(...\) and $$...$$ units render with KaTeX, as before.
- An unfinished expression at the end of a cut-off reply stays literal text
  with its backslashes (Marked used to drop them, leaving "[ \text{...} \"),
  and is never closed or completed.
- The rest of an expression begun in the previous message (a leading orphan
  closer) also stays literal text instead of losing its backslashes.
- Code fences and inline code are untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from test_homepage_handoff import Site, browser_page, find_browser
from test_homepage_interaction import Page

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOTICE = ("This response was cut off before it finished because it reached the output length limit. "
          "Say \"continue\" to get the rest.")
CALCULUS_CUT = (FIXTURES / "calculus_cutoff.md").read_text(encoding="utf-8").rstrip()

REPLIES = {
    "calculus_cut": CALCULUS_CUT + "\n\n*" + NOTICE + "*",
    "orphan_continuation": (
        r"\text{velocity} \xrightarrow{\text{integral}} \text{position}" "\n" r"\]" "\n\n"
        "This means:\n\n- Integrating velocity gives position.\n\n"
        r"\[" "\n" r"\int x^3\,dx=\frac{x^4}{4}+C" "\n" r"\]"
    ),
    "restated_continuation": (
        r"\[ \text{acceleration} \xrightarrow{\text{integral}} \text{velocity} "
        r"\xrightarrow{\text{integral}} \text{position} \]" "\n\nThis means:\n\n- Integrating velocity gives position."
    ),
    "inline_cut": r"The slope of the tangent line is \(f'(x)=2",
    "dollar_cut": "Completed: $$a^2+b^2=c^2$$ and then $$\\int_0^1 x",
    "inline_code_then_cut": r"Use `\[` first. **Keep this context.** Then \(f(x)=2",
    "double_backtick_then_cut": r"Use ``\[`` first. Then \(f(x)=2",
    "tilde_code_then_cut": "~~~python\npattern = r\"\\[\"\n~~~\n\nThen \\(f(x)=2",
    "math_around_code": "\\[unfinished\n\n```python\npattern = r\"\\]\"\n```",
    "orphan_after_complete": r"First \(x^2\). Then \] and **bold**.",
    "unsafe_cut": r"Before \(x=<img src=x onerror=alert(1)><script>alert(1)</script>",
    "code_with_delimiters": (
        "Use `\\[` to open display math. **Bold stays bold.**\n\n"
        "```python\nimport re\npattern = re.compile(r\"\\[\")\nprint(pattern)\n```\n\n"
        "And inline \\(x^2\\) renders."
    ),
}


def find_edge():
    browser = find_browser()
    if browser is None:
        pytest.skip("no supported Chromium-family browser is installed")
    return browser


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    browser = find_edge()
    profile = tmp_path_factory.mktemp("edge-math-continuation")
    site = Site()
    site.child_passive = True
    results = {}

    with site.http:
        with browser_page(browser, profile, (1024, 900), False) as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/app/"}, session)
            page.eval("""for (let i = 0; i < 400; i++) {
                if (document.getElementById('composer-input') && window.renderMathInElement && window.marked) break;
                await new Promise(r => setTimeout(r, 25)); }
              window.__errors = [];
              window.addEventListener('error', e => window.__errors.push(String(e.message)));
              return true;""")

            for name, reply in REPLIES.items():
                results[name] = page.eval(f"""
                  const reply = {json.dumps(reply)};
                  window.fetch = async () => new Response(JSON.stringify({{reply, session_id: 's-math'}}),
                    {{status: 200, headers: {{'Content-Type': 'application/json'}}}});
                  const before = document.querySelectorAll('#thread .msg-assistant').length;
                  const input = document.getElementById('composer-input');
                  input.value = {json.dumps(name)};
                  document.getElementById('composer').requestSubmit();
                  let body = null;
                  for (let i = 0; i < 400; i++) {{
                    const rows = document.querySelectorAll('#thread .msg-assistant');
                    if (rows.length > before) {{
                      body = rows[rows.length - 1].querySelector('.md');
                      if (body && !body.querySelector('.thinking') && body.textContent.trim()) break;
                    }}
                    await new Promise(r => setTimeout(r, 25));
                  }}
                  await new Promise(r => setTimeout(r, 100));
                  const clone = body.cloneNode(true);
                  clone.querySelectorAll('.katex').forEach(k => k.remove());
                  return {{
                    katex: body.querySelectorAll('.katex').length,
                    katexDisplay: body.querySelectorAll('.katex-display').length,
                    katexErrors: body.querySelectorAll('.katex-error').length,
                    textWithoutMath: clone.textContent,
                    codeBlocks: [...body.querySelectorAll('pre code')].map(c => c.textContent),
                    inlineCode: [...body.querySelectorAll(':not(pre) > code')].map(c => c.textContent),
                    strong: [...body.querySelectorAll('strong')].map(s => s.textContent),
                    listItems: [...body.querySelectorAll('li')].map(l => l.textContent),
                    unsafeElements: body.querySelectorAll("img, script, iframe, [onerror]").length,
                    errors: window.__errors.slice(),
                  }};""")
                time.sleep(0.1)

    return results


def count_complete_units(text):
    import re
    return len(re.findall(r"\\\[[\s\S]*?\\\]", text)) + len(re.findall(r"\\\([\s\S]*?\\\)", text))


def test_completed_math_in_a_cut_off_answer_still_renders(rendered):
    r = rendered["calculus_cut"]
    assert r["katex"] == count_complete_units(CALCULUS_CUT)
    assert r["katexErrors"] == 0 and r["errors"] == []


def test_the_unfinished_display_expression_stays_literal_and_unclosed(rendered):
    text = rendered["calculus_cut"]["textWithoutMath"]
    fragment = r"\[ \text{acceleration} \xrightarrow{\text{integral}}" + " \\"
    assert fragment in text                                          # backslashes preserved
    assert r"\]" not in text.split(fragment, 1)[1]                    # never closed
    assert "[ \\text{acceleration}" not in text.replace(fragment, "")  # no backslash-stripped copy
    assert NOTICE in text


def test_a_leading_orphan_closer_stays_literal_and_later_math_renders(rendered):
    r = rendered["orphan_continuation"]
    assert r"\text{velocity} \xrightarrow{\text{integral}} \text{position}" in r["textWithoutMath"]
    assert r"\]" in r["textWithoutMath"]
    assert r["katexDisplay"] == 1 and r["katexErrors"] == 0
    assert r["listItems"] == ["Integrating velocity gives position."]


def test_a_restated_expression_renders_as_one_complete_unit(rendered):
    r = rendered["restated_continuation"]
    assert r["katexDisplay"] == 1 and r["katexErrors"] == 0
    assert "\\[" not in r["textWithoutMath"] and "\\]" not in r["textWithoutMath"]


def test_an_unfinished_inline_expression_stays_literal(rendered):
    r = rendered["inline_cut"]
    assert r"\(f'(x)=2" in r["textWithoutMath"] and r["katex"] == 0


def test_an_unfinished_dollar_display_stays_literal_after_completed_math(rendered):
    r = rendered["dollar_cut"]
    assert r["katexDisplay"] == 1
    assert "$$\\int_0^1 x" in r["textWithoutMath"]


def test_code_and_inline_code_are_untouched(rendered):
    r = rendered["code_with_delimiters"]
    assert r["codeBlocks"] == ['import re\npattern = re.compile(r"\\[")\nprint(pattern)\n']
    assert "\\[" in r["inlineCode"]
    assert r["strong"] == ["Bold stays bold."]
    assert r["katex"] == 1 and r["errors"] == []


@pytest.mark.parametrize("name", ["inline_code_then_cut", "double_backtick_then_cut", "tilde_code_then_cut"])
def test_code_delimiters_do_not_hide_a_later_unfinished_expression(rendered, name):
    r = rendered[name]
    assert r"\(f(x)=2" in r["textWithoutMath"]
    assert r["katex"] == 0 and r["errors"] == []
    if name == "tilde_code_then_cut":
        assert r["codeBlocks"] == ['pattern = r"\\["\n']
    else:
        assert r["inlineCode"] == [r"\["]
    if name == "inline_code_then_cut":
        assert r["strong"] == ["Keep this context."]


def test_delimiters_are_not_paired_across_a_code_block(rendered):
    r = rendered["math_around_code"]
    assert r"\[unfinished" in r["textWithoutMath"]
    assert r["katex"] == 0 and r["codeBlocks"] == ['pattern = r"\\]"\n']


def test_an_orphan_after_completed_math_preserves_markdown(rendered):
    r = rendered["orphan_after_complete"]
    assert r["katex"] == 1 and r"\]" in r["textWithoutMath"]
    assert r["strong"] == ["bold"]


def test_unfinished_math_is_restored_as_safe_literal_text(rendered):
    r = rendered["unsafe_cut"]
    assert r["unsafeElements"] == 0 and r["errors"] == []
    assert '<img src=x onerror=alert(1)>' in r["textWithoutMath"]
    assert '<script>alert(1)</script>' in r["textWithoutMath"]
