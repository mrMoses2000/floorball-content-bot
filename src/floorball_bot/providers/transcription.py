from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

import httpx
import websockets

from floorball_bot.errors import PermanentProviderError, RetryableProviderError


@dataclass(frozen=True)
class TranscriptResult:
    provider_id: str
    text: str
    language: str
    confidence: float | None = None
    duration_seconds: float | None = None
    billing_metadata: dict | None = None


class Transcriber(Protocol):
    async def transcribe(self, path: Path, language: str) -> TranscriptResult: ...


class FakeTranscriber:
    def __init__(self, text: str = "Тестовая расшифровка", language: str = "ru") -> None:
        self.result = TranscriptResult("fake-1", text, language, 1.0, 1.0, {"external": False})
        self.paths: list[Path] = []

    async def transcribe(self, path: Path, language: str) -> TranscriptResult:
        self.paths.append(path)
        return TranscriptResult(**{**self.result.__dict__, "language": language})


class AssemblyAIBatchTranscriber:
    """Pre-recorded API for languages verified by the configured speech model."""

    def __init__(self, api_key: str, timeout_seconds: int = 300) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def transcribe(self, path: Path, language: str) -> TranscriptResult:
        if language == "kz":
            raise PermanentProviderError("Kazakh must use the verified Whisper streaming provider")
        headers = {"authorization": self.api_key}
        timeout = httpx.Timeout(30, read=60)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                audio_bytes = await asyncio.to_thread(path.read_bytes)
                upload = await client.post(
                    "https://api.assemblyai.com/v2/upload",
                    headers=headers,
                    content=audio_bytes,
                )
                upload.raise_for_status()
                request = {
                    "audio_url": upload.json()["upload_url"],
                    "language_code": "ru" if language == "ru" else language,
                    "speech_models": ["universal-3-pro"],
                    "punctuate": True,
                    "format_text": True,
                }
                created = await client.post(
                    "https://api.assemblyai.com/v2/transcript", headers=headers, json=request
                )
                created.raise_for_status()
                transcript_id = created.json()["id"]
                deadline = asyncio.get_running_loop().time() + self.timeout_seconds
                while asyncio.get_running_loop().time() < deadline:
                    response = await client.get(
                        f"https://api.assemblyai.com/v2/transcript/{transcript_id}", headers=headers
                    )
                    response.raise_for_status()
                    data = response.json()
                    if data["status"] == "completed":
                        return TranscriptResult(
                            provider_id=transcript_id,
                            text=(data.get("text") or "").strip(),
                            language=data.get("language_code") or language,
                            confidence=data.get("confidence"),
                            duration_seconds=data.get("audio_duration"),
                            billing_metadata={"speech_model": data.get("speech_model")},
                        )
                    if data["status"] == "error":
                        raise PermanentProviderError(data.get("error") or "AssemblyAI error")
                    await asyncio.sleep(2)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {408, 429, 500, 502, 503, 504}:
                raise RetryableProviderError(f"AssemblyAI HTTP {exc.response.status_code}") from exc
            raise PermanentProviderError(f"AssemblyAI HTTP {exc.response.status_code}") from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError("AssemblyAI network error") from exc
        raise RetryableProviderError("AssemblyAI transcript polling timeout")


class AssemblyAIWhisperStreamingTranscriber:
    """Whisper Streaming path used for mandatory Kazakh support.

    ffmpeg converts Telegram audio into bounded 16 kHz mono PCM. Audio is paced at
    wall-clock speed and the session is always terminated in a finally block.
    """

    def __init__(self, api_key: str, timeout_seconds: int = 300) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def transcribe(self, path: Path, language: str) -> TranscriptResult:
        if language not in {"kz", "kk"}:
            raise PermanentProviderError("Whisper streaming provider is reserved for Kazakh")
        query = urlencode({"speech_model": "whisper-rt", "sample_rate": 16000})
        uri = f"wss://streaming.assemblyai.com/v3/ws?{query}"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        turns: list[str] = []
        session_id = ""
        socket = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                socket = await websockets.connect(
                    uri,
                    additional_headers={"Authorization": self.api_key},
                    max_size=1_048_576,
                    open_timeout=20,
                    close_timeout=10,
                )
                first = json.loads(await socket.recv())
                session_id = str(first.get("id") or first.get("session_id") or "")

                async def receive() -> None:
                    async for raw in socket:
                        event = json.loads(raw)
                        if event.get("type") in {"Turn", "FinalTranscript"}:
                            text = str(event.get("transcript") or event.get("text") or "").strip()
                            if text and event.get("end_of_turn", True):
                                turns.append(text)
                        if event.get("type") in {"Termination", "SessionTerminated"}:
                            return

                receiver = asyncio.create_task(receive())
                assert process.stdout is not None
                while chunk := await process.stdout.read(3200):
                    await socket.send(chunk)
                    await asyncio.sleep(len(chunk) / (2 * 16000))
                await process.wait()
                if process.returncode:
                    assert process.stderr is not None
                    detail = (await process.stderr.read()).decode(errors="replace")[-1000:]
                    raise PermanentProviderError(f"ffmpeg failed: {detail}")
                await socket.send(json.dumps({"type": "Terminate"}))
                await receiver
        except TimeoutError as exc:
            raise RetryableProviderError("AssemblyAI Whisper streaming timeout") from exc
        except websockets.WebSocketException as exc:
            raise RetryableProviderError("AssemblyAI Whisper streaming error") from exc
        finally:
            if socket is not None:
                await socket.close()
            if process.returncode is None:
                process.kill()
                await process.wait()
        return TranscriptResult(
            provider_id=session_id,
            text=" ".join(turns).strip(),
            language="kz",
            billing_metadata={"speech_model": "whisper-rt", "paced": True},
        )


class RoutedAssemblyAITranscriber:
    def __init__(self, batch: Transcriber, kazakh: Transcriber) -> None:
        self.batch = batch
        self.kazakh = kazakh

    async def transcribe(self, path: Path, language: str) -> TranscriptResult:
        return await (self.kazakh if language in {"kz", "kk"} else self.batch).transcribe(
            path, language
        )
