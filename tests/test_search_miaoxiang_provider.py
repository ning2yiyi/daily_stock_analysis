# -*- coding: utf-8 -*-
"""
Unit tests for MiaoXiangSearchProvider (东方财富妙想资讯搜索).
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

# Mock newspaper before search_service import (optional dependency)
if "newspaper" not in sys.modules:
    mock_np = MagicMock()
    mock_np.Article = MagicMock()
    mock_np.Config = MagicMock()
    sys.modules["newspaper"] = mock_np

from src.search_service import MiaoXiangSearchProvider, SearchService


def _make_provider(key: str = "test_key") -> MiaoXiangSearchProvider:
    return MiaoXiangSearchProvider([key])


def _mock_response(payload: dict, status_code: int = 200):
    """Build a mock requests.Response with .json() returning payload."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


class TestMiaoXiangExtractTrunkText(unittest.TestCase):
    """Unit tests for _extract_trunk_text static method."""

    def test_empty_string(self):
        self.assertEqual(MiaoXiangSearchProvider._extract_trunk_text(""), "")

    def test_none(self):
        self.assertEqual(MiaoXiangSearchProvider._extract_trunk_text(None), "")

    def test_plain_string(self):
        self.assertEqual(MiaoXiangSearchProvider._extract_trunk_text("hello"), "hello")

    def test_list_of_strings(self):
        result = MiaoXiangSearchProvider._extract_trunk_text(["a", "b", "c"])
        self.assertEqual(result, "a\nb\nc")

    def test_list_of_dicts_content_key(self):
        result = MiaoXiangSearchProvider._extract_trunk_text([{"content": "foo"}, {"content": "bar"}])
        self.assertEqual(result, "foo\nbar")

    def test_list_of_dicts_text_key(self):
        result = MiaoXiangSearchProvider._extract_trunk_text([{"text": "hello"}])
        self.assertEqual(result, "hello")

    def test_list_mixed(self):
        result = MiaoXiangSearchProvider._extract_trunk_text(["plain", {"content": "rich"}])
        self.assertEqual(result, "plain\nrich")

    def test_dict_content_key(self):
        result = MiaoXiangSearchProvider._extract_trunk_text({"content": "bar"})
        self.assertEqual(result, "bar")

    def test_dict_text_key_fallback(self):
        result = MiaoXiangSearchProvider._extract_trunk_text({"text": "baz"})
        self.assertEqual(result, "baz")

    def test_unknown_structure_serialized_as_json(self):
        result = MiaoXiangSearchProvider._extract_trunk_text({"unknown_key": "val"})
        self.assertIn("unknown_key", result)


class TestMiaoXiangDoSearch(unittest.TestCase):
    """Tests for _do_search: HTTP success/failure and response parsing."""

    def _patch_post(self, payload: dict, status_code: int = 200, raise_exc=None):
        mock_resp = _mock_response(payload, status_code)
        if raise_exc:
            mock_resp.raise_for_status.side_effect = raise_exc
        return patch(
            "src.search_service._post_with_retry",
            return_value=mock_resp,
        )

    def test_success_with_title_and_string_trunk(self):
        provider = _make_provider()
        payload = {
            "status": 0,
            "data": {
                "title": "贵州茅台最新研报",
                "trunk": "机构上调目标价至1800元，维持买入评级。",
                "secuList": [{"secuCode": "600519", "secuName": "贵州茅台"}],
            },
        }
        with self._patch_post(payload):
            resp = provider._do_search("贵州茅台研报", "key", max_results=5)

        self.assertTrue(resp.success)
        self.assertEqual(resp.provider, "东方财富妙想")
        self.assertEqual(len(resp.results), 1)
        result = resp.results[0]
        self.assertEqual(result.title, "贵州茅台最新研报")
        self.assertIn("贵州茅台(600519)", result.snippet)
        self.assertIn("机构上调目标价", result.snippet)

    def test_success_secu_list_limited_to_five(self):
        provider = _make_provider()
        secus = [{"secuCode": str(i), "secuName": f"股票{i}"} for i in range(10)]
        payload = {
            "status": 0,
            "data": {"title": "T", "trunk": "content", "secuList": secus},
        }
        with self._patch_post(payload):
            resp = provider._do_search("test", "key", max_results=5)

        self.assertTrue(resp.success)
        # At most 5 securities in the snippet prefix
        snippet = resp.results[0].snippet
        secu_part = snippet.split("\n")[0]
        self.assertLessEqual(secu_part.count("股票"), 5)

    def test_api_error_code_113(self):
        provider = _make_provider()
        payload = {"status": 113, "data": {}}
        with self._patch_post(payload):
            resp = provider._do_search("query", "key", max_results=5)

        self.assertFalse(resp.success)
        self.assertIn("上限", resp.error_message)

    def test_api_error_code_114_invalid_key(self):
        provider = _make_provider()
        payload = {"status": 114, "data": {}}
        with self._patch_post(payload):
            resp = provider._do_search("query", "key", max_results=5)

        self.assertFalse(resp.success)
        self.assertIn("MX_APIKEY", resp.error_message)

    def test_empty_data_returns_failure(self):
        provider = _make_provider()
        payload = {"status": 0, "data": {"title": "", "trunk": "", "secuList": []}}
        with self._patch_post(payload):
            resp = provider._do_search("query", "key", max_results=5)

        self.assertFalse(resp.success)

    def test_network_error_returns_failure(self):
        import requests
        provider = _make_provider()
        with patch(
            "src.search_service._post_with_retry",
            side_effect=requests.exceptions.ConnectionError("timeout"),
        ):
            resp = provider._do_search("query", "key", max_results=5)

        self.assertFalse(resp.success)
        self.assertIn("timeout", resp.error_message)

    def test_trunk_as_list_of_dicts(self):
        provider = _make_provider()
        payload = {
            "status": 0,
            "data": {
                "title": "Test",
                "trunk": [{"content": "segment one"}, {"content": "segment two"}],
                "secuList": [],
            },
        }
        with self._patch_post(payload):
            resp = provider._do_search("query", "key", max_results=5)

        self.assertTrue(resp.success)
        self.assertIn("segment one", resp.results[0].snippet)
        self.assertIn("segment two", resp.results[0].snippet)

    def test_snippet_truncated_to_2000_chars(self):
        provider = _make_provider()
        long_text = "x" * 5000
        payload = {
            "status": 0,
            "data": {"title": "T", "trunk": long_text, "secuList": []},
        }
        with self._patch_post(payload):
            resp = provider._do_search("query", "key", max_results=5)

        self.assertTrue(resp.success)
        self.assertLessEqual(len(resp.results[0].snippet), 2000)


class TestSearchServiceMxKeyRegistration(unittest.TestCase):
    """Verify SearchService registers MiaoXiangSearchProvider when mx_keys provided."""

    def test_provider_registered_as_first_with_mx_keys(self):
        svc = SearchService(mx_keys=["test_key"])
        first = svc._providers[0]
        self.assertIsInstance(first, MiaoXiangSearchProvider)
        self.assertEqual(first.name, "东方财富妙想")

    def test_no_provider_without_mx_keys(self):
        svc = SearchService()
        names = [p.name for p in svc._providers]
        self.assertNotIn("东方财富妙想", names)

    def test_mx_provider_before_bocha(self):
        svc = SearchService(mx_keys=["mx"], bocha_keys=["bocha"])
        names = [p.name for p in svc._providers]
        self.assertLess(names.index("东方财富妙想"), names.index("Bocha"))


if __name__ == "__main__":
    unittest.main()
