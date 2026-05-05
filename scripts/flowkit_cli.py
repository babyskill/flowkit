#!/usr/bin/env python3
"""FlowKit CLI wrapper to avoid manual curl commands."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from flowkit_client import (
    CharacterConfig,
    CreateImageInput,
    CreateVideoInput,
    FlowkitError,
    RequestConfig,
    SceneConfig,
    create_image_from_reference,
    create_video_from_reference,
    ensure_health,
    wait_request,
)
from agent.services.post_process import merge_videos


def _slugify(text: str) -> str:
    out = []
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "project"


def _http_json(method: str, url: str, data: dict | None = None, timeout: int = 120) -> dict:
    import urllib.request
    import urllib.error

    body = None
    headers = {"Content-Type": "application/json"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        raise FlowkitError(f"HTTP {e.code} {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise FlowkitError(f"Request failed {url}: {e}") from e


def _run_ffprobe_json(args: list[str]) -> dict:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FlowkitError(f"ffprobe failed: {proc.stderr[-400:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise FlowkitError(f"ffprobe returned non-JSON output: {proc.stdout[:200]}") from e


def _probe_video(video_path: Path) -> tuple[int, int, float]:
    info = _run_ffprobe_json([
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        str(video_path),
    ])
    streams = info.get("streams") or []
    if not streams:
        raise FlowkitError(f"No video stream found: {video_path}")
    w = int(streams[0].get("width") or 0)
    h = int(streams[0].get("height") or 0)
    if w <= 0 or h <= 0:
        raise FlowkitError(f"Invalid width/height from ffprobe: {video_path}")

    dur_raw = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video_path)],
        capture_output=True,
        text=True,
    )
    if dur_raw.returncode != 0:
        raise FlowkitError(f"ffprobe duration failed: {dur_raw.stderr[-200:]}")
    try:
        duration_s = float(dur_raw.stdout.strip())
    except ValueError as e:
        raise FlowkitError(f"Invalid duration from ffprobe: {dur_raw.stdout!r}") from e
    return w, h, duration_s


def _extract_frame(video_path: Path, t: float, out_path: Path) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{t:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FlowkitError(f"ffmpeg frame extract failed: {proc.stderr[-400:]}")
    if not out_path.exists():
        raise FlowkitError(f"Frame not created: {out_path}")
    return str(out_path)


def _upload_local_image(api_base: str, file_path: str, *, project_id: str, file_name: str) -> str:
    res = _http_json(
        "POST",
        f"{api_base.rstrip('/')}/api/flow/upload-image",
        {"file_path": file_path, "project_id": project_id, "file_name": file_name},
        timeout=120,
    )
    media_id = res.get("media_id")
    if not media_id:
        raise FlowkitError(f"upload-image returned no media_id: {res}")
    return media_id


def _create_project(api_base: str, name: str, *, material: str, language: str) -> str:
    body = {"name": name, "material": material, "language": language}
    proj = _http_json("POST", f"{api_base.rstrip('/')}/api/projects", body, timeout=60)
    pid = proj.get("id")
    if not pid:
        raise FlowkitError(f"Create project failed: {proj}")
    return pid


def _create_video(api_base: str, project_id: str, title: str, orientation: str) -> str:
    vid = _http_json(
        "POST",
        f"{api_base.rstrip('/')}/api/videos",
        {"project_id": project_id, "title": title, "orientation": orientation},
        timeout=60,
    )
    video_id = vid.get("id")
    if not video_id:
        raise FlowkitError(f"Create video failed: {vid}")
    return video_id


def _create_scene(api_base: str, video_id: str, *, display_order: int, prompt: str,
                  video_prompt: str | None = None, transition_prompt: str | None = None,
                  chain_type: str = "ROOT", parent_scene_id: str | None = None, source: str = "user") -> str:
    body: dict = {
        "video_id": video_id,
        "display_order": display_order,
        "prompt": prompt,
        "chain_type": chain_type,
        "source": source,
    }
    if video_prompt is not None:
        body["video_prompt"] = video_prompt
    if transition_prompt is not None:
        body["transition_prompt"] = transition_prompt
    if parent_scene_id is not None:
        body["parent_scene_id"] = parent_scene_id
    res = _http_json("POST", f"{api_base.rstrip('/')}/api/scenes", body, timeout=60)
    sid = res.get("id")
    if not sid:
        raise FlowkitError(f"Create scene failed: {res}")
    return sid


def _patch_scene(api_base: str, scene_id: str, patch: dict) -> dict:
    return _http_json("PATCH", f"{api_base.rstrip('/')}/api/scenes/{scene_id}", patch, timeout=60)


def _submit_request(api_base: str, *, request_type: str, project_id: str,
                    video_id: str | None = None, scene_id: str | None = None,
                    orientation: str | None = None, source_media_id: str | None = None) -> str:
    body: dict = {"type": request_type, "project_id": project_id}
    if video_id is not None:
        body["video_id"] = video_id
    if scene_id is not None:
        body["scene_id"] = scene_id
    if orientation is not None:
        body["orientation"] = orientation
    if source_media_id is not None:
        body["source_media_id"] = source_media_id
    res = _http_json("POST", f"{api_base.rstrip('/')}/api/requests", body, timeout=60)
    rid = res.get("id")
    if not rid:
        raise FlowkitError(f"Submit request failed: {res}")
    return rid


def _download(url: str, out_path: Path) -> None:
    import urllib.request
    import ssl
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out_path.write_bytes(resp.read())
            return
    except Exception:
        with urllib.request.urlopen(req, timeout=120, context=ssl._create_unverified_context()) as resp:
            out_path.write_bytes(resp.read())


def _cmd_edit_from_source(args: argparse.Namespace) -> int:
    api_base = args.api_base.rstrip("/")
    ensure_health(api_base)

    source_dir = Path(args.source_dir).resolve()
    if not source_dir.exists():
        raise FlowkitError(f"source_dir not found: {source_dir}")

    candidates = sorted([p for p in source_dir.glob("*.mp4") if p.is_file()])
    if not candidates:
        raise FlowkitError(f"No .mp4 files found in {source_dir}")

    hook_video = None
    work_video = None
    for p in candidates:
        name = p.name.lower()
        if hook_video is None and "excavator" in name:
            hook_video = p
        elif work_video is None and "excavator" not in name:
            work_video = p
    hook_video = hook_video or candidates[0]
    work_video = work_video or (candidates[1] if len(candidates) > 1 else candidates[0])
    intro_video = Path(args.intro_mp4).resolve() if args.intro_mp4 else None
    outro_video = Path(args.outro_mp4).resolve() if args.outro_mp4 else None
    if intro_video and not intro_video.exists():
        raise FlowkitError(f"intro_mp4 not found: {intro_video}")
    if outro_video and not outro_video.exists():
        raise FlowkitError(f"outro_mp4 not found: {outro_video}")

    _, _, dur_hook = _probe_video(hook_video)
    w2, h2, dur_work = _probe_video(work_video)
    dur_intro = 0.0
    dur_outro = 0.0
    if intro_video:
        _, _, dur_intro = _probe_video(intro_video)
    if outro_video:
        _, _, dur_outro = _probe_video(outro_video)
    orientation = args.orientation
    if orientation == "AUTO":
        orientation = "HORIZONTAL" if w2 >= h2 else "VERTICAL"

    project_name = args.project_name
    video_title = args.video_title or project_name
    pid = args.project_id or _create_project(api_base, project_name, material=args.material, language=args.language)
    vid = _create_video(api_base, pid, video_title, orientation)

    out = _http_json("GET", f"{api_base}/api/projects/{pid}/output-dir", timeout=60)
    slug = out.get("slug") or _slugify(project_name)
    out_dir = (Path(__file__).resolve().parent.parent / "output" / slug).resolve()
    frames_dir = out_dir / "source_frames"
    subclips_dir = out_dir / "subclips"

    def pick_times(duration_s: float, t1_ratio: float, t2_ratio: float) -> tuple[float, float]:
        a = max(0.5, duration_s * t1_ratio)
        b = max(a + 0.5, duration_s * t2_ratio)
        b = min(duration_s - 0.5, b) if duration_s > 2.0 else min(duration_s * 0.8, b)
        return a, b

    t_hook_a, t_hook_b = pick_times(dur_hook, 0.12, 0.62)
    t_work_a, t_work_b = pick_times(dur_work, 0.18, 0.72)
    t_intro = max(0.5, dur_intro * 0.35) if dur_intro else 0.0
    t_outro = max(0.5, dur_outro * 0.55) if dur_outro else 0.0

    plan = []
    if intro_video:
        plan.append({
            "label": "intro",
            "video": intro_video,
            "t": t_intro,
            "prompt": "Photorealistic cinematic intro shot of a powerful wheeled excavator. Mystery mood, low angle, industrial documentary, sharp detail, natural colors, no fantasy.",
            "video_prompt": "0-2s: slow cinematic reveal. 2-6s: subtle push-in, engine vibration. 6-8s: quick emphasis beat, no added music.",
        })
    plan.extend([
        {
            "label": "hook",
            "video": hook_video,
            "t": t_hook_a,
            "prompt": "Photorealistic documentary shot of a wheeled excavator descending from a truck bed. Emphasize heavy weight, dust puffs, hydraulic details, sharp natural lighting, realistic colors.",
            "video_prompt": "0-2s: dramatic slow motion. 2-6s: subtle handheld shake, dust particles. 6-8s: quick cut feel, keep realism, no added music.",
        },
        {
            "label": "landing",
            "video": hook_video,
            "t": t_hook_b,
            "prompt": "Photorealistic excavator landing on the ground after descending. Emphasize suspension bounce, tire compression, small dust cloud, strong contrast, cinematic industrial mood.",
            "video_prompt": "0-3s: slow push-in. 3-6s: micro vibration from engine. 6-8s: settle and hold, realistic motion blur.",
        },
        {
            "label": "mystery_scan",
            "video": hook_video,
            "t": t_hook_b,
            "prompt": "Photorealistic excavator with subtle technical HUD overlay in-camera (lightweight, semi-transparent). Show minimal scan lines and small readouts, cinematic documentary, no neon overload.",
            "video_prompt": "0-8s: minimal HUD elements animate subtly, camera steady, keep realism, no sci-fi environment changes.",
        },
        {
            "label": "work_start",
            "video": work_video,
            "t": t_work_a,
            "prompt": "Photorealistic excavator starting to grab logs in a forest yard. Emphasize hydraulic power, wood texture, flying dust, crisp details, natural daylight.",
            "video_prompt": "0-3s: wide establishing. 3-6s: arm moves to grab, realistic speed. 6-8s: clamp tight, subtle camera shake.",
        },
        {
            "label": "hydraulic_macro",
            "video": work_video,
            "t": t_work_a,
            "prompt": "Photorealistic macro close-up of hydraulic piston and hoses working under load. Oil sheen, metal texture, tiny dust, documentary realism.",
            "video_prompt": "0-2s: macro hold. 2-6s: piston movement, hydraulic hiss feel. 6-8s: micro vibration, keep sharp realism.",
        },
        {
            "label": "best_grab",
            "video": work_video,
            "t": t_work_b,
            "prompt": "Photorealistic close shot of excavator grabbing and lifting logs. Emphasize force, tiny debris, realistic shadows, cinematic contrast.",
            "video_prompt": "0-2s: medium close-up. 2-6s: lift with slow motion accents. 6-8s: hold and slight pan, keep realism.",
        },
    ])
    if outro_video:
        plan.append({
            "label": "outro",
            "video": outro_video,
            "t": t_outro,
            "prompt": "Photorealistic cinematic outro shot of the excavator finishing the job. Calm but powerful ending, documentary realism, natural color grade, subtle dust in air.",
            "video_prompt": "0-4s: slow pull-back. 4-8s: settle, hold, satisfyingly heavy mood, no added music.",
        })

    scene_results: list[dict] = []
    for i, item in enumerate(plan):
        frame_path = frames_dir / f"{i:02d}_{item['label']}.jpg"
        _extract_frame(Path(item["video"]), float(item["t"]), frame_path)
        frame_mid = _upload_local_image(api_base, str(frame_path), project_id=pid, file_name=frame_path.name)

        sid = _create_scene(
            api_base,
            vid,
            display_order=len(scene_results),
            prompt=item["prompt"],
            video_prompt=item["video_prompt"],
            chain_type="ROOT",
            source="user",
        )

        prefix = "vertical" if orientation == "VERTICAL" else "horizontal"
        _patch_scene(api_base, sid, {
            f"{prefix}_image_media_id": frame_mid,
            f"{prefix}_image_status": "COMPLETED",
        })

        rid_vid = _submit_request(
            api_base,
            request_type="GENERATE_VIDEO",
            project_id=pid,
            video_id=vid,
            scene_id=sid,
            orientation=orientation,
        )
        if args.wait:
            vid_done = wait_request(api_base, rid_vid, poll_interval_s=args.poll_interval, timeout_s=args.timeout)
            if vid_done.get("status") != "COMPLETED":
                raise FlowkitError(f"Video request failed: {vid_done.get('error_message')}")

        scene_final = _http_json("GET", f"{api_base}/api/scenes/{sid}", timeout=60)
        scene_results.append({
            "scene_id": sid,
            "image_media_id": scene_final.get(f"{prefix}_image_media_id"),
            "image_url": scene_final.get(f"{prefix}_image_url"),
            "video_url": scene_final.get(f"{prefix}_video_url"),
            "label": item["label"],
            "request_ids": {"video": rid_vid},
        })

    transition_results: list[dict] = []
    for idx in range(len(scene_results) - 1):
        a = scene_results[idx]
        b = scene_results[idx + 1]
        sid_t = _create_scene(
            api_base,
            vid,
            display_order=len(scene_results) + len(transition_results),
            prompt="Cinematic transition clip between two shots.",
            transition_prompt="Match-cut transition with realistic motion blur and dust. Keep photorealistic style, preserve colors and lighting continuity. No added music.",
            chain_type="ROOT",
            source="user",
        )
        prefix = "vertical" if orientation == "VERTICAL" else "horizontal"
        patch = {
            f"{prefix}_image_media_id": a["image_media_id"],
            f"{prefix}_image_status": "COMPLETED",
            f"{prefix}_image_url": a["image_url"],
            f"{prefix}_end_scene_media_id": b["image_media_id"],
        }
        _patch_scene(api_base, sid_t, patch)

        rid_t = _submit_request(
            api_base,
            request_type="GENERATE_VIDEO",
            project_id=pid,
            video_id=vid,
            scene_id=sid_t,
            orientation=orientation,
        )
        if args.wait:
            done_t = wait_request(api_base, rid_t, poll_interval_s=args.poll_interval, timeout_s=args.timeout)
            if done_t.get("status") != "COMPLETED":
                raise FlowkitError(f"Transition video request failed: {done_t.get('error_message')}")

        scene_t = _http_json("GET", f"{api_base}/api/scenes/{sid_t}", timeout=60)
        transition_results.append({
            "scene_id": sid_t,
            "video_url": scene_t.get(f"{prefix}_video_url"),
            "label": f"transition_{a['label']}_to_{b['label']}",
            "request_ids": {"video": rid_t},
        })

    if not args.wait:
        print(json.dumps({
            "project_id": pid,
            "video_id": vid,
            "orientation": orientation,
            "note": "Submitted video generation requests. Use the dashboard or /fk-monitor to track progress. Re-run with --wait to auto-download + merge when ready.",
            "source": {"hook_video": str(hook_video), "work_video": str(work_video)},
            "output": {"slug": slug, "output_dir": str(out_dir), "frames_dir": str(frames_dir)},
            "scenes": scene_results,
            "transitions": transition_results,
        }, indent=2, ensure_ascii=False))
        return 0

    ordered = []
    for i in range(len(scene_results)):
        ordered.append(("scene", i, scene_results[i]))
        if i < len(transition_results):
            ordered.append(("transition", i, transition_results[i]))

    local_paths: list[str] = []
    for order_idx, (_, _, item) in enumerate(ordered):
        url = item.get("video_url")
        if not url:
            raise FlowkitError(f"Missing video_url for {item}")
        out_path = subclips_dir / f"scene_{order_idx:03d}_{item['label']}.mp4"
        _download(url, out_path)
        local_paths.append(str(out_path))

    final_path = out_dir / f"{slug}_final.mp4"
    ok = merge_videos(local_paths, str(final_path))
    if not ok:
        raise FlowkitError("Final merge failed (ffmpeg concat)")

    print(json.dumps({
        "project_id": pid,
        "video_id": vid,
        "orientation": orientation,
        "source": {"hook_video": str(hook_video), "work_video": str(work_video)},
        "output": {
            "slug": slug,
            "output_dir": str(out_dir),
            "frames_dir": str(frames_dir),
            "subclips_dir": str(subclips_dir),
            "final_video": str(final_path),
        },
        "scenes": scene_results,
        "transitions": transition_results,
    }, indent=2, ensure_ascii=False))
    return 0


def _extract_operations(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("operations"), list):
        return payload["operations"]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("operations"), list):
        return data["operations"]
    return []


def _flow_generate_video(
    api_base: str,
    *,
    project_id: str,
    scene_id: str,
    orientation: str,
    start_image_media_id: str,
    prompt: str,
    end_image_media_id: str | None = None,
    user_paygate_tier: str = "PAYGATE_TIER_ONE",
) -> list[dict]:
    aspect_ratio = "VIDEO_ASPECT_RATIO_LANDSCAPE" if orientation == "HORIZONTAL" else "VIDEO_ASPECT_RATIO_PORTRAIT"
    body = {
        "start_image_media_id": start_image_media_id,
        "prompt": prompt,
        "project_id": project_id,
        "scene_id": scene_id,
        "aspect_ratio": aspect_ratio,
        "user_paygate_tier": user_paygate_tier,
    }
    if end_image_media_id:
        body["end_image_media_id"] = end_image_media_id
    res = _http_json("POST", f"{api_base.rstrip('/')}/api/flow/generate-video", body, timeout=60)
    ops = _extract_operations(res) if isinstance(res, dict) else []
    if not ops and isinstance(res, dict) and isinstance(res.get("operations"), list):
        ops = res["operations"]
    if not ops:
        if isinstance(res, dict) and res.get("error"):
            raise FlowkitError(str(res["error"]))
        raise FlowkitError(f"generate-video returned no operations: {str(res)[:200]}")
    return ops


def _flow_poll_video(
    api_base: str,
    operations: list[dict],
    *,
    poll_interval_s: int,
    timeout_s: int,
) -> dict:
    started = time.time()
    ops = operations
    while True:
        if time.time() - started > timeout_s:
            raise FlowkitError("Timeout waiting Flow operations")
        res = _http_json(
            "POST",
            f"{api_base.rstrip('/')}/api/flow/check-status",
            {"operations": ops},
            timeout=30,
        )
        next_ops = _extract_operations(res) if isinstance(res, dict) else []
        if next_ops:
            ops = next_ops
        statuses = [o.get("status", "") for o in ops if isinstance(o, dict)]
        if statuses and all("SUCCESS" in s for s in statuses):
            return {"operations": ops}
        if any("FAILED" in s for s in statuses):
            return {"operations": ops, "error": "Flow operation failed"}
        time.sleep(max(1, poll_interval_s))


def _extract_video_output(ops_payload: dict) -> tuple[str | None, str | None]:
    ops = ops_payload.get("operations", []) if isinstance(ops_payload, dict) else []
    if not ops:
        return None, None
    meta = ops[0].get("operation", {}).get("metadata", {}).get("video", {}) if isinstance(ops[0], dict) else {}
    media_id = meta.get("mediaId")
    url = meta.get("fifeUrl") or meta.get("servingUri")
    if not media_id and url:
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url, re.I)
        if m:
            media_id = m.group(1)
    return media_id, url


def _cmd_process_video_direct(args: argparse.Namespace) -> int:
    api_base = args.api_base.rstrip("/")
    ensure_health(api_base)

    vid = args.video_id
    video = _http_json("GET", f"{api_base}/api/videos/{vid}", timeout=60)
    pid = video.get("project_id")
    if not pid:
        raise FlowkitError(f"Video not found or missing project_id: {video}")
    orientation = (video.get("orientation") or "VERTICAL").upper()
    prefix = "vertical" if orientation == "VERTICAL" else "horizontal"

    pending = _http_json(
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
        scene = _http_json("GET", f"{api_base}/api/scenes/{sid}", timeout=60)
        start_mid = scene.get(f"{prefix}_image_media_id")
        end_mid = scene.get(f"{prefix}_end_scene_media_id")
        base_prompt = None
        if end_mid and scene.get("transition_prompt"):
            base_prompt = scene.get("transition_prompt")
        base_prompt = base_prompt or scene.get("video_prompt") or scene.get("prompt") or "Cinematic realistic motion."
        if not start_mid:
            _http_json(
                "PATCH",
                f"{api_base}/api/requests/{rid}",
                {"status": "FAILED", "error_message": "Missing scene start image media_id"},
                timeout=60,
            )
            failed.append({"request_id": rid, "scene_id": sid, "error": "missing start_image_media_id"})
            continue

        try:
            print(f"  submit Flow generate-video (start={start_mid[:8]} end={(end_mid[:8] if end_mid else 'none')})", file=sys.stderr, flush=True)
            ops = _flow_generate_video(
                api_base,
                project_id=pid,
                scene_id=sid,
                orientation=orientation,
                start_image_media_id=start_mid,
                end_image_media_id=end_mid,
                prompt=base_prompt,
            )
            print("  polling...", file=sys.stderr, flush=True)
            polled = _flow_poll_video(api_base, ops, poll_interval_s=args.poll_interval, timeout_s=args.timeout)
            if polled.get("error"):
                raise FlowkitError(polled["error"])
            media_id, url = _extract_video_output(polled)
            if media_id and not url:
                media = _http_json("GET", f"{api_base}/api/flow/media/{media_id}", timeout=30)
                if isinstance(media, dict):
                    url = media.get("fifeUrl") or media.get("servingUri")

            _patch_scene(api_base, sid, {
                f"{prefix}_video_media_id": media_id,
                f"{prefix}_video_url": url,
                f"{prefix}_video_status": "COMPLETED" if (media_id or url) else "FAILED",
            })
            _http_json(
                "PATCH",
                f"{api_base}/api/requests/{rid}",
                {"status": "COMPLETED" if (media_id or url) else "FAILED", "media_id": media_id, "output_url": url},
                timeout=60,
            )
            print(f"  done media_id={(media_id[:8] if media_id else 'none')}", file=sys.stderr, flush=True)
            processed.append({"request_id": rid, "scene_id": sid, "media_id": media_id, "url": url})
        except Exception as e:
            _http_json(
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


def _cmd_merge_video(args: argparse.Namespace) -> int:
    api_base = args.api_base.rstrip("/")
    ensure_health(api_base)

    vid = args.video_id
    video = _http_json("GET", f"{api_base}/api/videos/{vid}", timeout=60)
    pid = video.get("project_id")
    if not pid:
        raise FlowkitError(f"Video not found or missing project_id: {video}")
    orientation = (video.get("orientation") or "VERTICAL").upper()
    prefix = "vertical" if orientation == "VERTICAL" else "horizontal"

    out = _http_json("GET", f"{api_base}/api/projects/{pid}/output-dir", timeout=60)
    slug = out.get("slug") or _slugify(video.get("title") or "video")
    rel = out.get("path") or f"output/{slug}"
    out_dir = (BASE_DIR / rel).resolve()
    subclips_dir = out_dir / "subclips"

    scenes = _http_json("GET", f"{api_base}/api/scenes?video_id={vid}", timeout=60)
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
        _download(url, out_path)
        local_paths.append(str(out_path))

    final_path = out_dir / f"{slug}_final.mp4"
    ok = merge_videos(local_paths, str(final_path))
    if not ok:
        raise FlowkitError("Final merge failed (ffmpeg concat)")

    print(json.dumps({
        "video_id": vid,
        "project_id": pid,
        "orientation": orientation,
        "output_dir": str(out_dir),
        "final_video": str(final_path),
        "scenes": len(scenes),
    }, indent=2, ensure_ascii=False))
    return 0


def _build_common_payload(args: argparse.Namespace):
    character = CharacterConfig(
        name=args.character_name,
        description=args.character_description,
        reference_image_url=args.reference_image_url,
        entity_type=args.entity_type,
        image_prompt=args.character_image_prompt,
        voice_description=args.voice_description,
        link_to_project=not args.no_link_character,
    )
    scene = SceneConfig(
        prompt=args.prompt,
        video_prompt=args.video_prompt,
        image_prompt=args.scene_image_prompt,
        transition_prompt=args.transition_prompt,
        display_order=args.display_order,
        chain_type=args.chain_type,
        source=args.scene_source,
        parent_scene_id=args.parent_scene_id,
        character_names=args.character_names or [],
    )
    image_request = RequestConfig(
        request_type=args.image_request_type,
        orientation=args.image_orientation,
        source_media_id=args.image_source_media_id,
    )
    return character, scene, image_request


def _cmd_create_image(args: argparse.Namespace) -> int:
    character, scene, image_request = _build_common_payload(args)
    payload = CreateImageInput(
        api_base=args.api_base.rstrip("/"),
        project_id=args.project_id,
        title=args.title,
        character=character,
        scene=scene,
        image_request=image_request,
        generate_reference=not args.skip_reference,
        poll_interval_s=args.poll_interval,
        timeout_s=args.timeout,
    )
    result = create_image_from_reference(payload)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_create_video(args: argparse.Namespace) -> int:
    character, scene, image_request = _build_common_payload(args)
    payload = CreateVideoInput(
        api_base=args.api_base.rstrip("/"),
        project_id=args.project_id,
        title=args.title,
        character=character,
        scene=scene,
        image_request=image_request,
        generate_reference=not args.skip_reference,
        video_request=RequestConfig(
            request_type=args.video_request_type,
            orientation=args.video_orientation,
            source_media_id=args.video_source_media_id,
        ),
        generate_video=not args.skip_video,
        poll_interval_s=args.poll_interval,
        timeout_s=args.timeout,
    )
    result = create_video_from_reference(payload)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-id", required=True, help="Existing project id")
    parser.add_argument("--title", required=True, help="Video title")
    parser.add_argument("--prompt", required=True, help="Scene prompt")
    parser.add_argument("--video-prompt", default=None, help="Scene video prompt")
    parser.add_argument("--scene-image-prompt", default=None, help="Override image_prompt on scene")
    parser.add_argument("--transition-prompt", default=None, help="Transition prompt for chain scenes")
    parser.add_argument("--display-order", type=int, default=0)
    parser.add_argument("--chain-type", default="ROOT", choices=["ROOT", "CONTINUATION", "INSERT"])
    parser.add_argument("--scene-source", default=None, choices=["root", "user", "system"])
    parser.add_argument("--parent-scene-id", default=None)
    parser.add_argument("--character-names", nargs="*", default=None, help="Extra character slugs/names for scene")

    parser.add_argument("--character-name", required=True, help="Character name")
    parser.add_argument("--character-description", required=True, help="Character description")
    parser.add_argument("--reference-image-url", required=True, help="Reference image URL")
    parser.add_argument("--entity-type", default="character")
    parser.add_argument(
        "--character-image-prompt",
        default="Keep exact character identity from reference image, full body, centered.",
    )
    parser.add_argument("--voice-description", default=None)
    parser.add_argument("--no-link-character", action="store_true", help="Do not link character to project")
    parser.add_argument("--skip-reference", action="store_true", help="Skip GENERATE_CHARACTER_IMAGE step")

    parser.add_argument(
        "--image-request-type",
        default="GENERATE_IMAGE",
        choices=["GENERATE_IMAGE", "REGENERATE_IMAGE", "EDIT_IMAGE"],
    )
    parser.add_argument("--image-orientation", default="VERTICAL", choices=["VERTICAL", "HORIZONTAL"])
    parser.add_argument("--image-source-media-id", default=None, help="Used for EDIT_IMAGE request")

    parser.add_argument("--poll-interval", type=int, default=3, help="Request polling interval (seconds)")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout per request (seconds)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FlowKit CLI")
    parser.add_argument("--api-base", default="http://127.0.0.1:8100", help="FlowKit API base URL")

    sub = parser.add_subparsers(dest="command", required=True)

    create_img = sub.add_parser("create-image", help="Create scene image from reference")
    _add_common_args(create_img)
    create_img.set_defaults(func=_cmd_create_image)

    create_vid = sub.add_parser("create-video", help="Create scene image + video from reference")
    _add_common_args(create_vid)
    create_vid.add_argument(
        "--video-request-type",
        default="GENERATE_VIDEO",
        choices=["GENERATE_VIDEO", "REGENERATE_VIDEO", "GENERATE_VIDEO_REFS", "UPSCALE_VIDEO"],
    )
    create_vid.add_argument("--video-orientation", default="VERTICAL", choices=["VERTICAL", "HORIZONTAL"])
    create_vid.add_argument("--video-source-media-id", default=None, help="Reserved for request parity")
    create_vid.add_argument("--skip-video", action="store_true", help="Only run image path")
    create_vid.set_defaults(func=_cmd_create_video)

    edit_src = sub.add_parser("edit-from-source", help="Cut frames from local source videos and generate an edited video")
    edit_src.add_argument("--source-dir", default="source", help="Directory containing .mp4 source videos")
    edit_src.add_argument("--project-name", default="Excavator Edit", help="New project name (used for output folder slug)")
    edit_src.add_argument("--video-title", default=None, help="Video title inside FlowKit")
    edit_src.add_argument("--project-id", default=None, help="Optional: use existing project_id instead of creating one")
    edit_src.add_argument("--video-id", default=None, help="Optional: use existing video_id instead of creating one")
    edit_src.add_argument("--material", default="realistic", help="Project material ID")
    edit_src.add_argument("--language", default="vi", help="Project language")
    edit_src.add_argument("--orientation", default="AUTO", choices=["AUTO", "VERTICAL", "HORIZONTAL"])
    edit_src.add_argument("--intro-mp4", default=None, help="Optional intro clip (local .mp4) used only for frame extraction")
    edit_src.add_argument("--outro-mp4", default=None, help="Optional outro clip (local .mp4) used only for frame extraction")
    edit_src.add_argument("--poll-interval", type=int, default=3)
    edit_src.add_argument("--timeout", type=int, default=1800)
    edit_src.add_argument("--wait", action="store_true", help="Wait for all videos to finish, then download + merge")
    edit_src.set_defaults(func=_cmd_edit_from_source)

    proc_vid = sub.add_parser("process-video-direct", help="Process pending video requests using /api/flow (bypass worker)")
    proc_vid.add_argument("--video-id", required=True)
    proc_vid.add_argument("--poll-interval", type=int, default=10)
    proc_vid.add_argument("--timeout", type=int, default=1800)
    proc_vid.add_argument("--limit", type=int, default=0, help="Process only first N pending requests (0 = all)")
    proc_vid.set_defaults(func=_cmd_process_video_direct)

    merge_vid = sub.add_parser("merge-video", help="Download all scene videos for a video_id and merge into one mp4")
    merge_vid.add_argument("--video-id", required=True)
    merge_vid.add_argument("--use-upscale", action="store_true", help="Prefer upscale_url over video_url when present")
    merge_vid.set_defaults(func=_cmd_merge_video)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except FlowkitError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
