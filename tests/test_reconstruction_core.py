import tempfile
import unittest
from pathlib import Path

from book_dashboard.model import PipelineError
from book_dashboard.reconstruction.checkpoint import CheckpointStore, checkpoint_key
from book_dashboard.reconstruction.pages import parse_pages


class PageSelectionTests(unittest.TestCase):
    def test_list_and_ranges_are_inclusive_one_based_and_sorted(self):
        self.assertEqual(parse_pages("20,10-12,10", total=415), [10, 11, 12, 20])

    def test_default_trial_pages(self):
        self.assertEqual(parse_pages(None, total=415), [10, 20])

    def test_all_pages(self):
        self.assertEqual(parse_pages(None, total=3, all_pages=True), [1, 2, 3])

    def test_all_pages_conflicts_with_explicit_pages(self):
        with self.assertRaises(PipelineError):
            parse_pages("1", total=3, all_pages=True)

    def test_rejects_out_of_range_zero_and_garbage(self):
        for bad in ("0", "416", "5-3", "a", "1,,2", ""):
            with self.subTest(bad=bad), self.assertRaises(PipelineError):
                parse_pages(bad, total=415)


class CheckpointTests(unittest.TestCase):
    CONFIG = {"force_ocr": True, "use_llm": False}
    MODELS = {"marker-pdf": "1.10.1"}

    def key(self, **kwargs):
        args = {"checksum": "abc", "page": 10, "config": self.CONFIG, "models": self.MODELS}
        return checkpoint_key(**{**args, **kwargs})

    def test_key_changes_with_every_component(self):
        base = self.key()
        self.assertEqual(base, self.key())
        self.assertNotEqual(base, self.key(checksum="abd"))
        self.assertNotEqual(base, self.key(page=11))
        self.assertNotEqual(base, self.key(config={"force_ocr": False}))
        self.assertNotEqual(base, self.key(models={"marker-pdf": "1.10.2"}))

    def test_config_key_order_is_irrelevant(self):
        self.assertEqual(self.key(config={"a": 1, "b": 2}), self.key(config={"b": 2, "a": 1}))

    def test_store_roundtrip_and_stale_key_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            store.save(10, self.key(), {"markdown": "x"})
            self.assertEqual(store.load(10, self.key()), {"markdown": "x"})
            self.assertIsNone(store.load(10, self.key(checksum="other")))
            self.assertIsNone(store.load(11, self.key()))

    def test_corrupt_checkpoint_is_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp))
            store.save(10, self.key(), {"markdown": "x"})
            (Path(tmp) / "page_0010.json").write_text("{", encoding="utf-8")
            self.assertIsNone(store.load(10, self.key()))
