"""What the mechanic sees while a reply is being made (the streaming endpoint, app/api/ai.py).

The reply is still made and checked as a whole by the engine, and the final reply replaces everything shown
before it. While it is being made, the turn reports:

  status  what it is doing now: "reading", "searching", "searching_web", "writing"
  delta   the next piece of the answer's text, already checked
  reset   the text shown so far is withdrawn: the model is asked again, or another kind of answer is made

Only text that passes the same checks as the whole message is shown. A sentence is shown when it is complete,
after the same clean-up as the whole message (source ids, word counts, a greeting or a title for the mechanic),
and only when the number checker finds no number, pin or value without a source and no forbidden word in it.
When a sentence fails, nothing more of that attempt is shown, because the model will be asked again; on the last
attempt of an answer that may be salvaged, the failing sentence is left out and the rest is shown, as `salvaged`
does with the whole message.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable

from app.engine.validator import Allowed, problems
from app.engine.wording import tidy

log = logging.getLogger(__name__)

Events = Callable[[str, dict], None]  # (event name, data); see the module docstring

_KEY = re.compile(r'"message"\s*:\s*"')
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_SEPARATOR = re.compile(r"((?<=[.!?؟۔])[ \t]+|\n+)")  # after a sentence's end, or a line break
_QUESTION_MARK = re.compile(r"[?؟]")


class Draft:
    """The text shown for the current turn, and its events. Without a listener it does nothing."""

    def __init__(self, events: Events | None = None) -> None:
        self.events = events
        self.shown = False

    def status(self, stage: str) -> None:
        if self.events is not None:
            self.events("status", {"stage": stage})

    def add(self, text: str) -> None:
        if self.events is not None and text:
            self.events("delta", {"text": text})
            self.shown = True

    def reset(self) -> None:
        if self.events is not None and self.shown:
            self.events("reset", {})
        self.shown = False


class MessageStream:
    """Reads the model's JSON as it arrives and shows the "message" value sentence by sentence, checked.

    `lead`: the lines the final reply will have above the model's text (greeting or caution, safety texts, the
    source label), shown first so the text does not move when the final reply replaces the preview.
    `header` and `codes`: the source label the model must not copy (as `engine.strip_header` removes it).
    The checks themselves (allowed numbers, forbidden words, ids) come from `compose`, through `begin`."""

    def __init__(self, draft: Draft, *, lead: Iterable[str] = (), header: str = "", codes: str = "") -> None:
        self.draft = draft
        self.lead = [line for line in lead if line]
        self.codes = codes
        self.header_pieces = [p.strip() for p in re.split(r"(?<=[.:۔])\s+", header) if len(p.strip()) > 10]
        self._held = True  # nothing is shown before `compose` starts an attempt with its checks

    def begin(self, attempt: int, *, allowed: Allowed, forbidden: re.Pattern[str] | None = None, ids: Iterable[str] = (),
              language: str = "en", salvage: bool = False, single_question: bool = False) -> None:
        """A new attempt of the model, with the checks of `compose`: what was shown of an earlier one is withdrawn."""
        self.draft.reset()
        self.draft.status("writing")
        self.allowed, self.forbidden, self.ids, self.language = allowed, forbidden, list(ids), language
        self.last_chance = attempt >= 1  # no further attempt: a failing sentence is left out (salvage) or ends it
        self.salvage, self.single_question = salvage, single_question
        self._raw, self._pos, self._mode, self._text = "", 0, "key", ""
        self._held = False
        self._started = False
        self._separator = ""
        self._questions = 0

    # -- the model's text -------------------------------------------------------------------------------------
    def feed(self, piece: str) -> None:
        if self._held or self._mode == "done":
            return
        try:
            self._raw += piece
            self._decode()
            self._show_ready(final=self._mode == "done")
        except Exception:  # noqa: BLE001  the preview must never break the reply: it stops, the final reply comes
            log.exception("the streamed preview stopped")
            self._held = True

    def _decode(self) -> None:
        """The characters of the "message" string received so far, JSON escapes decoded; a split escape waits."""
        if self._mode == "key":
            found = _KEY.search(self._raw)
            if not found:
                return
            self._mode, self._pos = "text", found.end()
        raw, i, out = self._raw, self._pos, []
        while i < len(raw):
            char = raw[i]
            if char == '"':
                self._mode = "done"
                i += 1
                break
            if char != "\\":
                out.append(char)
                i += 1
                continue
            if i + 1 >= len(raw):
                break
            kind = raw[i + 1]
            if kind != "u":
                out.append(_ESCAPES.get(kind, kind))
                i += 2
                continue
            if i + 6 > len(raw):
                break
            code = int(raw[i + 2:i + 6], 16)
            if 0xD800 <= code < 0xDC00:  # the first half of a pair: "🔧"
                if i + 12 > len(raw):
                    break
                low = int(raw[i + 8:i + 12], 16) if raw[i + 6:i + 8] == "\\u" else None
                if low is not None and 0xDC00 <= low < 0xE000:
                    out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                    i += 12
                    continue
            out.append(chr(code))
            i += 6
        self._pos = i
        self._text += "".join(out)

    def _show_ready(self, *, final: bool) -> None:
        if final:
            ready, self._text = self._text, ""
        else:
            ends = [m.end() for m in _SEPARATOR.finditer(self._text)]
            if not ends:
                return
            ready, self._text = self._text[:ends[-1]], self._text[ends[-1]:]
        parts = _SEPARATOR.split(ready)
        for k in range(0, len(parts), 2):
            if self._held:
                return
            self._show(parts[k], parts[k + 1] if k + 1 < len(parts) else "")

    # -- one sentence -----------------------------------------------------------------------------------------
    def _clean(self, sentence: str) -> str:
        """The clean-up `compose` gives the whole message, then the source label `engine.strip_header` removes."""
        text = tidy(sentence, self.ids, self.language, start=not self._started)
        if not self._started and self.codes and text.startswith(f"{self.codes}:"):
            text = text[len(self.codes) + 1:].strip()
        for piece in self.header_pieces:
            text = text.replace(piece, "")
        return text.strip()

    def _show(self, sentence: str, separator: str) -> None:
        text = self._clean(sentence)
        if not text:
            if self._started and "\n" in separator:
                self._separator = "\n" if "\n" not in self._separator else self._separator
            return
        failed = bool(problems(text, self.allowed)) or (self.forbidden is not None and bool(self.forbidden.search(text)))
        asked = len(_QUESTION_MARK.findall(text))
        if self.single_question and not self.last_chance and self._questions + asked > 1:
            failed = True  # a second question: this attempt is asked again
        if failed:
            if self.last_chance and self.salvage:
                if "\n" in separator:
                    self._separator = "\n" if "\n" not in self._separator else self._separator
                return  # left out, as `salvaged` leaves it out
            self._held = True
            return
        self._questions += asked
        if not self._started:
            self._started = True
            self.draft.add("".join(f"{line}\n" for line in self.lead) + text)
        else:
            self.draft.add(self._separator + text)
        self._separator = separator if separator else ""
