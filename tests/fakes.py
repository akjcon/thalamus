"""A stand-in for the Anthropic client so model-backed steps can be tested offline."""
from types import SimpleNamespace


class FakeClient:
    """Answers each parse() call with the next queued reply (a pydantic object, or an Exception to raise)."""

    def __init__(self, *replies, stop_reason="end_turn", usage=(1000, 200, 0)):
        self.replies, self.calls, self.stop_reason, self.usage = list(replies), [], stop_reason, usage
        self.messages = SimpleNamespace(stream=lambda **kw: _Stream(lambda: self._parse("messages", kw)))
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: _Stream(lambda: self._parse("beta", kw))))

    def _parse(self, door, kw):
        self.calls.append({"door": door, **kw})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(kw)
        i, o, c = self.usage
        return SimpleNamespace(parsed_output=reply, stop_reason=self.stop_reason, model=kw["model"],
                               usage=SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=c,
                                                     cache_creation_input_tokens=0))


class _Stream:
    """Mimics the SDK's stream manager: the request happens on enter, the answer comes from get_final_message()."""

    def __init__(self, call):
        self._call = call

    def __enter__(self):
        self._message = self._call()
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message
