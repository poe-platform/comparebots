"""

Bot that returns interleaved results from multiple bots.

"""
from __future__ import annotations

import asyncio
import json
import re
import traceback
from collections import defaultdict
from typing import AsyncIterable, AsyncIterator, Sequence

from fastapi_poe import PoeBot, run
from fastapi_poe.client import BotError, BotMessage, MetaMessage, stream_request
from fastapi_poe.types import (
    ProtocolMessage,
    QueryRequest,
    SettingsRequest,
    SettingsResponse,
)
from sse_starlette.sse import ServerSentEvent

COMPARE_REGEX = r"\s([A-Za-z_\-\d]+)\s+vs\.?\s+([A-Za-z_\-\d]+)\s*$"


async def advance_stream(
    label: str, gen: AsyncIterator[BotMessage]
) -> tuple[str, BotMessage | Exception | None]:
    try:
        return label, await gen.__anext__()
    except StopAsyncIteration:
        return label, None
    except Exception as e:
        return label, e


async def combine_streams(
    streams: Sequence[tuple[str, AsyncIterator[BotMessage]]]
) -> AsyncIterator[tuple[str, BotMessage | Exception]]:
    active_streams = dict(streams)
    while active_streams:
        for coro in asyncio.as_completed(
            [advance_stream(label, gen) for label, gen in active_streams.items()]
        ):
            label, msg = await coro
            if msg is None:
                del active_streams[label]
            else:
                if isinstance(msg, Exception):
                    del active_streams[label]
                yield label, msg


def get_bots_to_compare(messages: Sequence[ProtocolMessage]) -> tuple[str, str]:
    for message in reversed(messages):
        if message.role != "user":
            continue
        match = re.search(COMPARE_REGEX, message.content)
        if match is not None:
            return match.groups()
    return ("assistant", "claude-instant")


def preprocess_message(message: ProtocolMessage, bot: str) -> ProtocolMessage:
    """Preprocess the conversation history.

    For user messages, remove "x vs. y" from the end of the message.

    For bot messages, try to keep only the parts of the message that come from
    the bot we're querying.
    """
    if message.role == "user":
        new_content = re.sub(COMPARE_REGEX, "", message.content)
        return message.model_copy(update={"content": new_content})
    elif message.role == "bot":
        parts = re.split(r"\*\*([A-Za-z_\-\d]+)\*\* says:\n", message.content)
        for message_bot, text in zip(parts[1::2], parts[2::2]):
            if message_bot.casefold() == bot.casefold():
                return message.model_copy(update={"content": text})
        # If we can't find a message by this bot, just return the original message
        return message
    else:
        return message


def preprocess_query(query: QueryRequest, bot: str) -> QueryRequest:
    new_query = query.model_copy(
        update={"query": [preprocess_message(message, bot) for message in query.query]}
    )
    return new_query


def exception_to_message(label: str, e: Exception) -> str:
    if (
        isinstance(e, BotError)
        and isinstance(e.__cause__, BotError)
        and isinstance(e.__cause__.args[0], str)
    ):
        try:
            args = json.loads(e.__cause__.args[0])
        except json.JSONDecodeError:
            pass
        else:
            return f"**Error from {label}**: {args.get('text')}"
    tb = "".join(traceback.format_exception(e))
    return f"**Error from {label}**:\n```{tb}```"


class CompareBot(PoeBot):
    async def get_response(self, query: QueryRequest) -> AsyncIterable[ServerSentEvent]:
        bots = get_bots_to_compare(query.query)
        streams = [
            (bot, stream_request(preprocess_query(query, bot), bot, query.access_key))
            for bot in bots
        ]
        label_to_responses: dict[str, list[str]] = defaultdict(list)
        async for label, msg in combine_streams(streams):
            if isinstance(msg, MetaMessage):
                continue
            elif isinstance(msg, Exception):
                label_to_responses[label] = [exception_to_message(label, msg)]
            elif msg.is_suggested_reply:
                yield self.suggested_reply_event(msg.text)
                continue
            elif msg.is_replace_response:
                label_to_responses[label] = [msg.text]
            else:
                label_to_responses[label].append(msg.text)
            text = "\n\n".join(
                f"**{label.title()}** says:\n{''.join(chunks)}"
                for label, chunks in label_to_responses.items()
            )
            yield self.replace_response_event(text)

    async def get_settings(self, setting: SettingsRequest) -> SettingsResponse:
        return SettingsResponse(
            server_bot_dependencies={"any": 2},
            allow_attachments=True,
            introduction_message=(
                "Hi! I am a bot that allows you to compare responses from two other bots. Please "
                'provide me your query followed by a string that looks like "bot1 vs bot2" '
                "in order to see and compare responses from the two bots."
            ),
        )


if __name__ == "__main__":
    run(CompareBot())
