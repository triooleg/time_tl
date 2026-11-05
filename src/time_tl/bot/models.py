from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class NotificationMode(StrEnum):
    LESSONS = "lessons"
    LESSONS_AND_SIRENS = "lessons_and_sirens"
    SIRENS = "sirens"

    @property
    def label(self) -> str:
        return {
            self.LESSONS: "Лише уроки",
            self.LESSONS_AND_SIRENS: "Уроки + тривоги",
            self.SIRENS: "Лише тривоги",
        }[self]


@dataclass(frozen=True)
class Button:
    label: str
    callback_data: str | None = None
    request_location: bool = False


@dataclass(frozen=True)
class View:
    text: str
    rows: tuple[tuple[Button, ...], ...] = ()
    parse_mode: Literal["HTML"] | None = None
