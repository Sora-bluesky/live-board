from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


STATIC = Path(__file__).resolve().parents[1] / "skills/live-board/scripts/static"


class Navigation(HTMLParser):
    def __init__(self):
        super().__init__()
        self.views = []

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "a" and "data-view" in attributes:
            self.views.append(attributes["data-view"])


class StaticTests(unittest.TestCase):
    def test_every_navigation_view_has_a_renderer(self):
        navigation = Navigation()
        navigation.feed((STATIC / "index.html").read_text(encoding="utf-8"))
        expected = {"summary", "timeline", "backlog", "board", "list", "releases"}
        self.assertEqual(set(navigation.views), expected)
        self.assertEqual(len(navigation.views), len(expected))
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        registry = re.search(r"const views = \{(.*?)\n\};", script, re.DOTALL)
        self.assertIsNotNone(registry, "A single named view registry is required")
        registered = set(re.findall(r'^  "?([\w-]+)"?: render\w+,?$', registry.group(1), re.MULTILINE))
        self.assertEqual(registered, expected | {"projects", "for-you"})
        for renderer in re.findall(r": (render\w+)", registry.group(1)):
            self.assertRegex(script, rf"function {renderer}\(")

    def test_static_files_are_utf8_with_lf(self):
        for name in ("index.html", "app.js", "style.css"):
            with self.subTest(name=name):
                content = (STATIC / name).read_bytes()
                self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
                self.assertNotIn(b"\r", content)
                content.decode("utf-8")

    def test_mobile_history_replacement_contract(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertEqual(script.count('history[replace ? "replaceState" : "pushState"](null, "", hash)'), 1)
        self.assertEqual(script.count('history.replaceState(null, "", hashFor())'), 1)
        self.assertEqual(script.count("location.hash"), 4)

    def test_style_warning_disclosure_is_collapsed_and_neutral(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<details id="style-warnings" class="style-warnings" hidden>', html)
        self.assertLess(html.index('id="project-meta"'), html.index('id="style-warnings"'))
        self.assertLess(html.index('id="style-warnings"'), html.index('id="project-tabs"'))
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        rule = re.search(r"\.style-warnings\s*\{([^}]*)\}", css).group(1)
        self.assertIn("color: var(--muted);", rule)
        self.assertNotIn("background", rule)

    def test_sticky_content_headers_share_the_measured_global_header_height(self):
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        def rule(selector):
            match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
            self.assertIsNotNone(match, selector)
            return match.group(1)

        self.assertIn("--header-height: 96px;", rule(":root"))
        self.assertEqual(len(re.findall(r"--header-height\s*:", css)), 1)
        for selector in (".sidebar", ".workspace", ".board-column h3"):
            self.assertNotIn("96px", rule(selector), selector)
        sidebar = rule(".sidebar")
        self.assertIn("top: var(--header-height);", sidebar)
        self.assertIn("height: calc(100dvh - var(--header-height));", sidebar)
        for selector in (".board-column h3", ".disclosure > summary", ".next-grid h3"):
            rules = re.findall(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
            declarations = " ".join(rules)
            with self.subTest(selector=selector):
                self.assertIn("position: sticky;", declarations)
                self.assertIn("var(--header-height)", declarations)
        column = rule(".board-column h3")
        self.assertIn("background: var(--column);", column)
        self.assertIn("var(--swimlane-header-height, 0px)", column)
        self.assertLess(int(re.search(r"z-index: (\d+)", column).group(1)),
                        int(re.search(r"z-index: (\d+)", rule(".global-header")).group(1)))
        self.assertIn("background: var(--surface);", rule(".swimlane > summary"))
        self.assertIn("position: sticky;", rule(".table-box thead"))
        self.assertIn("var(--table-header-offset, 0px)", rule(".table-box thead"))
        self.assertIn('new ResizeObserver(updateStickyOffsets)', script)
        self.assertIn('setProperty("--header-height", `${header.height}px`)', script)

    def test_board_titles_only_are_clamped(self):
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        selectors = re.findall(r"([^{}]+)\{([^{}]*-webkit-line-clamp[^{}]*)\}", css)
        self.assertEqual(len(selectors), 1)
        selector, declarations = selectors[0]
        self.assertEqual(selector.strip(), ".board-card .card-title")
        for expected in ("display: -webkit-box;", "-webkit-box-orient: vertical;",
                         "-webkit-line-clamp: 3;", "overflow: hidden;", "overflow-wrap: anywhere;"):
            self.assertIn(expected, declarations)


if __name__ == "__main__":
    unittest.main()
