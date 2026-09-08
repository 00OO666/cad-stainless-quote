"""Export native group images for probed navigation candidates, not quote approval."""

from collections import defaultdict
from pathlib import Path

from PIL import Image

from .io import sha256_file, write_json_atomic
from .linking import _stable_id
from .render import render_regions
from .viewport_groups import other_group_title_handles


def render_probed_groups(index_payload, routes, probes, output_dir, *, target_px=3200):
    sources = {s["source_file_id"]: s for s in index_payload["sources"]}
    catalogue = routes["node_view_groups"]
    groups = {g["group_id"]: g for g in catalogue["groups"]}
    batches, images, issues = defaultdict(list), {}, []
    for gid, probe in probes["results"].items():
        group = groups[gid]
        if group["group_state"] != "UNIQUE_TITLE_GROUP_CANDIDATE" or probe["truncated"]:
            issues.append({"group_id": gid, "reason": "UNRESOLVED_OR_TRUNCATED_GROUP"})
            continue
        batches[(group["source_file_id"], group["space"])].append(gid)
    out = Path(output_dir).resolve()
    for (sid, space), gids in sorted(batches.items()):
        try:
            source = sources[sid]
            path = Path(source["source_path"])
            expected = source["source_sha256"]
            if (
                path.suffix.lower() != ".dxf"
                or sha256_file(path) != expected
                or any(probes["results"][g].get("source_sha256") != expected for g in gids)
            ):
                raise ValueError("indexed/probed/native source hashes disagree")
            batch = _stable_id("image-batch", {"source": sid, "space": space}).split(":")[-1]
            destination = out / batch
            result = render_regions(
                path,
                {g: probes["results"][g]["paper_evidence_bbox"] for g in gids},
                destination,
                layout=space.removeprefix("paper:"),
                target_px=target_px,
                mark_center=False,
                margin_ratio=0.015,
                render_profile="cad-dark-full",
                paper_viewport_sets={g: groups[g]["viewport_handles"] for g in gids},
                paper_excluded_handles={
                    g: other_group_title_handles(groups[g], catalogue) for g in gids
                },
            )
            if sha256_file(path) != expected:
                raise ValueError("source changed during render; generated batch unverified")
            completed = {}
            for gid, record in result["regions"].items():
                image_path = destination / record["file"]
                with Image.open(image_path) as im:
                    im.load()
                    pixels = list(im.size)
                completed[gid] = {
                    **record,
                    "absolute_path": str(image_path),
                    "natural_pixels": pixels,
                    "sha256": sha256_file(image_path),
                    "source_sha256": expected,
                    "state": "REVIEW",
                    "view_only_not_physical_component": True,
                }
            images.update(completed)
            for gid in set(gids) - set(completed):
                issues.append({"group_id": gid, "reason": "IMAGE_NOT_RENDERED"})
        except Exception as exc:
            issues.append({"group_ids": gids, "reason": type(exc).__name__ + ": " + str(exc)})
    manifest = {
        "state": "REVIEW",
        "requested_groups": len(probes["results"]),
        "rendered_groups": len(images),
        "images": images,
        "issues": issues,
        "mutates_takeoff": False,
        "scope": "native selected viewports; other numbered titles excluded explicitly",
        "limitations": [
            "Native proxy/rendering limitations and neighbouring context remain.",
            "Successful export does not approve quote screenshot or quantity.",
        ],
    }
    write_json_atomic(out / "group-images.json", manifest)
    return manifest
