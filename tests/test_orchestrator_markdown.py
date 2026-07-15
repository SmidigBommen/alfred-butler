import unittest

from alfred_tools.orchestrator.markdown import parse_markdown


class MarkdownRenderingTests(unittest.TestCase):
    def test_parses_headings_and_github_style_tables(self):
        blocks = parse_markdown(
            """## Popular bulbs

| Category | Bulb | Popularity |
|:--|:--|--:|
| Spring | **Tulips** | Very high |
| Summer | Dahlias | High |
"""
        )

        self.assertEqual(blocks[0]["type"], "heading")
        self.assertEqual(blocks[0]["level"], 2)
        self.assertEqual(blocks[1]["type"], "table")
        self.assertEqual(blocks[1]["align"], ["left", "left", "right"])
        self.assertEqual(blocks[1]["headers"][1], [{"type": "text", "text": "Bulb"}])
        self.assertEqual(
            blocks[1]["rows"][0][1],
            [{"type": "strong", "content": [{"type": "text", "text": "Tulips"}]}],
        )

    def test_parses_a_link_nested_inside_bold_text(self):
        blocks = parse_markdown("**[Lloyd's Register](https://example.com/ships)**")

        self.assertEqual(
            blocks[0]["content"],
            [
                {
                    "type": "strong",
                    "content": [
                        {
                            "type": "link",
                            "content": [{"type": "text", "text": "Lloyd's Register"}],
                            "url": "https://example.com/ships",
                        }
                    ],
                }
            ],
        )

    def test_keeps_html_inert_and_allows_only_public_web_link_schemes(self):
        blocks = parse_markdown(
            "<script>alert('no')</script> [bad](javascript:alert(1)) "
            "[good](https://example.com/source)"
        )

        content = blocks[0]["content"]
        self.assertIn("<script>alert('no')</script>", content[0]["text"])
        links = [token for token in content if token["type"] == "link"]
        self.assertEqual(
            links,
            [
                {
                    "type": "link",
                    "content": [{"type": "text", "text": "good"}],
                    "url": "https://example.com/source",
                }
            ],
        )
        self.assertIn(
            "[bad](javascript:alert(1))", "".join(token.get("text", "") for token in content)
        )

    def test_parses_lists_quotes_and_fenced_code_without_executing_it(self):
        blocks = parse_markdown(
            """1. First
2. Second

> A useful note

```html
<img src=x onerror=alert(1)>
```
"""
        )

        self.assertEqual([block["type"] for block in blocks], ["list", "quote", "code"])
        self.assertTrue(blocks[0]["ordered"])
        self.assertEqual(blocks[2]["language"], "html")
        self.assertIn("onerror", blocks[2]["text"])


if __name__ == "__main__":
    unittest.main()
