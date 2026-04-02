import json
import time
import uuid
from datetime import timedelta

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from pets.models import Pet
from products.models import Product

from .models import ChatMessage, ChatMessageRecommendation, ChatSession


def _trace_log(event, request_id=None, **fields):
    payload = {"request_id": request_id} if request_id else {}
    payload.update({key: value for key, value in fields.items() if value is not None})
    parts = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, default=str)}"
        for key, value in sorted(payload.items())
    )
    print(f"[DJANGO_CHAT_TRACE] event={event} {parts}".rstrip())


def _normalize_profile_context_type(raw_value):
    value = (raw_value or "").strip().lower()
    if value in {
        ChatSession.PROFILE_CONTEXT_PET,
        ChatSession.PROFILE_CONTEXT_FUTURE,
        ChatSession.PROFILE_CONTEXT_NONE,
    }:
        return value
    return ChatSession.PROFILE_CONTEXT_NONE


def _chat_base_url():
    return settings.FASTAPI_INTERNAL_CHAT_URL.rstrip("/")


def _internal_headers(user_id, include_content_type=True, request_id=None):
    headers = {
        "X-Internal-Service-Token": settings.INTERNAL_SERVICE_TOKEN,
        "X-User-Id": str(user_id),
    }
    if request_id:
        headers["X-Request-Id"] = str(request_id)
    if include_content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _read_json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("유효한 JSON 요청이 필요합니다.")


def _proxy_error_response(detail, status):
    return JsonResponse({"detail": detail}, status=status)


def _stream_error_event(message):
    payload = {"type": "error", "message": message}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _map_upstream_exception(exc):
    if isinstance(exc, httpx.TimeoutException):
        return "채팅 서버 응답이 지연되고 있습니다. 잠시 후 다시 시도해 주세요.", 504
    return "채팅 서버와 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.", 502


def _stream_timeout():
    return httpx.Timeout(
        connect=settings.FASTAPI_STREAM_CONNECT_TIMEOUT,
        read=settings.FASTAPI_STREAM_READ_TIMEOUT,
        write=settings.FASTAPI_STREAM_WRITE_TIMEOUT,
        pool=settings.FASTAPI_STREAM_POOL_TIMEOUT,
    )


def _serialize_session(session):
    updated_at = timezone.localtime(session.updated_at)
    return {
        "session_id": str(session.session_id),
        "title": session.title,
        "target_pet_id": str(session.target_pet_id) if session.target_pet_id else None,
        "profile_context_type": _normalize_profile_context_type(session.profile_context_type),
        "display_date": updated_at.strftime("%y/%m/%d"),
        "created_at": timezone.localtime(session.created_at).isoformat(),
        "updated_at": updated_at.isoformat(),
    }


def _serialize_message(message):
    recommended_products = []
    if hasattr(message, "recommended_products"):
        recommended_products = [
            {
                "goods_id": recommendation.product.goods_id,
                "product_name": recommendation.product.goods_name,
                "brand_name": recommendation.product.brand_name,
                "price": recommendation.product.price,
                "discount_price": recommendation.product.discount_price,
                "rating": float(recommendation.product.rating) if recommendation.product.rating is not None else None,
                "reviews": recommendation.product.review_count,
                "thumbnail_url": recommendation.product.thumbnail_url,
                "product_url": recommendation.product.product_url,
                "rank_order": recommendation.rank_order,
            }
            for recommendation in message.recommended_products.all()
        ]
    return {
        "message_id": str(message.message_id),
        "role": message.role,
        "content": message.content,
        "created_at": timezone.localtime(message.created_at).isoformat(),
        "recommended_products": recommended_products,
    }


def _serialize_session_groups(sessions):
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    groups = []
    grouped = {}

    for session in sessions:
        session_date = timezone.localtime(session.updated_at).date()
        if session_date == today:
            key, label = "today", "오늘"
        elif session_date == yesterday:
            key, label = "yesterday", "어제"
        else:
            key, label = session_date.isoformat(), timezone.localtime(session.updated_at).strftime("%y/%m/%d")

        if key not in grouped:
            grouped[key] = {"key": key, "label": label, "sessions": []}
            groups.append(grouped[key])
        grouped[key]["sessions"].append(_serialize_session(session))

    return groups


