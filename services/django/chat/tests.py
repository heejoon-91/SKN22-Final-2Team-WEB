import json
import uuid
from unittest.mock import patch

import httpx
from django.conf import settings
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from chat.api_views import sessions_proxy_view
from chat.models import ChatMessage, ChatMessageRecommendation, ChatSession
from pets.models import FuturePetProfile, Pet, PetAllergy, PetFoodPreference, PetHealthConcern
from products.models import Product
from users.models import User, UserProfile
from users.onboarding import ONBOARDING_FORCE_PROFILE_SESSION_KEY


class ChatPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="chat-owner@example.com",
            password="Password123!",
        )
        UserProfile.objects.create(user=self.user, nickname="Chat Owner")
        self.client.force_login(self.user)

    def test_chat_page_renders_related_pet_fields(self):
        pet = Pet.objects.create(
            user=self.user,
            name="Nabi",
            species="cat",
            gender="female",
            budget_range="5_10",
        )
        PetHealthConcern.objects.create(pet=pet, concern="skin")
        PetAllergy.objects.create(pet=pet, ingredient="chicken")
        PetFoodPreference.objects.create(pet=pet, food_type="dry")

        response = self.client.get(reverse("chat"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["member_pets"]), 1)
        serialized_pet = response.context["member_pets"][0]
        self.assertEqual(serialized_pet["health_concerns"], ["skin"])
        self.assertEqual(serialized_pet["allergies"], ["chicken"])
        self.assertEqual(serialized_pet["food_preferences"], ["dry"])

    def test_chat_page_profile_menu_includes_wishlist_link(self):
        Pet.objects.create(
            user=self.user,
            name="Bori",
            species="dog",
            gender="male",
            budget_range="5_10",
        )

        response = self.client.get(reverse("chat"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/products/?tab=wishlist")
        self.assertContains(response, "관심 상품")

    def test_chat_page_allows_chat_when_profile_complete_but_no_pet(self):
        response = self.client.get(reverse("chat"))

        self.assertEqual(response.status_code, 200)

    def test_chat_page_allows_future_guardian_profile_without_registered_pet(self):
        FuturePetProfile.objects.create(
            user=self.user,
            preferred_species="dog",
            housing_type="apartment",
            experience_level="first",
            interests=["adoption", "starter_items"],
        )

        response = self.client.get(reverse("chat"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "예비 집사")
        self.assertContains(response, "future-profile")

    def test_chat_page_redirects_to_profile_setup_when_profile_is_incomplete(self):
        incomplete_user = User.objects.create_user(
            email="chat-incomplete@example.com",
            password="Password123!",
        )
        UserProfile.objects.create(user=incomplete_user, nickname="")
        self.client.force_login(incomplete_user)

        response = self.client.get(reverse("chat"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('profile')}?setup=1")

    def test_chat_page_redirects_to_profile_setup_when_new_user_force_flag_exists(self):
        session = self.client.session
        session[ONBOARDING_FORCE_PROFILE_SESSION_KEY] = True
        session.save()

        response = self.client.get(reverse("chat"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{reverse('profile')}?setup=1")


class _FakeStreamResponse:
    def __init__(self, chunks, status_code=200):
        self._chunks = chunks
        self.status_code = status_code
        self.text = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        for chunk in self._chunks:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            for line in text.splitlines():
                yield line

    def json(self):
        return {"detail": "error"}


class _ExplodingHttpxClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, headers=None, json=None):
        raise httpx.ConnectError("connection failed")

    def stream(self, method, url, headers=None, json=None):
        raise httpx.ConnectError("connection failed")


class _TimeoutHttpxClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, headers=None, json=None):
        raise httpx.ReadTimeout("timed out")

    def stream(self, method, url, headers=None, json=None):
        raise httpx.ReadTimeout("timed out")


class _FakeHttpxClient:
    last_stream_request = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, headers=None, json=None):
        self.__class__.last_stream_request = {
            "method": method,
            "url": url,
            "headers": headers,
            "json": json,
        }
        return _FakeStreamResponse(
            [
                b'data: {"type":"token","content":"hello"}\n\n',
                'data: {"type":"products","cards":[{"goods_id":"TEST-PRODUCT-1","product_name":"추천 상품","brand_name":"브랜드","price":10000,"discount_price":9000,"rating":4.5,"reviews":12,"thumbnail_url":"https://example.com/thumb.jpg","product_url":"https://example.com/product"}]}\n\n',
                b'data: {"type":"done"}\n\n',
            ]
        )


class _IncompleteStreamHttpxClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, headers=None, json=None):
        return _FakeStreamResponse(
            [
                'data: {"type":"info","content":"응답 생성 중"}\n\n',
            ]
        )


def _read_streaming_response(response):
    chunks = []
    for chunk in response.streaming_content:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


class ChatProxyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="chat-proxy@example.com",
            password="Password123!",
        )
        UserProfile.objects.create(user=self.user, nickname="ChatProxy")
        self.pet = Pet.objects.create(
            user=self.user,
            name="Nabi",
            species="cat",
            gender="female",
            budget_range="5_10",
        )
        self.product = Product.objects.create(
            goods_id="TEST-PRODUCT-1",
            goods_name="추천 상품",
            brand_name="브랜드",
            price=10000,
            discount_price=9000,
            rating=4.5,
            review_count=12,
            thumbnail_url="https://example.com/thumb.jpg",
            product_url="https://example.com/product",
            soldout_yn=False,
            soldout_reliable=True,
            pet_type=["고양이"],
            category=["사료"],
            subcategory=["전연령"],
            health_concern_tags=[],
            crawled_at=timezone.now(),
        )
        self.other_user = User.objects.create_user(
            email="other-chat-owner@example.com",
            password="Password123!",
        )
        UserProfile.objects.create(user=self.other_user, nickname="OtherUser")
        self.other_session = ChatSession.objects.create(
            user=self.other_user,
            title="다른 사람 세션",
        )

    def test_chat_proxy_requires_authentication(self):
        response = self.client.post(
            "/api/chat/",
            data='{"message":"hello"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    @patch("chat.api_views.httpx.Client", _FakeHttpxClient)
    def test_chat_proxy_streams_fastapi_response_with_internal_auth_headers(self):
        self.client.force_login(self.user)

        response = self.client.post(
            "/api/chat/",
            data='{"message":"hello","thread_id":"thread-1","pet_profile":{"species":"cat"}}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertTrue(response["X-Request-Id"])
        payload = _read_streaming_response(response)
        self.assertIn('"type":"token"', payload)
        self.assertEqual(_FakeHttpxClient.last_stream_request["url"], settings.FASTAPI_INTERNAL_CHAT_URL)
        self.assertEqual(
            _FakeHttpxClient.last_stream_request["headers"]["X-Internal-Service-Token"],
            settings.INTERNAL_SERVICE_TOKEN,
        )
        self.assertEqual(_FakeHttpxClient.last_stream_request["headers"]["X-User-Id"], str(self.user.id))
        self.assertEqual(_FakeHttpxClient.last_stream_request["headers"]["X-Request-Id"], response["X-Request-Id"])
        self.assertEqual(_FakeHttpxClient.last_stream_request["json"]["thread_id"], "thread-1")
        self.assertEqual(_FakeHttpxClient.last_stream_request["json"]["user_id"], str(self.user.id))

    def test_sessions_proxy_crud_and_message_load_are_backed_by_db(self):
        self.client.force_login(self.user)

        existing_session = ChatSession.objects.create(
            user=self.user,
            target_pet=self.pet,
            profile_context_type=ChatSession.PROFILE_CONTEXT_PET,
            title="기존 세션",
        )
        stored_message = ChatMessage.objects.create(
            session=existing_session,
            role="user",
            content="hello",
        )

        list_response = self.client.get("/api/chat/sessions/")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["sessions"][0]["session_id"], str(existing_session.session_id))
        self.assertEqual(list_response.json()["sessions"][0]["profile_context_type"], ChatSession.PROFILE_CONTEXT_PET)
        self.assertEqual(list_response.json()["groups"][0]["key"], "today")

        create_response = self.client.post(
            "/api/chat/sessions/",
            data=json.dumps(
                {
                    "title": "신규 세션",
                    "target_pet_id": str(self.pet.pet_id),
                    "profile_context_type": ChatSession.PROFILE_CONTEXT_PET,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(create_response.status_code, 201)
        self.assertEqual(create_response.json()["title"], "신규 세션")
        self.assertEqual(create_response.json()["profile_context_type"], ChatSession.PROFILE_CONTEXT_PET)
        created_session = ChatSession.objects.get(session_id=create_response.json()["session_id"])
        self.assertEqual(created_session.user_id, self.user.id)
        self.assertEqual(created_session.target_pet_id, self.pet.pet_id)
        self.assertEqual(created_session.profile_context_type, ChatSession.PROFILE_CONTEXT_PET)

        patch_response = self.client.patch(
            f"/api/chat/sessions/{existing_session.session_id}/",
            data=json.dumps(
                {
                    "title": "수정 제목",
                    "profile_context_type": ChatSession.PROFILE_CONTEXT_FUTURE,
                    "target_pet_id": None,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.json()["title"], "수정 제목")
        self.assertEqual(patch_response.json()["profile_context_type"], ChatSession.PROFILE_CONTEXT_FUTURE)
        existing_session.refresh_from_db()
        self.assertEqual(existing_session.title, "수정 제목")
        self.assertEqual(existing_session.profile_context_type, ChatSession.PROFILE_CONTEXT_FUTURE)
        self.assertIsNone(existing_session.target_pet_id)

        messages_response = self.client.get(f"/api/chat/sessions/{existing_session.session_id}/messages/")
        self.assertEqual(messages_response.status_code, 200)
        self.assertEqual(messages_response.json()["messages"][0]["content"], "hello")
        self.assertEqual(messages_response.json()["messages"][0]["message_id"], str(stored_message.message_id))

        delete_response = self.client.delete(f"/api/chat/sessions/{created_session.session_id}/")
        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(delete_response.json()["deleted"])
        self.assertFalse(ChatSession.objects.filter(session_id=created_session.session_id).exists())

    @patch("chat.api_views.httpx.Client", _FakeHttpxClient)
    def test_session_messages_proxy_persists_user_and_assistant_messages(self):
        self.client.force_login(self.user)
        secondary_pet = Pet.objects.create(
            user=self.user,
            name="Bori",
            species="dog",
            gender="male",
            budget_range="10_15",
        )
        session = ChatSession.objects.create(user=self.user, target_pet=self.pet, title="추천 세션")

        response = self.client.post(
            f"/api/chat/sessions/{session.session_id}/messages/",
            data='{"message":"hello","pet_profile":{"species":"cat"}}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertTrue(response["X-Request-Id"])
        payload = _read_streaming_response(response)
        self.assertIn('"type":"done"', payload)
        self.assertEqual(_FakeHttpxClient.last_stream_request["url"], settings.FASTAPI_INTERNAL_CHAT_URL)
        self.assertEqual(_FakeHttpxClient.last_stream_request["headers"]["X-Request-Id"], response["X-Request-Id"])
        self.assertEqual(_FakeHttpxClient.last_stream_request["json"]["message"], "hello")
        self.assertEqual(_FakeHttpxClient.last_stream_request["json"]["thread_id"], str(session.session_id))
        self.assertEqual(_FakeHttpxClient.last_stream_request["json"]["user_id"], str(self.user.id))
        self.assertEqual(_FakeHttpxClient.last_stream_request["json"]["target_pet_id"], str(self.pet.pet_id))
        self.assertNotEqual(_FakeHttpxClient.last_stream_request["json"]["target_pet_id"], str(secondary_pet.pet_id))

        messages = list(session.messages.order_by("created_at").values_list("role", "content"))
        self.assertEqual(messages, [("user", "hello"), ("assistant", "hello")])
        assistant_message = session.messages.get(role="assistant")
        recommendation = ChatMessageRecommendation.objects.get(message=assistant_message)
        self.assertEqual(recommendation.product_id, self.product.goods_id)
        self.assertEqual(recommendation.rank_order, 0)

        list_response = self.client.get(f"/api/chat/sessions/{session.session_id}/messages/")
        self.assertEqual(list_response.status_code, 200)
        assistant_payload = list_response.json()["messages"][1]
        self.assertEqual(assistant_payload["recommended_products"][0]["goods_id"], self.product.goods_id)

    @patch("chat.api_views.httpx.Client", _ExplodingHttpxClient)
    def test_session_messages_proxy_persists_error_message_when_fastapi_is_unreachable(self):
        self.client.force_login(self.user)
        session = ChatSession.objects.create(user=self.user, title="실패 세션")

        response = self.client.post(
            f"/api/chat/sessions/{session.session_id}/messages/",
            data='{"message":"hello"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = _read_streaming_response(response)
        self.assertIn('"type": "error"', payload)
        self.assertIn("채팅 서버와 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.", payload)
        messages = list(session.messages.order_by("created_at").values_list("role", "content"))
        self.assertEqual(
            messages,
            [
                ("user", "hello"),
                ("assistant", "채팅 서버와 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."),
            ],
        )

    @patch("chat.api_views.httpx.Client", _IncompleteStreamHttpxClient)
    def test_session_messages_proxy_emits_error_when_upstream_ends_without_terminal_event(self):
        self.client.force_login(self.user)
        session = ChatSession.objects.create(user=self.user, title="비정상 종료 세션")

        response = self.client.post(
            f"/api/chat/sessions/{session.session_id}/messages/",
            data='{"message":"hello"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = _read_streaming_response(response)
        self.assertIn('"type": "error"', payload)
        self.assertIn("응답 생성이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.", payload)
        messages = list(session.messages.order_by("created_at").values_list("role", "content"))
        self.assertEqual(
            messages,
            [
                ("user", "hello"),
                ("assistant", "응답 생성이 지연되고 있습니다. 잠시 후 다시 시도해 주세요."),
            ],
        )

    def test_session_messages_proxy_rejects_other_users_session_access(self):
        self.client.force_login(self.user)

        response = self.client.get(f"/api/chat/sessions/{self.other_session.session_id}/messages/")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "대화를 찾을 수 없습니다.")

    @patch("chat.api_views._get_owned_target_pet", return_value=None)
    def test_sessions_proxy_rejects_unknown_target_pet(self, mocked_get_owned_target_pet):
        missing_pet_id = str(uuid.uuid4())
        request = RequestFactory().post(
            "/api/chat/sessions/",
            data=json.dumps(
                {
                    "title": "신규 세션",
                    "target_pet_id": missing_pet_id,
                    "profile_context_type": ChatSession.PROFILE_CONTEXT_PET,
                }
            ),
            content_type="application/json",
        )
        request.user = self.user
        response = sessions_proxy_view(request)
        payload = json.loads(response.content.decode("utf-8"))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(payload["detail"], "선택한 반려동물을 찾을 수 없습니다.")
        mocked_get_owned_target_pet.assert_called_once()

    def test_sessions_proxy_persists_none_profile_context_without_target_pet(self):
        self.client.force_login(self.user)

        response = self.client.post(
            "/api/chat/sessions/",
            data=json.dumps(
                {
                    "title": "선택 안 함 세션",
                    "profile_context_type": ChatSession.PROFILE_CONTEXT_NONE,
                    "target_pet_id": str(self.pet.pet_id),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        created_session = ChatSession.objects.get(session_id=response.json()["session_id"])
        self.assertEqual(created_session.profile_context_type, ChatSession.PROFILE_CONTEXT_NONE)
        self.assertIsNone(created_session.target_pet_id)
