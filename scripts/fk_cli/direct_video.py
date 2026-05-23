from __future__ import annotations

import json
import sys
from pathlib import Path

from agent.services.post_process import add_corner_logo, merge_videos
from flowkit_client import FlowkitError

from .common import (
    download,
    ensure_server,
    extract_video_output,
    flow_generate_video,
    flow_poll_video,
    get_output_dir,
    http_json,
    patch_scene,
)


def cmd_process_video_direct(args) -> int:
    api_base = args.api_base.rstrip("/")
    ensure_server(api_base)

    vid = args.video_id
    video = http_json("GET", f"{api_base}/api/videos/{vid}", timeout=60)
    pid = video.get("project_id")
    if not pid:
        raise FlowkitError(f"Video not found or missing project_id: {video}")
    orientation = (video.get("orientation") or "VERTICAL").upper()
    prefix = "vertical" if orientation == "VERTICAL" else "horizontal"

    pending = http_json(
        "GET",
        f"{api_base}/api/requests?video_id={vid}&status=PENDING",
        timeout=60,
    )
    if not isinstance(pending, list):
        raise FlowkitError(f"Invalid pending response: {pending}")
    pending = [r for r in pending if r.get("type") in ("GENERATE_VIDEO", "REGENERATE_VIDEO", "GENERATE_VIDEO_REFS")]
    pending.sort(key=lambda r: r.get("created_at") or "")
    if args.limit and args.limit > 0:
        pending = pending[: args.limit]

    processed = []
    failed = []

    total = len(pending)
    print(f"Direct processing: {total} requests (orientation={orientation})", file=sys.stderr, flush=True)

    for req in pending:
        rid = req.get("id")
        sid = req.get("scene_id")
        if not rid or not sid:
            continue
        print(f"- start {rid[:8]} scene={sid[:8]}", file=sys.stderr, flush=True)
        scene = http_json("GET", f"{api_base}/api/scenes/{sid}", timeout=60)
        start_mid = scene.get(f"{prefix}_image_media_id")
        end_mid = scene.get(f"{prefix}_end_scene_media_id")

        base_prompt = None
        if end_mid and scene.get("transition_prompt"):
            base_prompt = scene.get("transition_prompt")
        base_prompt = base_prompt or scene.get("video_prompt") or scene.get("prompt") or "Cinematic realistic motion."

        if not start_mid:
            http_json(
                "PATCH",
                f"{api_base}/api/requests/{rid}",
                {"status": "FAILED", "error_message": "Missing scene start image media_id"},
                timeout=60,
            )
            failed.append({"request_id": rid, "scene_id": sid, "error": "missing start_image_media_id"})
            continue

        try:
            print(f"  submit Flow generate-video (start={start_mid[:8]} end={(end_mid[:8] if end_mid else 'none')})", file=sys.stderr, flush=True)
            ops = flow_generate_video(
                api_base,
                project_id=pid,
                scene_id=sid,
                orientation=orientation,
                start_image_media_id=start_mid,
                end_image_media_id=end_mid,
                prompt=base_prompt,
                video_model_key=args.video_model_key,
            )
            print("  polling...", file=sys.stderr, flush=True)
            polled = flow_poll_video(api_base, ops, poll_interval_s=args.poll_interval, timeout_s=args.timeout)
            if polled.get("error"):
                raise FlowkitError(polled["error"])
            media_id, url = extract_video_output(polled)
            if media_id and not url:
                media = http_json("GET", f"{api_base}/api/flow/media/{media_id}", timeout=30)
                if isinstance(media, dict):
                    url = media.get("fifeUrl") or media.get("servingUri")

            patch_scene(api_base, sid, {
                f"{prefix}_video_media_id": media_id,
                f"{prefix}_video_url": url,
                f"{prefix}_video_status": "COMPLETED" if (media_id or url) else "FAILED",
            })
            http_json(
                "PATCH",
                f"{api_base}/api/requests/{rid}",
                {"status": "COMPLETED" if (media_id or url) else "FAILED", "media_id": media_id, "output_url": url},
                timeout=60,
            )
            print(f"  done media_id={(media_id[:8] if media_id else 'none')}", file=sys.stderr, flush=True)
            processed.append({"request_id": rid, "scene_id": sid, "media_id": media_id, "url": url})
        except Exception as e:
            http_json(
                "PATCH",
                f"{api_base}/api/requests/{rid}",
                {"status": "FAILED", "error_message": str(e)[:200]},
                timeout=60,
            )
            print(f"  failed: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr, flush=True)
            failed.append({"request_id": rid, "scene_id": sid, "error": str(e)})

    print(json.dumps({
        "video_id": vid,
        "project_id": pid,
        "orientation": orientation,
        "processed": processed,
        "failed": failed,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_merge_video(args) -> int:
    api_base = args.api_base.rstrip("/")
    ensure_server(api_base)

    vid = args.video_id
    video = http_json("GET", f"{api_base}/api/videos/{vid}", timeout=60)
    pid = video.get("project_id")
    if not pid:
        raise FlowkitError(f"Video not found or missing project_id: {video}")
    orientation = (video.get("orientation") or "VERTICAL").upper()
    prefix = "vertical" if orientation == "VERTICAL" else "horizontal"

    slug, out_dir = get_output_dir(api_base, pid)
    subclips_dir = out_dir / "subclips"

    scenes = http_json("GET", f"{api_base}/api/scenes?video_id={vid}", timeout=60)
    if not isinstance(scenes, list):
        raise FlowkitError(f"Invalid scenes response: {scenes}")
    scenes.sort(key=lambda s: int(s.get("display_order") or 0))

    local_paths: list[str] = []
    for i, s in enumerate(scenes):
        url = None
        if args.use_upscale:
            url = s.get(f"{prefix}_upscale_url")
        url = url or s.get(f"{prefix}_video_url")
        if not url:
            raise FlowkitError(f"Missing video URL for scene {s.get('id')} (order {i})")
        out_path = subclips_dir / f"scene_{i:03d}_{s.get('id','scene')}.mp4"
        download(url, out_path)
        local_paths.append(str(out_path))

    final_path = out_dir / f"{slug}_final.mp4"
    ok = merge_videos(local_paths, str(final_path))
    if not ok:
        raise FlowkitError("Final merge failed (ffmpeg concat)")

    if not args.no_watermark:
        wm_path = out_dir / f"{slug}_final_watermarked.mp4"
        ok_wm = add_corner_logo(
            str(final_path),
            str(wm_path),
            logo_path=str(Path(args.logo_path).resolve()),
            box_w=int(args.logo_w),
            box_h=int(args.logo_h),
            margin=int(args.logo_margin),
        )
        if not ok_wm:
            raise FlowkitError("Watermark failed (ffmpeg overlay)")
        final_path.unlink(missing_ok=True)
        wm_path.replace(final_path)

    print(json.dumps({
        "video_id": vid,
        "project_id": pid,
        "orientation": orientation,
        "output_dir": str(out_dir),
        "final_video": str(final_path),
        "scenes": len(scenes),
    }, indent=2, ensure_ascii=False))
    return 0
