"""
Image processor — resize and compress photos/signatures to meet
Sarathi portal upload requirements before the agent uploads them.

Sarathi limits:
  - Photo      : max 20 KB, recommended 200x200 px
  - Signature  : max 10 KB
  - Documents  : max 200 KB (PDF or JPEG)
"""

import io
import os
import structlog
from pathlib import Path
from typing import Optional
from PIL import Image

from config.settings import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()


class ImageProcessor:

    def compress_photo(self, input_path: str) -> str:
        """Resize + compress a photo to meet Sarathi photo requirements."""
        return self._process(
            input_path=input_path,
            max_kb=settings.photo_max_size_kb,
            target_width=settings.photo_width_px,
            target_height=settings.photo_height_px,
            suffix="_photo",
        )

    def compress_signature(self, input_path: str) -> str:
        """Compress a signature image to meet Sarathi signature requirements."""
        return self._process(
            input_path=input_path,
            max_kb=settings.signature_max_size_kb,
            target_width=None,
            target_height=None,
            suffix="_sig",
        )

    def compress_document(self, input_path: str, max_kb: int = 200) -> str:
        """Compress a document image for upload."""
        return self._process(
            input_path=input_path,
            max_kb=max_kb,
            target_width=None,
            target_height=None,
            suffix="_doc",
        )

    def _process(
        self,
        input_path: str,
        max_kb: int,
        target_width: Optional[int],
        target_height: Optional[int],
        suffix: str,
    ) -> str:
        p = Path(input_path)
        output_path = str(p.parent / f"{p.stem}{suffix}.jpg")

        img = Image.open(input_path).convert("RGB")

        # Resize if target dimensions given
        if target_width and target_height:
            img = img.resize((target_width, target_height), Image.LANCZOS)

        # Binary search for quality that fits under max_kb
        max_bytes = max_kb * 1024
        quality = 90
        while quality >= 10:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            size = buf.tell()
            if size <= max_bytes:
                break
            quality -= 10

        if quality < 10:
            # Still too large — scale down dimensions further
            scale = 0.8
            while quality == 10:
                w = int(img.width * scale)
                h = int(img.height * scale)
                img = img.resize((w, h), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=10, optimize=True)
                if buf.tell() <= max_bytes:
                    break
                scale -= 0.1

        with open(output_path, "wb") as f:
            buf.seek(0)
            f.write(buf.read())

        final_kb = os.path.getsize(output_path) / 1024
        log.info(
            "image.compressed",
            input=input_path,
            output=output_path,
            final_kb=round(final_kb, 1),
            quality=quality,
        )
        return output_path
