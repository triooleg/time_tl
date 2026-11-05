"""No real transport: prove silence and destination confinement before adapters."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    DeleteMessage,
    DeleteWebhook,
    EditMessageReplyMarkup,
    EditMessageText,
    ForwardMessage,
    GetChat,
    GetMe,
    GetUpdates,
    SendChatAction,
    SendDocument,
    SendMessage,
    SendPhoto,
    SetMyCommands,
    SetWebhook,
)
from aiogram.types import CallbackQuery, Chat, Message, ReplyParameters, Update, User

from time_tl.bot.power_canary_guard import PowerCanaryGuard, PowerCanaryRejected

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
ALLOWED = 101


def message(actor=ALLOWED, chat=ALLOWED, chat_type="private"):
    return Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=chat, type=chat_type),
        from_user=User(id=actor, is_bot=False, first_name="Synthetic"),
        text="/start",
    )


def callback(identifier="callback-one", actor=ALLOWED, chat=ALLOWED, chat_type="private"):
    return CallbackQuery(
        id=identifier,
        from_user=User(id=actor, is_bot=False, first_name="Synthetic"),
        message=message(chat=chat, chat_type=chat_type),
        chat_instance="synthetic",
        data="menu:power",
    )


@pytest.mark.parametrize(
    "allowed",
    [
        frozenset(),
        frozenset({101, 102}),
        frozenset({0}),
        frozenset({-1}),
        frozenset({True}),
        frozenset({"101"}),
        {101},
        (101,),
        frozenset({2**63}),
    ],
)
def test_exact_one_positive_integer_allowlist_required(allowed):
    with pytest.raises(ValueError, match="invalid_power_canary_policy"):
        PowerCanaryGuard(allowed)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"callbackcapacity": 0},
        {"callbackcapacity": 257},
        {"callbackcapacity": True},
        {"callbackTTL": 0},
        {"callbackTTL": 301},
        {"callbackTTL": float("nan")},
        {"callbackTTL": float("inf")},
        {"expires_at": datetime(2026, 11, 4)},
    ],
)
def test_policy_limits_are_bounded(kwargs):
    with pytest.raises(ValueError, match="invalid_power_canary_policy"):
        PowerCanaryGuard(frozenset({ALLOWED}), **kwargs)


async def test_approved_private_command_reaches_handler_without_any_auto_reply():
    guard, handler = PowerCanaryGuard(frozenset({ALLOWED})), AsyncMock(return_value="accepted")
    event = Update(update_id=1, message=message())
    assert await guard(handler, event, {"synthetic": True}) == "accepted"
    handler.assert_awaited_once_with(event, {"synthetic": True})
    assert guard.statistics()["updates_admitted"] == 1


@pytest.mark.parametrize(
    "event",
    [
        Update(update_id=1, message=message(actor=102, chat=102)),
        Update(update_id=2, message=message(actor=102)),
        Update(update_id=3, message=message(chat=102)),
        Update(update_id=4, message=message(chat=-100, chat_type="group")),
        Update(update_id=5, edited_message=message()),
        Update(update_id=6, callback_query=callback(actor=102, chat=102)),
        Update(update_id=7, callback_query=callback(chat=-100, chat_type="supergroup")),
        Update(
            update_id=8,
            callback_query=callback().model_copy(
                update={"message": None, "inline_message_id": "synthetic-inline"}
            ),
        ),
        Update(update_id=9, message=message(), callback_query=callback()),
        Update(update_id=10),
        Update(update_id=11, message=message(), future_update={"data": "unknown"}),
        SimpleNamespace(message=message()),
    ],
)
async def test_nonapproved_or_unsupported_updates_are_silently_dropped(event):
    guard, handler = PowerCanaryGuard(frozenset({ALLOWED})), AsyncMock()
    assert await guard(handler, event, {}) is None
    handler.assert_not_awaited()
    assert guard.statistics() == {
        "updates_admitted": 0,
        "updates_dropped": 1,
        "requests_admitted": 0,
        "requests_denied": 0,
    }


async def test_update_middleware_drops_foreign_callback_before_any_handler_or_reply():
    guard, session, handler = PowerCanaryGuard(frozenset({ALLOWED})), AsyncMock(), AsyncMock()
    dispatcher, router = Dispatcher(), Router()
    dispatcher.update.outer_middleware(guard)

    @router.callback_query()
    async def callback_handler(event):
        await handler(event)
    dispatcher.include_router(router)
    bot = Bot("123456:LOCAL_SYNTHETIC_NOT_REAL", session=session)
    await dispatcher.feed_update(
        bot, Update(update_id=1, callback_query=callback(actor=102, chat=102))
    )
    handler.assert_not_awaited()
    session.assert_not_awaited()


async def test_registered_real_aiogram_request_middleware_blocks_before_fake_transport():
    guard = PowerCanaryGuard(frozenset({ALLOWED}))
    session = AiohttpSession()
    transport = AsyncMock(return_value=True)
    session.make_request = transport
    session.middleware(guard.request)
    bot = Bot("123456:LOCAL_SYNTHETIC_NOT_REAL", session=session)
    try:
        assert await bot.send_message(ALLOWED, "synthetic") is True
        with pytest.raises(PowerCanaryRejected):
            await bot.send_message(102, "synthetic")
        with pytest.raises(PowerCanaryRejected):
            await bot.delete_webhook(drop_pending_updates=True)
        assert transport.await_count == 1
        assert type(transport.await_args.args[1]) is SendMessage
    finally:
        await session.close()


@pytest.mark.parametrize(
    "method",
    [
        GetMe(),
        GetUpdates(),
        DeleteWebhook(),
        DeleteWebhook(drop_pending_updates=False),
        SendMessage(chat_id=ALLOWED, text="synthetic"),
        EditMessageText(chat_id=ALLOWED, message_id=1, text="synthetic"),
        EditMessageReplyMarkup(chat_id=ALLOWED, message_id=1),
        SendDocument(chat_id=ALLOWED, document="synthetic-file-id"),
        SendPhoto(chat_id=ALLOWED, photo="synthetic-photo-id"),
        DeleteMessage(chat_id=ALLOWED, message_id=1),
        SendChatAction(chat_id=ALLOWED, action="typing"),
    ],
)
async def test_explicit_safe_requests_pass_to_transport(method):
    guard, transport, bot = (
        PowerCanaryGuard(frozenset({ALLOWED})),
        AsyncMock(return_value=True),
        object(),
    )
    assert await guard.request(transport, bot, method) is True
    transport.assert_awaited_once_with(bot, method)


@pytest.mark.parametrize(
    "method",
    [
        DeleteWebhook(drop_pending_updates=True),
        GetChat(chat_id=ALLOWED),
        SetMyCommands(commands=[]),
        SetWebhook(url="https://synthetic.invalid/webhook"),
        SendMessage(chat_id=102, text="synthetic"),
        SendMessage(chat_id="101", text="synthetic"),
        SendMessage(chat_id="@synthetic", text="synthetic"),
        EditMessageText(inline_message_id="synthetic-inline", text="synthetic"),
        EditMessageText(
            chat_id=ALLOWED, message_id=1, text="synthetic", inline_message_id="synthetic"
        ),
        SendDocument(chat_id=102, document="synthetic-file-id"),
        ForwardMessage(chat_id=ALLOWED, from_chat_id=ALLOWED, message_id=1),
        CopyMessage(chat_id=ALLOWED, from_chat_id=ALLOWED, message_id=1),
        SendMessage(chat_id=ALLOWED, text="synthetic", business_connection_id="synthetic-business"),
        SendMessage(chat_id=ALLOWED, text="synthetic", allow_paid_broadcast=True),
        SendMessage(chat_id=ALLOWED, text="synthetic", receiver_user_id=102),
        SendMessage(chat_id=ALLOWED, text="synthetic", arbitrary_future_field=102),
        SendMessage(
            chat_id=ALLOWED,
            text="synthetic",
            reply_parameters=ReplyParameters(message_id=1, chat_id=102),
        ),
    ],
)
async def test_unlisted_and_redirecting_requests_fail_before_transport(method):
    guard, transport = PowerCanaryGuard(frozenset({ALLOWED})), AsyncMock()
    with pytest.raises(PowerCanaryRejected, match="^power_canary_request_rejected$"):
        await guard.request(transport, object(), method)
    transport.assert_not_awaited()


async def test_callbacks_require_recent_admitted_capability_and_are_single_use():
    clock = [0.0]
    guard = PowerCanaryGuard(frozenset({ALLOWED}), clock=lambda: clock[0])
    transport, handler = AsyncMock(return_value=True), AsyncMock()
    request = AnswerCallbackQuery(callback_query_id="callback-one")
    with pytest.raises(PowerCanaryRejected):
        await guard.request(transport, object(), request)
    await guard(handler, Update(update_id=1, callback_query=callback()), {})
    assert await guard.request(transport, object(), request)
    with pytest.raises(PowerCanaryRejected):
        await guard.request(transport, object(), request)
    # Replayed inbound update cannot recreate a consumed answer capability.
    assert await guard(handler, Update(update_id=1, callback_query=callback()), {}) is None
    assert handler.await_count == 1
    assert transport.await_count == 1


async def test_callback_capabilities_expire_and_evict_at_capacity():
    clock = [0.0]
    guard = PowerCanaryGuard(
        frozenset({ALLOWED}), clock=lambda: clock[0], callbackcapacity=1, callbackTTL=10
    )
    transport = AsyncMock()
    for index, identifier in enumerate(("old", "new")):
        await guard(AsyncMock(), Update(update_id=index, callback_query=callback(identifier)), {})
    with pytest.raises(PowerCanaryRejected):
        await guard.request(transport, object(), AnswerCallbackQuery(callback_query_id="old"))
    clock[0] = 10.0
    with pytest.raises(PowerCanaryRejected):
        await guard.request(transport, object(), AnswerCallbackQuery(callback_query_id="new"))
    transport.assert_not_awaited()


async def test_expiry_blocks_all_inbound_and_outbound_without_renewal():
    clock = [NOW]
    guard = PowerCanaryGuard(
        frozenset({ALLOWED}), expires_at=NOW + timedelta(minutes=1), wall_clock=lambda: clock[0]
    )
    handler, transport = AsyncMock(), AsyncMock()
    await guard(handler, Update(update_id=1, callback_query=callback()), {})
    clock[0] += timedelta(minutes=1)
    with pytest.raises(PowerCanaryRejected):
        await guard(handler, Update(update_id=2, message=message()), {})
    for method in (
        GetMe(),
        GetUpdates(),
        SendMessage(chat_id=ALLOWED, text="synthetic"),
        AnswerCallbackQuery(callback_query_id="callback-one"),
    ):
        with pytest.raises(PowerCanaryRejected):
            await guard.request(transport, object(), method)
    transport.assert_not_awaited()
    assert handler.await_count == 1
    # A newly constructed guard reuses the same absolute deadline, so restart is not renewal.
    restarted = PowerCanaryGuard(
        frozenset({ALLOWED}), expires_at=NOW + timedelta(minutes=1), wall_clock=lambda: clock[0]
    )
    with pytest.raises(PowerCanaryRejected):
        await restarted.request(transport, object(), GetUpdates())
