"""The streamed reply: the preview shows only checked text, and the final reply is the same text."""

import json
import re

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.engine.engine import Engine
from app.engine.streaming import Draft, MessageStream
from app.engine.validator import Allowed
from app.engine.wording import compose, fixed
from app.llm.gateway import LLMGateway
from tests.test_engine import (  # noqa: F401  kb: the test knowledge base (fixture)
    CAM,
    FakeWeb,
    Script,
    kb,
    session,
)


class Listener:
    def __init__(self):
        self.events = []

    def __call__(self, name, data):
        self.events.append((name, data))

    def text(self):
        """What the mechanic sees at the end: the deltas since the last reset."""
        shown = ""
        for name, data in self.events:
            shown = "" if name == "reset" else shown + data["text"] if name == "delta" else shown
        return shown

    def names(self):
        return [name for name, _ in self.events]


def fed(stream: MessageStream, answer: dict, size: int, attempt: int = 0, **checks) -> None:
    raw = json.dumps(answer, ensure_ascii=True)  # \\uXXXX escapes, split anywhere: the hardest input
    stream.begin(attempt, allowed=checks.pop("allowed", Allowed.from_texts([])), **checks)
    for i in range(0, len(raw), size):
        stream.feed(raw[i:i + size])


# -- the preview of one answer ------------------------------------------------------------------------------
@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 64])
def test_the_message_is_read_from_the_json_as_it_arrives_in_any_piece_size(size):
    listener = Listener()
    message = 'Check the "CMP" connector first.\n- Then the ground 🔧 under the dash.\nافحص الفيوز. What do you find?'
    fed(MessageStream(Draft(listener), lead=["Hello.", "P0341: label:"]), {"message": message, "use": ["web:1"]}, size)
    assert listener.text() == "Hello.\nP0341: label:\n" + message
    assert listener.names()[0] == "status" and listener.events[0][1] == {"stage": "writing"}


def test_a_sentence_with_a_number_without_a_source_stops_the_preview_of_that_attempt():
    listener = Listener()
    stream = MessageStream(Draft(listener))
    fed(stream, {"message": "Check the connector. It should read 7.5 ohm. Then check the ground."}, 4)
    assert listener.text() == "Check the connector."  # the model will be asked again: nothing after the failing sentence


def test_on_the_last_attempt_of_a_salvaged_answer_the_failing_sentence_is_left_out():
    listener = Listener()
    stream = MessageStream(Draft(listener))
    fed(stream, {"message": "Check the connector. It should read 7.5 ohm. Then check the ground."}, 4, attempt=0)
    fed(stream, {"message": "Check the connector. It should read 7.5 ohm. Then check the ground."}, 4, attempt=1, salvage=True)
    assert "reset" in listener.names()  # the first attempt's text was withdrawn
    assert listener.text() == "Check the connector. Then check the ground."


def test_ids_greetings_titles_and_a_copied_label_are_never_shown():
    listener = Listener()
    header = fixed("general_header", "en", codes="P0341")
    stream = MessageStream(Draft(listener), header=header, codes="P0341")
    fed(stream, {"message": f"{header} Check the CMP sensor (web:AB12CD34EF56). Is it dry?"}, 3, ids=["web:AB12CD34EF56"])
    assert listener.text() == "Check the CMP sensor. Is it dry?"
    polite = Listener()
    fed(MessageStream(Draft(polite)), {"message": "Hello, my friend, check the CMP sensor. Is it dry?"}, 3)
    assert polite.text() == "Check the CMP sensor. Is it dry?"


def test_a_second_question_stops_the_first_attempt_and_forbidden_words_are_never_shown():
    listener = Listener()
    fed(MessageStream(Draft(listener)), {"message": "Is it wet? Is it hot? Check it."}, 5, single_question=True)
    assert listener.text() == "Is it wet?"
    solvents = Listener()
    fed(MessageStream(Draft(solvents)), {"message": "Unplug it. Clean it with brake cleaner."}, 5,
        forbidden=re.compile("brake cleaner"))
    assert solvents.text() == "Unplug it."


