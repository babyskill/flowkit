from __future__ import annotations

import json
from pathlib import Path

from agent.services.post_process import merge_videos
from flowkit_client import FlowkitError, wait_request

from .common import (
    create_project,
    create_scene,
    create_video,
    download,
    ensure_server,
    extract_frame,
    get_output_dir,
    http_json,
    patch_scene,
    probe_video,
    submit_request,
    upload_local_image,
)


def cmd_edit_from_source(args) -> int:
    api_base = args.api_base.rstrip("/")
    ensure_server(api_base)

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

    _, _, dur_hook = probe_video(hook_video)
    w2, h2, dur_work = probe_video(work_video)
    dur_intro = probe_video(intro_video)[2] if intro_video else 0.0
    dur_outro = probe_video(outro_video)[2] if outro_video else 0.0

    orientation = args.orientation
    if orientation == "AUTO":
        orientation = "HORIZONTAL" if w2 >= h2 else "VERTICAL"

    project_name = args.project_name
    video_title = args.video_title or project_name
    pid = args.project_id or create_project(api_base, project_name, material=args.material, language=args.language)
    vid = args.video_id or create_video(api_base, pid, video_title, orientation)

    slug, out_dir = get_output_dir(api_base, pid)
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
        extract_frame(Path(item["video"]), float(item["t"]), frame_path)
        frame_mid = upload_local_image(api_base, str(frame_path), project_id=pid, file_name=frame_path.name)

        sid = create_scene(
            api_base,
            vid,
            display_order=len(scene_results),
            prompt=item["prompt"],
            video_prompt=item["video_prompt"],
            chain_type="ROOT",
            source="user",
        )

        prefix = "vertical" if orientation == "VERTICAL" else "horizontal"
        patch_scene(api_base, sid, {
            f"{prefix}_image_media_id": frame_mid,
            f"{prefix}_image_status": "COMPLETED",
        })

        rid_vid = submit_request(
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

        scene_final = http_json("GET", f"{api_base}/api/scenes/{sid}", timeout=60)
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
        sid_t = create_scene(
            api_base,
            vid,
            display_order=len(scene_results) + len(transition_results),
            prompt="Cinematic transition clip between two shots.",
            transition_prompt="Match-cut transition with realistic motion blur and dust. Keep photorealistic style, preserve colors and lighting continuity. No added music.",
            chain_type="ROOT",
            source="user",
        )
        prefix = "vertical" if orientation == "VERTICAL" else "horizontal"
        patch_scene(api_base, sid_t, {
            f"{prefix}_image_media_id": a["image_media_id"],
            f"{prefix}_image_status": "COMPLETED",
            f"{prefix}_image_url": a["image_url"],
            f"{prefix}_end_scene_media_id": b["image_media_id"],
        })

        rid_t = submit_request(
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

        scene_t = http_json("GET", f"{api_base}/api/scenes/{sid_t}", timeout=60)
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
        ordered.append(scene_results[i])
        if i < len(transition_results):
            ordered.append(transition_results[i])

    local_paths: list[str] = []
    for order_idx, item in enumerate(ordered):
        url = item.get("video_url")
        if not url:
            raise FlowkitError(f"Missing video_url for {item}")
        out_path = subclips_dir / f"scene_{order_idx:03d}_{item['label']}.mp4"
        download(url, out_path)
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
