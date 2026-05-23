#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
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
)

from scripts.fk_cli.direct_video import cmd_merge_video, cmd_process_video_direct
from scripts.fk_cli.edit_from_source import cmd_edit_from_source
from scripts.fk_cli.storyboard import cmd_storyboard_approve, cmd_storyboard_create, cmd_storyboard_render


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
    edit_src.add_argument("--logo-path", default="assets/flowkit_logo.png")
    edit_src.add_argument("--logo-w", type=int, default=100)
    edit_src.add_argument("--logo-h", type=int, default=100)
    edit_src.add_argument("--logo-margin", type=int, default=10)
    edit_src.add_argument("--no-watermark", action="store_true")
    edit_src.set_defaults(func=cmd_edit_from_source)

    proc_vid = sub.add_parser("process-video-direct", help="Process pending video requests using /api/flow (bypass worker)")
    proc_vid.add_argument("--video-id", required=True)
    proc_vid.add_argument("--poll-interval", type=int, default=10)
    proc_vid.add_argument("--timeout", type=int, default=1800)
    proc_vid.add_argument("--limit", type=int, default=0, help="Process only first N pending requests (0 = all)")
    proc_vid.add_argument("--video-model-key", default=None, help="Optional: override Flow videoModelKey (fast/lite/high)")
    proc_vid.set_defaults(func=cmd_process_video_direct)

    merge_vid = sub.add_parser("merge-video", help="Download all scene videos for a video_id and merge into one mp4")
    merge_vid.add_argument("--video-id", required=True)
    merge_vid.add_argument("--use-upscale", action="store_true", help="Prefer upscale_url over video_url when present")
    merge_vid.add_argument("--logo-path", default="assets/flowkit_logo.png")
    merge_vid.add_argument("--logo-w", type=int, default=100)
    merge_vid.add_argument("--logo-h", type=int, default=100)
    merge_vid.add_argument("--logo-margin", type=int, default=10)
    merge_vid.add_argument("--no-watermark", action="store_true")
    merge_vid.set_defaults(func=cmd_merge_video)

    sb_create = sub.add_parser("storyboard-create", help="Create storyboard (images first) from source videos")
    sb_create.add_argument("--source-dir", default="source", help="Directory containing .mp4 source videos")
    sb_create.add_argument("--project-name", default="Storyboard Project", help="Project name")
    sb_create.add_argument("--video-title", default=None, help="Video title inside FlowKit")
    sb_create.add_argument("--project-id", default=None, help="Optional: use existing project_id instead of creating one")
    sb_create.add_argument("--video-id", default=None, help="Optional: use existing video_id instead of creating one")
    sb_create.add_argument("--material", default="realistic", help="Project material ID")
    sb_create.add_argument("--language", default="vi", help="Project language")
    sb_create.add_argument("--orientation", default="AUTO", choices=["AUTO", "VERTICAL", "HORIZONTAL"])
    sb_create.add_argument("--intro-mp4", default=None, help="Optional intro clip (local .mp4)")
    sb_create.add_argument("--outro-mp4", default=None, help="Optional outro clip (local .mp4)")
    sb_create.set_defaults(func=cmd_storyboard_create)

    sb_approve = sub.add_parser("storyboard-approve", help="Approve storyboard cards before rendering videos")
    sb_approve.add_argument("--storyboard", required=True, help="Path to storyboard.json")
    sb_approve.add_argument("--all", action="store_true", help="Approve all cards")
    sb_approve.add_argument("--labels", nargs="*", default=None, help="Approve only these labels")
    sb_approve.set_defaults(func=cmd_storyboard_approve)

    sb_render = sub.add_parser("storyboard-render", help="Render videos from an approved storyboard")
    sb_render.add_argument("--storyboard", required=True, help="Path to storyboard.json")
    sb_render.add_argument("--video-model-key", default=None, help="Optional: override Flow videoModelKey (fast/lite/high)")
    sb_render.add_argument("--logo-path", default="assets/flowkit_logo.png")
    sb_render.add_argument("--logo-w", type=int, default=100)
    sb_render.add_argument("--logo-h", type=int, default=100)
    sb_render.add_argument("--logo-margin", type=int, default=10)
    sb_render.add_argument("--no-watermark", action="store_true")
    sb_render.add_argument("--poll-interval", type=int, default=10)
    sb_render.add_argument("--timeout", type=int, default=1800)
    sb_render.add_argument(
        "--transition-prompt",
        default="Match-cut transition with realistic motion blur and dust. Keep photorealistic style, preserve colors and lighting continuity. No added music.",
    )
    sb_render.set_defaults(func=cmd_storyboard_render)

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
