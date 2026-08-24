from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from PIL import Image, ImageOps, UnidentifiedImageError

from floorball_bot.errors import ValidationBlocked

ALLOWED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass(frozen=True)
class ProcessedMedia:
    sha256: str
    detected_mime: str
    bytes: int
    width: int
    height: int
    original_path: Path
    derivative_path: Path


class MediaPipeline:
    def __init__(
        self,
        root: Path,
        *,
        max_input_bytes: int = 20 * 1024 * 1024,
        max_pixels: int = 40_000_000,
        max_derivative_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.root = root
        self.max_input_bytes = max_input_bytes
        self.max_pixels = max_pixels
        self.max_derivative_bytes = max_derivative_bytes

    def process_image(self, source: Path, uploader_id: UUID) -> ProcessedMedia:
        size = source.stat().st_size
        if size <= 0 or size > self.max_input_bytes:
            raise ValidationBlocked("image size is outside allowed bounds")
        digest = self._sha256(source)
        try:
            with Image.open(source) as image:
                image.verify()
            with Image.open(source) as image:
                detected = ALLOWED_FORMATS.get(image.format or "")
                if not detected:
                    raise ValidationBlocked("unsupported image content")
                if image.width * image.height > self.max_pixels:
                    raise ValidationBlocked("image pixel count exceeds limit")
                image = ImageOps.exif_transpose(image)
                image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
                output = image.convert("RGB")
                originals = self.root / "originals" / str(uploader_id)
                derivatives = self.root / "derived" / digest[:2]
                originals.mkdir(parents=True, exist_ok=True, mode=0o700)
                derivatives.mkdir(parents=True, exist_ok=True, mode=0o700)
                original_path = originals / digest
                derivative_path = derivatives / f"{digest}.webp"
                if not original_path.exists():
                    self._copy_exclusive(source, original_path)
                quality = 82
                while True:
                    with tempfile.NamedTemporaryFile(dir=derivatives, delete=False) as temporary:
                        temporary_path = Path(temporary.name)
                    try:
                        output.save(temporary_path, "WEBP", quality=quality, method=6, exif=b"")
                        if (
                            temporary_path.stat().st_size <= self.max_derivative_bytes
                            or quality <= 55
                        ):
                            os.chmod(temporary_path, 0o600)
                            temporary_path.replace(derivative_path)
                            break
                    finally:
                        temporary_path.unlink(missing_ok=True)
                    quality -= 7
                if derivative_path.stat().st_size > self.max_derivative_bytes:
                    derivative_path.unlink(missing_ok=True)
                    raise ValidationBlocked("public derivative exceeds size limit")
                return ProcessedMedia(
                    sha256=digest,
                    detected_mime=detected,
                    bytes=size,
                    width=output.width,
                    height=output.height,
                    original_path=original_path,
                    derivative_path=derivative_path,
                )
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
            quarantine = self.root / "quarantine"
            quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
            raise ValidationBlocked("invalid or unsafe image") from exc

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _copy_exclusive(source: Path, target: Path) -> None:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
                descriptor = -1
                for block in iter(lambda: input_file.read(1024 * 1024), b""):
                    output_file.write(block)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