def _get_owned_session(user, session_id):
    try:
        return (
            ChatSession.objects.select_related("target_pet")
            .filter(session_id=session_id, user=user)
            .first()
        )
    except (ValidationError, ValueError, TypeError):
        return None


def _get_owned_target_pet(user, pet_id):
    if not pet_id:
        return None

    try:
        return Pet.objects.filter(pet_id=pet_id, user=user).first()
    except (ValidationError, ValueError, TypeError):
        return None


def _touch_session(session):
    session.updated_at = timezone.now()
    session.save(update_fields=["updated_at"])


def _capture_sse_event(event_lines, capture):
    payload = None
    for line in event_lines:
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue

    if not payload:
        return None

    event_type = payload.get("type")
    if event_type == "token":
        capture["assistant_text"] += payload.get("content", "")
    elif event_type == "products":
        capture["product_cards"] = payload.get("cards") or []
    elif event_type == "error":
        capture["error_message"] = payload.get("message") or "죄송합니다, 오류가 발생했습니다."
    elif event_type == "done":
        capture["completed"] = True
    return event_type


def _persist_recommended_products(message, product_cards):
    if not product_cards:
        return

    goods_ids = [card.get("goods_id") for card in product_cards if card.get("goods_id")]
    if not goods_ids:
        return

    product_map = Product.objects.in_bulk(goods_ids, field_name="goods_id")
    recommendations = []
    for index, card in enumerate(product_cards):
        product = product_map.get(card.get("goods_id"))
        if product is None:
            continue
        recommendations.append(
            ChatMessageRecommendation(
                message=message,
                product=product,
                rank_order=index,
            )
        )

    if recommendations:
        ChatMessageRecommendation.objects.bulk_create(recommendations, ignore_conflicts=True)


def _stream_fastapi_response(url, payload, user_id, capture=None, request_id=None):
    headers = _internal_headers(user_id, request_id=request_id)
    started_at = time.perf_counter()
    first_event_logged = False
    _trace_log(
        "stream_start",
        request_id=request_id,
        url=url,
        thread_id=payload.get("thread_id"),
        target_pet_id=payload.get("target_pet_id"),
        message_chars=len(payload.get("message") or ""),
    )

    try:
        with httpx.Client(timeout=_stream_timeout()) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                _trace_log(
                    "upstream_connected",
                    request_id=request_id,
                    status_code=response.status_code,
                    elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                )
                if response.status_code != 200:
                    detail = "채팅 요청 처리에 실패했습니다."
                    try:
                        detail = response.json().get("detail", detail)
                    except Exception:
                        text = response.text.strip()
                        if text:
                            detail = text
                    if capture is not None:
                        capture["error_message"] = detail
                    _trace_log("upstream_non_200", request_id=request_id, detail=detail)
                    yield _stream_error_event(detail)
                    return

                try:
                    event_lines = []
                    for line in response.iter_lines():
                        if line == "":
                            if event_lines:
                                event_type = None
                                if capture is not None:
                                    event_type = _capture_sse_event(event_lines, capture)
                                if event_type is None:
                                    event_type = _capture_sse_event(event_lines, {
                                        "assistant_text": "",
                                        "product_cards": [],
                                        "error_message": None,
                                        "completed": False,
                                    })
                                if event_type and not first_event_logged:
                                    _trace_log(
                                        "first_sse_event",
                                        request_id=request_id,
                                        event_type=event_type,
                                        elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                                    )
                                    first_event_logged = True
                                if event_type in {"error", "done", "products"}:
                                    _trace_log(
                                        "sse_event",
                                        request_id=request_id,
                                        event_type=event_type,
                                        elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                                    )
                                yield "\n".join(event_lines) + "\n\n"
                                event_lines = []
                            continue
                        event_lines.append(line)

                    if event_lines:
                        event_type = None
                        if capture is not None:
                            event_type = _capture_sse_event(event_lines, capture)
                        if event_type is None:
                            event_type = _capture_sse_event(event_lines, {
                                "assistant_text": "",
                                "product_cards": [],
                                "error_message": None,
                                "completed": False,
                            })
                        if event_type and not first_event_logged:
                            _trace_log(
                                "first_sse_event",
                                request_id=request_id,
                                event_type=event_type,
                                elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                            )
                            first_event_logged = True
                        if event_type in {"error", "done", "products"}:
                            _trace_log(
                                "sse_event",
                                request_id=request_id,
                                event_type=event_type,
                                elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                            )
                        yield "\n".join(event_lines) + "\n\n"

                    if not (capture and (capture["completed"] or capture["error_message"])):
                        detail = "응답 생성이 지연되고 있습니다. 잠시 후 다시 시도해 주세요."
                        if capture is not None:
                            capture["error_message"] = detail
                        _trace_log(
                            "stream_missing_terminal_event",
                            request_id=request_id,
                            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                        )
                        yield _stream_error_event(detail)
                except httpx.HTTPError as exc:
                    detail, _ = _map_upstream_exception(exc)
                    if capture is not None:
                        capture["error_message"] = detail
                    _trace_log(
                        "stream_iter_error",
                        request_id=request_id,
                        error_type=type(exc).__name__,
                        detail=detail,
                        elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
                    )
                    yield _stream_error_event(detail)
    except httpx.HTTPError as exc:
        detail, _ = _map_upstream_exception(exc)
        if capture is not None:
            capture["error_message"] = detail
        _trace_log(
            "stream_connect_error",
            request_id=request_id,
            error_type=type(exc).__name__,
            detail=detail,
            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
        )
        yield _stream_error_event(detail)


