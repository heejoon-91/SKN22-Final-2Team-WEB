import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

fake_openai = types.ModuleType("openai")


class _FakeOpenAI:
    def __init__(self, *args, **kwargs):
        pass


fake_openai.OpenAI = _FakeOpenAI
sys.modules.setdefault("openai", fake_openai)

fake_sqlalchemy = types.ModuleType("sqlalchemy")


def _fake_create_engine(*args, **kwargs):
    return object()


def _fake_text(value):
    return value


fake_sqlalchemy.create_engine = _fake_create_engine
fake_sqlalchemy.text = _fake_text
sys.modules.setdefault("sqlalchemy", fake_sqlalchemy)

fake_langgraph = types.ModuleType("langgraph")
fake_langgraph_graph = types.ModuleType("langgraph.graph")
fake_langgraph_message = types.ModuleType("langgraph.graph.message")


def _fake_add_messages(messages, incoming):
    return messages + incoming


fake_langgraph_message.add_messages = _fake_add_messages
sys.modules.setdefault("langgraph", fake_langgraph)
sys.modules.setdefault("langgraph.graph", fake_langgraph_graph)
sys.modules.setdefault("langgraph.graph.message", fake_langgraph_message)

fake_fastapi = types.ModuleType("fastapi")


class _FakeRouter:
    def get(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


def _fake_query(default=None, **kwargs):
    return default


fake_fastapi.APIRouter = _FakeRouter
fake_fastapi.Query = _fake_query
sys.modules.setdefault("fastapi", fake_fastapi)

from pipeline.nodes.recommend import _split_keywords  # noqa: E402
from pipeline.utils import _compile_filters  # noqa: E402


class SearchUtilsTest(unittest.TestCase):
    def test_compile_product_filters_and_exclusions(self):
        where_clause, params = _compile_filters(
            "products",
            {
                "sold_out": False,
                "pet_type": "dog",
                "category": "사료",
                "subcategory": "퍼피",
                "price_lte": 50000,
            },
            {
                "main_ingredients": ["치킨", "소고기"],
            },
        )

        self.assertIn("sold_out = :sold_out", where_clause)
        self.assertIn("pet_type && CAST(:pet_type_values AS TEXT[])", where_clause)
        self.assertIn("category && CAST(:category_values AS TEXT[])", where_clause)
        self.assertIn("subcategory && CAST(:subcategory_values AS TEXT[])", where_clause)
        self.assertIn("COALESCE(discount_price, price) <= :price_lte", where_clause)
        self.assertIn("NOT (main_ingredients && CAST(:exclude_main_ingredients AS TEXT[]))", where_clause)
        self.assertEqual(params["pet_type_values"], ["dog"])
        self.assertEqual(params["category_values"], ["사료"])
        self.assertEqual(params["subcategory_values"], ["퍼피"])
        self.assertEqual(params["price_lte"], 50000)
        self.assertEqual(params["exclude_main_ingredients"], ["치킨", "소고기"])

    def test_compile_domain_filters_uses_any(self):
        where_clause, params = _compile_filters(
            "domain_qna",
            {"species": ["dog", "both"], "category": "건강 및 질병"},
            None,
        )

        self.assertIn("species = ANY(CAST(:species_values AS TEXT[]))", where_clause)
        self.assertIn("category = ANY(CAST(:category_values AS TEXT[]))", where_clause)
        self.assertEqual(params["species_values"], ["dog", "both"])
        self.assertEqual(params["category_values"], ["건강 및 질병"])

    def test_split_keywords_normalizes_delimiters(self):
        self.assertEqual(
            _split_keywords("관절 보조제, 눈물 제거제/피부 관리\n면역"),
            ["관절 보조제", "눈물 제거제", "피부 관리", "면역"],
        )


class RecommendRouterTest(unittest.TestCase):
    def test_recommend_router_shapes_results(self):
        import asyncio

        import routers.recommend as recommend_module

        with patch.object(recommend_module, "hybrid_search") as hybrid_search_mock:
            hybrid_search_mock.return_value = [
                type("Point", (), {"payload": {"goods_id": "G1"}, "score": 0.9})(),
            ]

            response = asyncio.run(
                recommend_module.recommend(
                    query="테스트",
                    pet_type="dog",
                    category="사료",
                    subcategory=None,
                    limit=3,
                )
            )

        self.assertEqual(response["results"], [{"goods_id": "G1", "_score": 0.9}])


if __name__ == "__main__":
    unittest.main()
