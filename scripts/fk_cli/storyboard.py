from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from agent.services.post_process import add_corner_logo, merge_videos
from flowkit_client import FlowkitError

from .common import (
    create_project,
    create_scene,
    create_video,
    download,
    ensure_server,
    extract_frame,
    extract_uuid_from_url,
    extract_video_output,
    flow_generate_video,
    flow_poll_video,
    get_output_dir,
    http_json,
    patch_scene,
    probe_video,
    slugify,
    upload_local_image,
)


def _extract_image_url(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    media = data.get("media")
    if isinstance(media, list) and media:
        item = media[0]
        if isinstance(item, dict):
            img = item.get("image", {}).get("generatedImage", {})
            if isinstance(img, dict):
                return img.get("fifeUrl") or img.get("imageUri")
    return None


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@dataclass
class StoryboardCard:
    order: int
    label: str
    kind: str
    prompt: str
    source_video: str | None = None
    source_time_s: float | None = None
    poster_path: str | None = None
    poster_media_id: str | None = None
    approved: bool = False


def cmd_storyboard_create(args) -> int:
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

    sb_dir = out_dir / "storyboard"
    images_dir = sb_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    def pick_times(duration_s: float, t1_ratio: float, t2_ratio: float) -> tuple[float, float]:
        a = max(0.5, duration_s * t1_ratio)
        b = max(a + 0.5, duration_s * t2_ratio)
        b = min(duration_s - 0.5, b) if duration_s > 2.0 else min(duration_s * 0.8, b)
        return a, b

    t_hook_a, t_hook_b = pick_times(dur_hook, 0.12, 0.62)
    t_work_a, t_work_b = pick_times(dur_work, 0.18, 0.72)
    t_intro = max(0.5, dur_intro * 0.35) if dur_intro else 0.0
    t_outro = max(0.5, dur_outro * 0.55) if dur_outro else 0.0

    cards: list[StoryboardCard] = []
    order = 0

    def add_source(label: str, video_path: Path, t: float, prompt: str):
        nonlocal order
        cards.append(StoryboardCard(
            order=order,
            label=label,
            kind="SOURCE",
            prompt=prompt,
            source_video=str(video_path),
            source_time_s=float(t),
        ))
        order += 1

    def add_ai(label: str, prompt: str):
        nonlocal order
        cards.append(StoryboardCard(
            order=order,
            label=label,
            kind="AI_IMAGE",
            prompt=prompt,
        ))
        order += 1

    if intro_video:
        add_source(
            "intro",
            intro_video,
            t_intro,
            "Intro: establish the excavator as a powerful machine. Keep photorealistic documentary style.",
        )
    add_source(
        "hook",
        hook_video,
        t_hook_a,
        "Hook: excavator descending from truck. Emphasize tension, weight, realism.",
    )
    add_source(
        "landing",
        hook_video,
        t_hook_b,
        "Landing: tire compression, dust, heavy impact, realistic camera feel.",
    )
    add_ai(
        "mystery_scan",
        "Photorealistic excavator shot with a very subtle technical scan overlay (minimal HUD lines only). Keep the real environment unchanged. No neon. No sci-fi background.",
    )
    add_source(
        "work_start",
        work_video,
        t_work_a,
        "Work start: excavator starts grabbing logs. Crisp textures, natural light.",
    )
    add_ai(
        "hydraulic_macro",
        "Photorealistic macro close-up of hydraulic piston and hoses under load. Oil sheen, metal texture, tiny dust, documentary realism.",
    )
    add_source(
        "best_grab",
        work_video,
        t_work_b,
        "Payoff: the best grab and lift. Force, debris, realistic shadows.",
    )
    if outro_video:
        add_source(
            "outro",
            outro_video,
            t_outro,
            "Outro: satisfying end shot, calm but powerful, documentary realism.",
        )

    for c in cards:
        if c.kind == "SOURCE":
            src = Path(c.source_video) if c.source_video else None
            if not src:
                raise FlowkitError(f"Missing source video for {c.label}")
            poster = images_dir / f"{c.order:03d}_{c.label}_source.jpg"
            extract_frame(src, float(c.source_time_s or 0.0), poster)
            media_id = upload_local_image(api_base, str(poster), project_id=pid, file_name=poster.name)
            c.poster_path = str(poster.relative_to(sb_dir))
            c.poster_media_id = media_id
            continue

        aspect = "IMAGE_ASPECT_RATIO_LANDSCAPE" if orientation == "HORIZONTAL" else "IMAGE_ASPECT_RATIO_PORTRAIT"
        gen = http_json(
            "POST",
            f"{api_base}/api/flow/generate-image",
            {"prompt": c.prompt, "project_id": pid, "aspect_ratio": aspect, "user_paygate_tier": "PAYGATE_TIER_ONE"},
            timeout=120,
        )
        img_url = _extract_image_url(gen)
        if not img_url:
            raise FlowkitError(f"generate-image returned no image URL for {c.label}: {gen}")
        tmp_path = images_dir / f"{c.order:03d}_{c.label}_ai.jpg"
        download(img_url, tmp_path)
        media_id = upload_local_image(api_base, str(tmp_path), project_id=pid, file_name=tmp_path.name)
        c.poster_path = str(tmp_path.relative_to(sb_dir))
        c.poster_media_id = media_id

    storyboard = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project_id": pid,
        "video_id": vid,
        "project_name": project_name,
        "video_title": video_title,
        "slug": slug,
        "orientation": orientation,
        "cards": [
            {
                "order": c.order,
                "label": c.label,
                "kind": c.kind,
                "prompt": c.prompt,
                "source_video": c.source_video,
                "source_time_s": c.source_time_s,
                "poster": {"path": c.poster_path, "media_id": c.poster_media_id},
                "approved": bool(c.approved),
            }
            for c in cards
        ],
    }

    sb_path = sb_dir / "storyboard.json"
    sb_path.write_text(json.dumps(storyboard, indent=2, ensure_ascii=False) + "\n")

    cards_html = []
    for c in storyboard["cards"]:
        img_rel = c["poster"]["path"]
        kind = c["kind"]
        label = c["label"]
        prompt = c["prompt"]
        src = c.get("source_video")
        ts = c.get("source_time_s")
        extra = ""
        if src and ts is not None:
            extra = f"<div class='meta'>SOURCE: {_escape_html(str(Path(src).name))} @ {ts:.2f}s</div>"
        cards_html.append(
            f"""<div class="card">
  <div class="top">
    <div class="badges">
      <span class="badge kind">{_escape_html(kind)}</span>
      <span class="badge label">{_escape_html(label)}</span>
    </div>
    <label class="approve"><input type="checkbox" disabled /> approve</label>
  </div>
  <a class="imgwrap" href="{_escape_html(img_rel)}" target="_blank"><img src="{_escape_html(img_rel)}" /></a>
  {extra}
  <details><summary>Prompt</summary><pre>{_escape_html(prompt)}</pre></details>
</div>"""
        )

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Storyboard Preview — { _escape_html(project_name) }</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#0b0c10;color:#e6e6e6;margin:0}}
    header{{padding:16px 18px;border-bottom:1px solid #222;background:#0f1117;position:sticky;top:0}}
    header h1{{margin:0;font-size:16px}}
    header .sub{{opacity:.8;font-size:12px;margin-top:6px}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;padding:16px}}
    .card{{background:#11131a;border:1px solid #222;border-radius:12px;overflow:hidden}}
    .top{{display:flex;align-items:center;justify-content:space-between;padding:10px 10px 8px}}
    .badges{{display:flex;gap:8px;flex-wrap:wrap}}
    .badge{{font-size:11px;padding:3px 8px;border-radius:999px;border:1px solid #2b2f3a}}
    .badge.kind{{background:#1a2233}}
    .badge.label{{background:#1b1a33}}
    .approve{{font-size:12px;opacity:.7}}
    .imgwrap{{display:block}}
    img{{width:100%;height:auto;display:block}}
    .meta{{padding:8px 10px;font-size:12px;opacity:.75}}
    details{{padding:0 10px 10px}}
    pre{{white-space:pre-wrap;word-break:break-word;background:#0c0e13;border:1px solid #222;border-radius:10px;padding:10px;margin:8px 0 0}}
    .note{{padding:10px 18px;font-size:12px;opacity:.8}}
    code{{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}}
  </style>
</head>
<body>
  <header>
    <h1>Storyboard Preview</h1>
    <div class="sub">
      Project: <code>{_escape_html(project_name)}</code> · Video: <code>{_escape_html(vid)}</code> · Orientation: <code>{_escape_html(orientation)}</code>
    </div>
  </header>
  <div class="note">
    To approve: run <code>python scripts/flowkit_cli.py storyboard-approve --storyboard "{_escape_html(str(sb_path))}" --all</code>
  </div>
  <section class="grid">
    {''.join(cards_html)}
  </section>
</body>
</html>
"""
    (sb_dir / "preview.html").write_text(html, encoding="utf-8")

    print(json.dumps({
        "project_id": pid,
        "video_id": vid,
        "slug": slug,
        "orientation": orientation,
        "storyboard": str(sb_path),
        "preview_html": str((sb_dir / "preview.html")),
        "cards": len(cards),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_storyboard_approve(args) -> int:
    sb_path = Path(args.storyboard).resolve()
    if not sb_path.exists():
        raise FlowkitError(f"Storyboard not found: {sb_path}")
    data = json.loads(sb_path.read_text(encoding="utf-8"))
    cards = data.get("cards")
    if not isinstance(cards, list):
        raise FlowkitError("Invalid storyboard.json (cards missing)")

    labels = set(args.labels or [])
    updated = 0
    for c in cards:
        if not isinstance(c, dict):
            continue
        if args.all:
            c["approved"] = True
            updated += 1
        elif labels and c.get("label") in labels:
            c["approved"] = True
            updated += 1

    sb_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"storyboard": str(sb_path), "approved": updated}, indent=2, ensure_ascii=False))
    return 0


def cmd_storyboard_render(args) -> int:
    api_base = args.api_base.rstrip("/")
    ensure_server(api_base)

    sb_path = Path(args.storyboard).resolve()
    if not sb_path.exists():
        raise FlowkitError(f"Storyboard not found: {sb_path}")

    data = json.loads(sb_path.read_text(encoding="utf-8"))
    pid = data.get("project_id")
    vid = data.get("video_id")
    orientation = (data.get("orientation") or "VERTICAL").upper()
    if not pid or not vid:
        raise FlowkitError("Storyboard missing project_id/video_id")

    cards = data.get("cards")
    if not isinstance(cards, list):
        raise FlowkitError("Invalid storyboard.json (cards missing)")

    approved = [c for c in cards if isinstance(c, dict) and c.get("approved") is True]
    approved.sort(key=lambda c: int(c.get("order") or 0))
    if not approved:
        raise FlowkitError("No approved cards. Run storyboard-approve first.")

    slug, out_dir = get_output_dir(api_base, pid)
    subclips_dir = out_dir / "subclips"
    subclips_dir.mkdir(parents=True, exist_ok=True)

    prefix = "vertical" if orientation == "VERTICAL" else "horizontal"

    shots = []
    for c in approved:
        poster = c.get("poster") or {}
        mid = poster.get("media_id")
        if not mid:
            raise FlowkitError(f"Card missing poster media_id: {c.get('label')}")
        shots.append({"label": c.get("label") or f"shot_{c.get('order')}", "media_id": mid, "prompt": c.get("prompt") or ""})

    rendered = []

    for idx, s in enumerate(shots):
        sid = create_scene(
            api_base,
            vid,
            display_order=idx * 2,
            prompt=s["prompt"] or "Photorealistic documentary shot.",
            video_prompt="",
            chain_type="ROOT",
            source="user",
        )
        patch_scene(api_base, sid, {
            f"{prefix}_image_media_id": s["media_id"],
            f"{prefix}_image_status": "COMPLETED",
        })

        ops = flow_generate_video(
            api_base,
            project_id=pid,
            scene_id=sid,
            orientation=orientation,
            start_image_media_id=s["media_id"],
            prompt=s["prompt"] or "Photorealistic documentary shot.",
            video_model_key=args.video_model_key,
        )
        polled = flow_poll_video(api_base, ops, poll_interval_s=args.poll_interval, timeout_s=args.timeout)
        if polled.get("error"):
            raise FlowkitError(polled["error"])
        media_id, url = extract_video_output(polled)
        if not url and media_id:
            media = http_json("GET", f"{api_base}/api/flow/media/{media_id}", timeout=30)
            if isinstance(media, dict):
                url = media.get("fifeUrl") or media.get("servingUri")
        patch_scene(api_base, sid, {
            f"{prefix}_video_media_id": media_id or extract_uuid_from_url(url or "") or None,
            f"{prefix}_video_url": url,
            f"{prefix}_video_status": "COMPLETED" if url else "FAILED",
        })

        if not url:
            raise FlowkitError(f"Video URL missing after render: {s['label']}")
        out_path = subclips_dir / f"scene_{idx*2:03d}_{slugify(s['label'])}.mp4"
        download(url, out_path)
        rendered.append({"kind": "shot", "label": s["label"], "scene_id": sid, "video": str(out_path)})

        if idx >= len(shots) - 1:
            continue

        nxt = shots[idx + 1]
        sid_t = create_scene(
            api_base,
            vid,
            display_order=idx * 2 + 1,
            prompt="Cinematic transition clip between two shots.",
            transition_prompt=args.transition_prompt,
            chain_type="ROOT",
            source="user",
        )
        patch_scene(api_base, sid_t, {
            f"{prefix}_image_media_id": s["media_id"],
            f"{prefix}_image_status": "COMPLETED",
            f"{prefix}_end_scene_media_id": nxt["media_id"],
        })

        ops_t = flow_generate_video(
            api_base,
            project_id=pid,
            scene_id=sid_t,
            orientation=orientation,
            start_image_media_id=s["media_id"],
            end_image_media_id=nxt["media_id"],
            prompt=args.transition_prompt,
            video_model_key=args.video_model_key,
        )
        polled_t = flow_poll_video(api_base, ops_t, poll_interval_s=args.poll_interval, timeout_s=args.timeout)
        if polled_t.get("error"):
            raise FlowkitError(polled_t["error"])
        media_id_t, url_t = extract_video_output(polled_t)
        if not url_t and media_id_t:
            media = http_json("GET", f"{api_base}/api/flow/media/{media_id_t}", timeout=30)
            if isinstance(media, dict):
                url_t = media.get("fifeUrl") or media.get("servingUri")
        patch_scene(api_base, sid_t, {
            f"{prefix}_video_media_id": media_id_t or extract_uuid_from_url(url_t or "") or None,
            f"{prefix}_video_url": url_t,
            f"{prefix}_video_status": "COMPLETED" if url_t else "FAILED",
        })
        if not url_t:
            raise FlowkitError(f"Transition URL missing after render: {s['label']} -> {nxt['label']}")
        out_path_t = subclips_dir / f"scene_{idx*2+1:03d}_transition.mp4"
        download(url_t, out_path_t)
        rendered.append({"kind": "transition", "label": f"{s['label']} -> {nxt['label']}", "scene_id": sid_t, "video": str(out_path_t)})

    final_path = out_dir / f"{slug}_final.mp4"
    ok = merge_videos([x["video"] for x in rendered], str(final_path))
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
        "project_id": pid,
        "video_id": vid,
        "slug": slug,
        "orientation": orientation,
        "final_video": str(final_path),
        "clips": len(rendered),
    }, indent=2, ensure_ascii=False))
    return 0
