"""Render compact REVIEW-only contact sheets for ranked stage candidates."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from .io import sha256_file, write_json_atomic


def _list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _safe_name(value: Any) -> str:
    compact = "".join(character if character.isalnum() else "_" for character in str(value))
    return compact.strip("_")[:48] or "selection"


def _fit_image(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    contained = ImageOps.contain(source.convert("RGB"), size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, (24, 29, 35))
    x = (size[0] - contained.width) // 2
    y = (size[1] - contained.height) // 2
    canvas.paste(contained, (x, y))
    return canvas


def render_stage_candidate_boards(
    regions_payload: Mapping[str, Any],
    output_dir: Path | str,
    *,
    columns: int = 4,
    tile_width: int = 420,
    tile_height: int = 300,
    maximum_per_board: int = 24,
) -> dict[str, Any]:
    """Render bounded candidate overview boards without selecting a candidate."""

    if columns < 1 or tile_width < 160 or tile_height < 140 or maximum_per_board < 1:
        raise ValueError("invalid board geometry or candidate limit")
    destination = Path(output_dir).resolve()
    board_dir = destination / "boards"
    board_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    board_count = 0
    candidate_count = 0
    missing_image_count = 0
    for record_index, raw_record in enumerate(_list(regions_payload.get("records")), start=1):
        if not isinstance(raw_record, Mapping):
            continue
        candidates = [
            value
            for value in _list(raw_record.get("evidence"))
            if isinstance(value, Mapping)
        ][:maximum_per_board]
        usable: list[tuple[Mapping[str, Any], Path]] = []
        missing: list[dict[str, Any]] = []
        for candidate in candidates:
            path = Path(str(candidate.get("absolute_path") or ""))
            if not path.is_file():
                missing_image_count += 1
                missing.append(
                    {
                        "stage_candidate_id": candidate.get("stage_candidate_id"),
                        "sheet_id": candidate.get("sheet_id"),
                        "reason": "CANDIDATE_IMAGE_MISSING",
                    }
                )
                continue
            usable.append((candidate, path))

        output_record: dict[str, Any] = {
            "selection_key": raw_record.get("selection_key"),
            "component_id": raw_record.get("component_id"),
            "sequence": raw_record.get("sequence", record_index),
            "name": raw_record.get("name"),
            "stage": raw_record.get("stage") or regions_payload.get("stage"),
            "state": "REVIEW" if usable else "MISSING",
            "reason_codes": [
                "CONTACT_SHEET_REQUIRES_EXPLICIT_CANDIDATE_SELECTION"
            ] if usable else ["NO_USABLE_CANDIDATE_IMAGES"],
            "missing_candidates": missing,
            "candidates": [],
        }
        if not usable:
            records.append(output_record)
            continue

        rows = (len(usable) + columns - 1) // columns
        header_height = 50
        board = Image.new(
            "RGB",
            (columns * tile_width, header_height + rows * tile_height),
            (16, 20, 25),
        )
        draw = ImageDraw.Draw(board)
        header = (
            f"SEQ {output_record['sequence']}  STAGE {output_record['stage']}  "
            f"CANDIDATES {len(usable)}  REVIEW ONLY"
        )
        draw.text((12, 16), header, fill=(235, 238, 242))
        image_height = tile_height - 50
        for index, (candidate, path) in enumerate(usable, start=1):
            row = (index - 1) // columns
            column = (index - 1) % columns
            x = column * tile_width
            y = header_height + row * tile_height
            with Image.open(path) as source:
                fitted = _fit_image(source, (tile_width - 8, image_height - 8))
            board.paste(fitted, (x + 4, y + 4))
            label = f"C{index:02d}"
            drawing_number = str(candidate.get("drawing_number") or "-")
            source_kind = str(candidate.get("candidate_source") or "candidate")
            score = candidate.get("retrieval_rank_score")
            score_text = f"{float(score):.3f}" if isinstance(score, (int, float)) else "-"
            draw.rectangle(
                (x, y + image_height, x + tile_width - 1, y + tile_height - 1),
                fill=(30, 36, 43),
                outline=(75, 86, 99),
            )
            draw.text(
                (x + 8, y + image_height + 8),
                f"{label}  {drawing_number}  {source_kind}  score={score_text}",
                fill=(238, 241, 245),
            )
            output_record["candidates"].append(
                {
                    "board_label": label,
                    "stage_candidate_rank": candidate.get("stage_candidate_rank"),
                    "stage_candidate_id": candidate.get("stage_candidate_id"),
                    "sheet_id": candidate.get("sheet_id"),
                    "drawing_number": candidate.get("drawing_number"),
                    "candidate_source": candidate.get("candidate_source"),
                    "retrieval_rank_score": candidate.get("retrieval_rank_score"),
                    "absolute_path": str(path.resolve()),
                    "state": "REVIEW",
                }
            )
        identity = "\0".join(
            [
                str(output_record.get("selection_key") or output_record["sequence"]),
                str(output_record.get("stage") or ""),
                *[
                    str(value.get("stage_candidate_id") or value.get("sheet_id"))
                    for value in output_record["candidates"]
                ],
            ]
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
        board_path = board_dir / (
            f"{int(output_record['sequence']):03d}-"
            f"{_safe_name(output_record.get('selection_key'))}-{digest}.png"
        )
        board.save(board_path, format="PNG", optimize=True)
        output_record["board_absolute_path"] = str(board_path)
        output_record["board_sha256"] = sha256_file(board_path)
        output_record["board_pixel_size"] = list(board.size)
        output_record["candidate_count"] = len(output_record["candidates"])
        output_record["candidate_truncated"] = len(
            _list(raw_record.get("evidence"))
        ) > maximum_per_board
        records.append(output_record)
        board_count += 1
        candidate_count += len(output_record["candidates"])

    result = {
        "schema_version": "1.0",
        "purpose": "ranked_stage_candidate_contact_sheets_review_only",
        "path_scope": "local_run_diagnostics",
        "warning": (
            "A contact sheet only makes candidate comparison cheaper. It does not select a "
            "physical component, confirm a relation, assign a measurement role, or permit PASS."
        ),
        "stage": regions_payload.get("stage"),
        "board_count": board_count,
        "candidate_count": candidate_count,
        "missing_image_count": missing_image_count,
        "maximum_per_board": maximum_per_board,
        "records": records,
    }
    write_json_atomic(destination / "stage_candidate_boards.json", result)
    return result


__all__ = ["render_stage_candidate_boards"]