def test_a_broken_piece_never_breaks_the_reply():
    listener = Listener()
    stream = MessageStream(Draft(listener))
    stream.begin(0, allowed=Allowed.from_texts([]))
    stream.feed('{"message": "Check it.\\uZZZZ more. ')  # not JSON: the preview stops, quietly
    stream.feed('And this."}')
    assert listener.text() == ""


def test_compose_streams_the_same_text_it_returns():
    listener = Listener()
    model = Script(general=[{"message": "Start with the battery. Then the fuses. What do you find?"}]).model
    out = compose(model, "general", system="s", facts={}, language="en", allowed=Allowed.from_texts([]),
                  stream=MessageStream(Draft(listener)), single_question=True)
    assert out["message"] == listener.text() == "Start with the battery. Then the fuses. What do you find?"


# -- the provider's stream ------------------------------------------------------------------------------------
def test_the_gateway_reads_a_streamed_answer_with_its_usage_and_cost():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        pieces = ['{"message": "Che', 'ck it."}']
        chunks = [{"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                   "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]} for p in pieces]
        chunks[-1]["choices"][0]["finish_reason"] = "stop"
        chunks.append({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"], "choices": [],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "estimated_cost": 0.0001}})
        payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})

    settings = Settings(_env_file=None, deepinfra_api_key=SecretStr("test-key"))
    gateway = LLMGateway(settings=settings, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    pieces = []
    result = gateway.chat([{"role": "user", "content": "x"}], json_schema={"type": "object"}, on_text=pieces.append)
    assert pieces == ['{"message": "Che', 'ck it."}'] and result.text == '{"message": "Check it."}'
    assert (result.prompt_tokens, result.completion_tokens, result.cost_usd, result.finish_reason) == (10, 4, 0.0001, "stop")
    assert seen[0]["stream"] is True and seen[0]["stream_options"] == {"include_usage": True}
    assert seen[0]["response_format"]["type"] == "json_schema"


# -- a whole turn (database) ----------------------------------------------------------------------------------
def test_a_streamed_turn_shows_what_it_does_and_its_preview_is_the_final_reply(kb):  # noqa: F811
    listener = Listener()
    script = Script(general=[{"message": "Start with the battery and the fuses. What do you find?"}])
    reply = Engine(kb, script.model, events=listener).turn(session(), "2025 Integra 1.5 turbo, code P0341.").reply
    assert reply.tier == "general"
    assert [d["stage"] for n, d in listener.events if n == "status"] == ["searching", "writing"]
    assert listener.text() == reply.message  # greeting, source label and the answer: nothing moves at the end
    assert "No verified data" not in reply.message and "no verified data" not in reply.message


def test_a_withdrawn_source_answer_is_reset_before_the_general_answer(kb):  # noqa: F811
    listener = Listener()
    script = Script(web=[{"use": [], "message": "Per a forum, check the connector. What do you see?"}],
                    general=[{"message": "Start with the CMP sensor connector. What do you find?"}])
    reply = Engine(kb, script.model, external=FakeWeb([CAM]), events=listener).turn(session(), "2025 Integra, code P0341.").reply
    stages = [d["stage"] for n, d in listener.events if n == "status"]
    assert stages[:2] == ["searching", "searching_web"] and "reset" in listener.names()
    assert reply.tier == "general" and listener.text() == reply.message


def test_without_a_listener_nothing_changes(kb):  # noqa: F811
    script = Script(general=[{"message": "Start with the battery and the fuses. What do you find?"}])
    plain = Engine(kb, script.model).turn(session(), "2025 Integra 1.5 turbo, code P0341.").reply
    assert plain.message.startswith("Hello, I'm glad to help.\nP0341: general diagnostic guidance for this fault:\nStart with")
