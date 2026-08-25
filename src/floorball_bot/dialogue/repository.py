from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from floorball_bot.dialogue.models import DialogueMode, DialogueSpec, GapReport


class IndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str = Field(pattern=r"^[a-z0-9_.-]+\.json$")


class SpecIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    modes: dict[DialogueMode, IndexEntry]


@dataclass(frozen=True)
class LoadedDialogueSpec:
    spec: DialogueSpec
    sha256: str
    canonical_json: str


@dataclass(frozen=True)
class PromptBundle:
    system_instruction: str
    spec_sha256: str
    spec_version: str


class DialogueSpecRepository:
    def __init__(self, root: Path | None = None) -> None:
        package_root = Path(__file__).resolve().parent
        self.root = (root or package_root / "specs").resolve()
        self.prompt_path = package_root / "prompts" / "common-system.md"
        self.index = SpecIndex.model_validate_json(
            (self.root / "index.json").read_text(encoding="utf-8")
        )

    def load(self, mode: DialogueMode | str) -> LoadedDialogueSpec:
        parsed_mode = DialogueMode(mode)
        entry = self.index.modes[parsed_mode]
        path = (self.root / entry.file).resolve()
        if path.parent != self.root:
            raise ValueError("dialogue spec path escapes spec root")
        spec = DialogueSpec.model_validate_json(path.read_text(encoding="utf-8"))
        if spec.mode != parsed_mode:
            raise ValueError(f"spec mode mismatch: expected {parsed_mode}, got {spec.mode}")
        canonical = json.dumps(
            spec.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        return LoadedDialogueSpec(spec=spec, sha256=digest, canonical_json=canonical)

    def load_all(self) -> tuple[LoadedDialogueSpec, ...]:
        return tuple(self.load(mode) for mode in DialogueMode)

    def build_system_prompt(
        self,
        loaded: LoadedDialogueSpec,
        gaps: GapReport,
        *,
        context_json: str = "{}",
        context_sha256: str = "",
    ) -> PromptBundle:
        common = self.prompt_path.read_text(encoding="utf-8").strip()
        instruction = (
            f"{common}\n\n"
            f"SPEC_SHA256: {loaded.sha256}\n"
            f"SPEC_VERSION: {loaded.spec.version}\n"
            f"CONTEXT_SHA256: {context_sha256 or 'none'}\n"
            f"DIALOGUE_SPEC_JSON:\n{loaded.canonical_json}\n\n"
            "DETERMINISTIC_GAP_REPORT_JSON:\n"
            f"{gaps.model_dump_json()}\n\n"
            f"SAFE_CONTEXT_JSON:\n{context_json}"
        )
        return PromptBundle(
            system_instruction=instruction,
            spec_sha256=loaded.sha256,
            spec_version=loaded.spec.version,
        )
