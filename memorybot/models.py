"""Structured Gemini outputs (used as response schemas)."""
from typing import Literal

from pydantic import BaseModel


class AttachmentPick(BaseModel):
    ref: str
    name: str


class SavePlan(BaseModel):
    path: str
    path_from_user: bool
    content: str
    attachments: list[AttachmentPick]


class FileChange(BaseModel):
    path: str
    action: Literal["update", "delete"]
    new_content: str


class EditPlan(BaseModel):
    summary: str
    changes: list[FileChange]
    delete_attachments: list[str]