def _build_chat_payload(payload, user_id, thread_id=None, target_pet_id=None):
    safe_payload = {
        "message": (payload.get("message") or "").strip(),
        "pet_profile": payload.get("pet_profile"),
        "health_concerns": payload.get("health_concerns") or [],
        "allergies": payload.get("allergies") or [],
        "food_preferences": payload.get("food_preferences") or [],
        "user_id": str(user_id),
    }
    if thread_id:
        safe_payload["thread_id"] = str(thread_id)
    elif payload.get("thread_id"):
        safe_payload["thread_id"] = payload.get("thread_id")
    else:
        safe_payload["thread_id"] = "default"
    resolved_target_pet_id = target_pet_id or payload.get("target_pet_id")
    if resolved_target_pet_id:
        safe_payload["target_pet_id"] = str(resolved_target_pet_id)
    return safe_payload


def _persist_streamed_response(session, url, payload, user_id, request_id=None):
    capture = {
        "assistant_text": "",
        "error_message": None,
        "completed": False,
        "product_cards": [],
    }

    try:
        for chunk in _stream_fastapi_response(url, payload, user_id, capture=capture, request_id=request_id):
            yield chunk
    finally:
        content = ""
        if capture["error_message"]:
            content = capture["error_message"].strip()
        elif capture["completed"]:
            content = capture["assistant_text"].strip()

        if content:
            assistant_message = ChatMessage.objects.create(session=session, role="assistant", content=content)
            _persist_recommended_products(assistant_message, capture["product_cards"])
            _touch_session(session)
            _trace_log(
                "persist_streamed_response",
                request_id=request_id,
                session_id=str(session.session_id),
                completed=capture["completed"],
                error=bool(capture["error_message"]),
                product_cards=len(capture["product_cards"]),
                content_chars=len(content),
            )


def _require_authenticated(request):
    if request.user.is_authenticated:
        return None
    return JsonResponse({"detail": "로그인이 필요합니다."}, status=401)


@require_http_methods(["POST"])
def chat_proxy_view(request):
    unauthorized = _require_authenticated(request)
    if unauthorized:
        return unauthorized

    try:
        payload = _read_json_body(request)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    message = (payload.get("message") or "").strip()
    if not message:
        return JsonResponse({"detail": "message is required."}, status=400)

    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    safe_payload = _build_chat_payload(payload, request.user.id, target_pet_id=payload.get("target_pet_id"))
    _trace_log(
        "chat_proxy_request",
        request_id=request_id,
        user_id=request.user.id,
        thread_id=safe_payload.get("thread_id"),
        target_pet_id=safe_payload.get("target_pet_id"),
        message_chars=len(message),
    )

    return StreamingHttpResponse(
        _stream_fastapi_response(_chat_base_url() + "/", safe_payload, request.user.id, request_id=request_id),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-Id": request_id,
        },
    )


