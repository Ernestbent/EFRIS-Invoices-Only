# Copyright (c) 2026, Othieno Benedict Ernest and contributors
# See license.txt

import base64
import gzip
import json
import unittest

from efris.efris.doctype.efris_goods_category.efris_goods_category import (
    normalize_category,
    parse_t124_content,
)


class TestEFRISGoodsCategory(unittest.TestCase):
    def test_parse_base64_plain_text_response(self):
        payload = {"page": {"pageCount": "1"}, "records": [{"commodityCategoryCode": "1"}]}
        content = base64.b64encode(json.dumps(payload).encode()).decode()

        self.assertEqual(parse_t124_content({"data": {"content": content}}), payload)

    def test_parse_base64_gzip_response(self):
        payload = {"page": {"pageCount": "2"}, "records": [{"commodityCategoryCode": "2"}]}
        compressed = gzip.compress(json.dumps(payload).encode())
        content = base64.b64encode(compressed).decode()

        self.assertEqual(parse_t124_content({"data": {"content": content}}), payload)

    def test_normalize_category_flags(self):
        category = normalize_category(
            {
                "commodityCategoryCode": "40151524",
                "commodityCategoryName": "Oil Pipe",
                "commodityCategoryLevel": "4",
                "isLeafNode": "101",
                "serviceMark": "102",
                "enableStatusCode": "1",
                "excisable": "102",
            },
            synced_on="2026-08-21 12:00:00",
        )

        self.assertEqual(category["category_code"], "40151524")
        self.assertEqual(category["category_level"], 4)
        self.assertEqual(category["is_leaf_node"], 1)
        self.assertEqual(category["is_service"], 0)
        self.assertEqual(category["enabled"], 1)
