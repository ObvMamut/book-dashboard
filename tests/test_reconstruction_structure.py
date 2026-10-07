import unittest

from book_dashboard.reconstruction.structure import (
    build_page,
    clean_markdown,
    full_page_figures,
    math_problem,
    prose,
)

PROSE = "Soit a un entier relatif et b un entier naturel non nul, alors il existe un couple."


def result(markdown, *, images=None, structure=None, page=10):
    return {
        "page": page,
        "markdown": markdown,
        "images": images or {},
        "structure": structure or [],
    }


class CleanTests(unittest.TestCase):
    def test_whitespace_only_and_math_untouched(self):
        raw = "Lorsque  $a  +  b$  sont   des  entiers.   \n\n\n\n$$x  =  y$$\n"
        self.assertEqual(
            clean_markdown(raw), "Lorsque $a  +  b$ sont des entiers.\n\n$$x  =  y$$\n"
        )

    def test_accents_and_leading_indent_preserved(self):
        raw = "  - À l’école, œuvre « é »\n"
        self.assertEqual(clean_markdown(raw), "- À l’école, œuvre « é »\n".replace("- ", "- ", 1))

    def test_prose_excludes_math_and_images(self):
        self.assertEqual(prose("Texte $x^2$ ![a](b.png) fin"), "Texte fin")


class MathTests(unittest.TestCase):
    def test_problems(self):
        self.assertIsNone(math_problem(r"\frac{a}{b}"))
        self.assertIsNone(math_problem(r"\{a\}"))
        self.assertEqual(math_problem(r"\frac{a"), "unbalanced braces")
        self.assertEqual(math_problem(r"\left( x"), "unbalanced \\left/\\right")
        self.assertEqual(math_problem(""), "empty formula")


class BuildPageTests(unittest.TestCase):
    def test_clean_page_has_no_issues_and_counts_formulas(self):
        markdown, issues, stats = build_page(result(f"{PROSE} $a\\in\\mathbb{{Z}}$\n"))
        self.assertEqual(issues, [])
        self.assertTrue(markdown.startswith("<!-- page 10 -->"))
        self.assertEqual(stats["formulas"], 1)

    def test_figure_path_is_rewritten(self):
        images = {"f.jpeg": {"path": "figures/page_0010/f.jpeg"}}
        markdown, issues, _ = build_page(result(f"{PROSE}\n\n![](f.jpeg)\n", images=images))
        self.assertIn("![](figures/page_0010/f.jpeg)", markdown)
        self.assertEqual(issues, [])

    def test_missing_figure_file_is_an_error(self):
        _, issues, _ = build_page(result(f"{PROSE}\n\n![](gone.jpeg)\n"))
        self.assertEqual([i.kind for i in issues], ["missing_figure"])

    def test_full_page_picture_is_flagged_and_not_embedded(self):
        structure = [
            {
                "id": "/page/0/Page/1",
                "block_type": "Page",
                "bbox": [0, 0, 600, 900],
                "children": [
                    {"id": "/page/0/Picture/3", "block_type": "Picture", "bbox": [0, 0, 590, 880]}
                ],
            }
        ]
        images = {"p.jpeg": {"path": "figures/page_0010/p.jpeg"}}
        self.assertEqual(full_page_figures(structure), ["/page/0/Picture/3"])
        markdown, issues, stats = build_page(
            result("![](p.jpeg)\n", images=images, structure=structure)
        )
        self.assertNotIn("](figures", markdown)
        self.assertIn("full_page_figure", [i.kind for i in issues])
        self.assertEqual(stats["figures"], 0)

    def test_small_picture_is_fine(self):
        structure = [
            {
                "id": "p",
                "block_type": "Page",
                "bbox": [0, 0, 600, 900],
                "children": [{"id": "q", "block_type": "Picture", "bbox": [0, 0, 100, 100]}],
            }
        ]
        self.assertEqual(full_page_figures(structure), [])

    def test_empty_page_and_suspicious_math(self):
        _, issues, _ = build_page(result(""))
        self.assertEqual([i.kind for i in issues], ["no_text"])
        _, issues, _ = build_page(result(f"{PROSE} $\\frac{{a$"))
        self.assertEqual([(i.kind, i.severity) for i in issues], [("suspicious_math", "warning")])