@require_http_methods(["GET", "POST"])
def sessions_proxy_view(request):
    unauthorized = _require_authenticated(request)
    if unauthorized:
        return unauthorized

    if request.method == "GET":
        sessions = list(
            request.user.chat_sessions.select_related("target_pet").order_by("-updated_at", "-created_at")
        )
        return JsonResponse(
            {
                "sessions": [_serialize_session(session) for session in sessions],
                "groups": _serialize_session_groups(sessions),
            }
        )

    try:
        payload = _read_json_body(request)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    title = (payload.get("title") or "").strip() or "새 대화"
    target_pet_id = payload.get("target_pet_id")
    profile_context_type = _normalize_profile_context_type(payload.get("profile_context_type"))
    target_pet = None
    if profile_context_type == ChatSession.PROFILE_CONTEXT_PET and target_pet_id:
        target_pet = _get_owned_target_pet(request.user, target_pet_id)
        if target_pet is None:
            return JsonResponse({"detail": "선택한 반려동물을 찾을 수 없습니다."}, status=404)

    session = ChatSession.objects.create(
        user=request.user,
        target_pet=target_pet,
        profile_context_type=profile_context_type,
        title=title,
    )
    return JsonResponse(_serialize_session(session), status=201)


@require_http_methods(["PATCH", "DELETE"])
def session_detail_proxy_view(request, session_id):
    unauthorized = _require_authenticated(request)
    if unauthorized:
        return unauthorized

    session = _get_owned_session(request.user, session_id)
    if session is None:
        return JsonResponse({"detail": "대화를 찾을 수 없습니다."}, status=404)

    if request.method == "DELETE":
        session.delete()
        return JsonResponse({"deleted": True})

    try:
        payload = _read_json_body(request)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    next_title = (payload.get("title") or "").strip() or session.title or "새 대화"
    next_profile_context_type = _normalize_profile_context_type(payload.get("profile_context_type") or session.profile_context_type)
    next_target_pet = session.target_pet

    if next_profile_context_type == ChatSession.PROFILE_CONTEXT_PET:
        requested_target_pet_id = payload.get("target_pet_id") or session.target_pet_id
        if requested_target_pet_id:
            next_target_pet = _get_owned_target_pet(request.user, requested_target_pet_id)
            if next_target_pet is None:
                return JsonResponse({"detail": "선택한 반려동물을 찾을 수 없습니다."}, status=404)
        else:
            next_target_pet = None
    else:
        next_target_pet = None

    updated_fields = []
    if session.title != next_title:
        session.title = next_title
        updated_fields.append("title")
    if session.profile_context_type != next_profile_context_type:
        session.profile_context_type = next_profile_context_type
        updated_fields.append("profile_context_type")
    if session.target_pet_id != (next_target_pet.pet_id if next_target_pet else None):
        session.target_pet = next_target_pet
        updated_fields.append("target_pet")

    if updated_fields:
        session.save(update_fields=updated_fields + ["updated_at"])
    else:
        _touch_session(session)
    return JsonResponse(_serialize_session(session))


@require_http_methods(["GET", "POST"])
def session_messages_proxy_view(request, session_id):
    unauthorized = _require_authenticated(request)
    if unauthorized:
        return unauthorized

    session = _get_owned_session(request.user, session_id)
    if session is None:
        return JsonResponse({"detail": "대화를 찾을 수 없습니다."}, status=404)

    if request.method == "GET":
        messages = [
            _serialize_message(message)
            for message in session.messages.prefetch_related("recommended_products__product").order_by("created_at")
        ]
        return JsonResponse(
            {
                "session_id": str(session.session_id),
                "messages": messages,
                "history_trimmed": False,
            }
        )

    try:
        payload = _read_json_body(request)
    except ValueError as exc:
        return JsonResponse({"detail": str(exc)}, status=400)

    message = (payload.get("message") or "").strip()
    if not message:
        return JsonResponse({"detail": "message is required."}, status=400)

    ChatMessage.objects.create(session=session, role="user", content=message)
    _touch_session(session)

    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    safe_payload = _build_chat_payload(
        payload,
        request.user.id,
        thread_id=session.session_id,
        target_pet_id=session.target_pet_id,
    )
    _trace_log(
        "session_message_proxy_request",
        request_id=request_id,
        user_id=request.user.id,
        session_id=str(session.session_id),
        target_pet_id=str(session.target_pet_id) if session.target_pet_id else None,
        message_chars=len(message),
    )
    return StreamingHttpResponse(
        _persist_streamed_response(
            session,
            _chat_base_url() + "/",
            safe_payload,
            request.user.id,
            request_id=request_id,
        ),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-Id": request_id,
        },
    )
