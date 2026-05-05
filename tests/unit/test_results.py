# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name


from searx.result_types import LegacyResult, MainResult
from searx.results import ResultContainer, merge_two_main_results
from tests import SearxTestCase


class ResultContainerTestCase(SearxTestCase):
    # pylint: disable=use-dict-literal

    TEST_SETTINGS = "test_result_container.yml"

    def test_empty(self):
        container = ResultContainer()
        self.assertEqual(container.get_ordered_results(), [])

    def test_one_result(self):
        result = dict(url="https://example.org", title="title ..", content="Lorem ..")

        container = ResultContainer()
        container.extend("google", [result])
        container.close()

        self.assertEqual(len(container.get_ordered_results()), 1)

        res = LegacyResult(result)
        res.normalize_result_fields()
        self.assertIn(res, container.get_ordered_results())

    def test_one_suggestion(self):
        result = dict(suggestion="lorem ipsum ..")

        container = ResultContainer()
        container.extend("duckduckgo", [result])
        container.close()

        self.assertEqual(len(container.get_ordered_results()), 0)
        self.assertEqual(len(container.suggestions), 1)
        self.assertIn(result["suggestion"], container.suggestions)

    def test_merge_url_result(self):
        # from the merge of eng1 and eng2 we expect this result
        result = LegacyResult(
            url="https://example.org", title="very long title, lorem ipsum", content="Lorem ipsum dolor sit amet .."
        )
        result.normalize_result_fields()
        eng1 = dict(url=result.url, title="short title", content=result.content, engine="google")
        eng2 = dict(url="http://example.org", title=result.title, content="lorem ipsum", engine="duckduckgo")

        container = ResultContainer()
        container.extend(None, [eng1, eng2])
        container.close()

        result_list = container.get_ordered_results()
        self.assertEqual(len(container.get_ordered_results()), 1)
        self.assertIn(result, result_list)
        self.assertEqual(result_list[0].title, result.title)
        self.assertEqual(result_list[0].content, result.content)

    def test_merge_url_result_accumulates_metadata(self):
        eng1 = dict(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
            metadata="meta1",
        )
        eng2 = dict(
            url="http://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
            metadata="meta2",
        )

        container = ResultContainer()
        container.extend(None, [eng1, eng2])
        container.close()

        result_list = container.get_ordered_results()
        self.assertEqual(len(result_list), 1)
        self.assertIn("meta1", result_list[0]["metadata"])
        self.assertIn("meta2", result_list[0]["metadata"])

    def test_merge_url_result_metadata_no_duplicate(self):
        eng1 = dict(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
            metadata="same_meta",
        )
        eng2 = dict(
            url="http://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
            metadata="same_meta",
        )

        container = ResultContainer()
        container.extend(None, [eng1, eng2])
        container.close()

        result_list = container.get_ordered_results()
        self.assertEqual(len(result_list), 1)
        self.assertEqual(result_list[0]["metadata"], "same_meta")

    def test_merge_url_result_metadata_one_empty(self):
        eng1 = dict(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
        )
        eng2 = dict(
            url="http://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
            metadata="meta2",
        )

        container = ResultContainer()
        container.extend(None, [eng1, eng2])
        container.close()

        result_list = container.get_ordered_results()
        self.assertEqual(len(result_list), 1)
        self.assertEqual(result_list[0]["metadata"], "meta2")

    def test_merge_url_result_fills_empty_fields(self):
        eng1 = dict(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
        )
        eng2 = dict(
            url="http://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
            thumbnail="https://example.org/thumb.jpg",
            author="author",
        )

        container = ResultContainer()
        container.extend(None, [eng1, eng2])
        container.close()

        result_list = container.get_ordered_results()
        self.assertEqual(len(result_list), 1)
        self.assertEqual(result_list[0]["thumbnail"], "https://example.org/thumb.jpg")
        self.assertEqual(result_list[0]["author"], "author")

    def test_urls_with_different_fragments_do_not_merge(self):
        eng1 = dict(
            url="https://example.org/docs#section-one",
            title="section one",
            content="content",
            engine="google",
        )
        eng2 = dict(
            url="https://example.org/docs#section-two",
            title="section two",
            content="content",
            engine="duckduckgo",
        )

        container = ResultContainer()
        container.extend(None, [eng1, eng2])
        container.close()

        result_list = container.get_ordered_results()
        self.assertEqual(len(result_list), 2)


class MergeMainResultTestCase(SearxTestCase):

    def test_merge_accumulates_metadata(self):
        origin = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
            metadata="meta1",
        )
        other = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
            metadata="meta2",
        )
        merge_two_main_results(origin, other)
        self.assertIn("meta1", origin.metadata)
        self.assertIn("meta2", origin.metadata)

    def test_merge_accumulates_metadata_items(self):
        origin = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
            metadata_items=[{"key": "k1", "value": "v1"}],
        )
        other = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
            metadata_items=[{"key": "k2", "value": "v2"}],
        )
        merge_two_main_results(origin, other)
        self.assertEqual(len(origin.metadata_items), 2)

    def test_merge_metadata_items_no_duplicates(self):
        origin = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
            metadata_items=[{"key": "k1", "value": "v1"}],
        )
        other = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
            metadata_items=[{"key": "k1", "value": "v1"}],
        )
        merge_two_main_results(origin, other)
        self.assertEqual(len(origin.metadata_items), 1)

    def test_merge_metadata_one_empty(self):
        origin = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
        )
        other = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
            metadata="meta2",
        )
        merge_two_main_results(origin, other)
        self.assertEqual(origin.metadata, "meta2")

    def test_merge_metadata_both_empty(self):
        origin = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
        )
        other = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
        )
        merge_two_main_results(origin, other)
        self.assertEqual(origin.metadata, "")

    def test_merge_fills_empty_fields(self):
        origin = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="google",
        )
        other = MainResult(
            url="https://example.org",
            title="title",
            content="content",
            engine="duckduckgo",
            thumbnail="https://example.org/thumb.jpg",
            author="author",
        )
        merge_two_main_results(origin, other)
        self.assertEqual(origin.thumbnail, "https://example.org/thumb.jpg")
        self.assertEqual(origin.author, "author")
