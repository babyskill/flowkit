#!/usr/bin/env python3
"""Small client helpers for local FlowKit API workflows."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class FlowkitError(RuntimeError):
    """Raised when FlowKit API returns an error."""


def _http_json(method: str, url: str, data: dict[str, Any] | None = None, timeout: int = 120) -> dict[str, Any]:
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


def _download_binary(url: str, out_path: Path, timeout: int = 120) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out_path.write_bytes(resp.read())
            return str(out_path)
    except Exception:
        # Some hosts may fail strict cert chain on local Python install.
        with urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as resp:
            out_path.write_bytes(resp.read())
            return str(out_path)


def _download_outputs(
    *,
    project_id: str,
    video_id: str,
    scene_id: str,
    image_url: str | None,
    video_url: str | None,
) -> dict[str, str | None]:
    root = Path(__file__).resolve().parent.parent / "output" / project_id
    images_dir = root / "images"
    videos_dir = root / "videos"
    downloaded_image = None
    downloaded_video = None

    if image_url:
        parsed = urllib.parse.urlparse(image_url)
        media_id = parsed.path.rstrip("/").split("/")[-1] or scene_id
        downloaded_image = _download_binary(image_url, images_dir / f"{video_id}_{scene_id}_{media_id}.jpg")

    if video_url:
        parsed = urllib.parse.urlparse(video_url)
        media_id = parsed.path.rstrip("/").split("/")[-1] or scene_id
        downloaded_video = _download_binary(video_url, videos_dir / f"{video_id}_{scene_id}_{media_id}.mp4")

    return {
        "downloaded_image_path": downloaded_image,
        "downloaded_video_path": downloaded_video,
        "download_dir": str(root),
    }


@dataclass
class CharacterConfig:
    name: str
    description: str
    reference_image_url: str
    entity_type: str = "character"
    image_prompt: str = "Keep exact character identity from reference image, full body, centered."
    voice_description: str | None = None
    link_to_project: bool = True


@dataclass
class SceneConfig:
    prompt: str
    video_prompt: str | None = None
    image_prompt: str | None = None
    transition_prompt: str | None = None
    display_order: int = 0
    chain_type: str = "ROOT"
    source: str | None = None
    parent_scene_id: str | None = None
    character_names: list[str] = field(default_factory=list)


@dataclass
class RequestConfig:
    request_type: str
    orientation: str = "VERTICAL"
    source_media_id: str | None = None


@dataclass
class CreateImageInput:
    api_base: str
    project_id: str
    title: str
    character: CharacterConfig
    scene: SceneConfig
    image_request: RequestConfig = field(default_factory=lambda: RequestConfig(request_type="GENERATE_IMAGE"))
    generate_reference: bool = True
    poll_interval_s: int = 3
    timeout_s: int = 1800


@dataclass
class CreateVideoInput(CreateImageInput):
    video_request: RequestConfig = field(default_factory=lambda: RequestConfig(request_type="GENERATE_VIDEO"))
    generate_video: bool = True


def wait_request(api_base: str, request_id: str, *, poll_interval_s: int = 3, timeout_s: int = 1800) -> dict[str, Any]:
    started = time.time()
    while True:
        req = _http_json("GET", f"{api_base}/api/requests/{request_id}")
        status = req.get("status")
        if status in ("COMPLETED", "FAILED"):
            return req
        if time.time() - started > timeout_s:
            raise FlowkitError(f"Timeout waiting request {request_id}")
        time.sleep(poll_interval_s)


def ensure_health(api_base: str) -> dict[str, Any]:
    return _http_json("GET", f"{api_base}/health")


def _create_character(api_base: str, project_id: str, cfg: CharacterConfig) -> tuple[str, str]:
    body: dict[str, Any] = {
        "name": cfg.name,
        "entity_type": cfg.entity_type,
        "description": cfg.description,
        "reference_image_url": cfg.reference_image_url,
        "image_prompt": cfg.image_prompt,
    }
    if cfg.voice_description:
        body["voice_description"] = cfg.voice_description
    character = _http_json("POST", f"{api_base}/api/characters", body)
    character_id = character["id"]
    slug = character.get("slug") or cfg.name
    if cfg.link_to_project:
        _http_json("POST", f"{api_base}/api/projects/{project_id}/characters/{character_id}", {})
    return character_id, slug


def _create_video(api_base: str, project_id: str, title: str, orientation: str) -> str:
    video = _http_json(
        "POST",
        f"{api_base}/api/videos",
        {"project_id": project_id, "title": title, "orientation": orientation},
    )
    return video["id"]


def _create_scene(api_base: str, video_id: str, cfg: SceneConfig) -> str:
    body: dict[str, Any] = {
        "video_id": video_id,
        "display_order": cfg.display_order,
        "prompt": cfg.prompt,
        "chain_type": cfg.chain_type,
    }
    if cfg.video_prompt is not None:
        body["video_prompt"] = cfg.video_prompt
    if cfg.image_prompt is not None:
        body["image_prompt"] = cfg.image_prompt
    if cfg.transition_prompt is not None:
        body["transition_prompt"] = cfg.transition_prompt
    if cfg.source is not None:
        body["source"] = cfg.source
    if cfg.parent_scene_id is not None:
        body["parent_scene_id"] = cfg.parent_scene_id
    if cfg.character_names:
        body["character_names"] = cfg.character_names
    scene = _http_json("POST", f"{api_base}/api/scenes", body)
    return scene["id"]


def _submit_request(
    *,
    api_base: str,
    request_type: str,
    project_id: str,
    video_id: str | None = None,
    scene_id: str | None = None,
    character_id: str | None = None,
    orientation: str | None = None,
    source_media_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"type": request_type, "project_id": project_id}
    if video_id is not None:
        body["video_id"] = video_id
    if scene_id is not None:
        body["scene_id"] = scene_id
    if character_id is not None:
        body["character_id"] = character_id
    if orientation is not None:
        body["orientation"] = orientation
    if source_media_id is not None:
        body["source_media_id"] = source_media_id
    return _http_json("POST", f"{api_base}/api/requests", body)


def create_image_from_reference(payload: CreateImageInput) -> dict[str, Any]:
    api_base = payload.api_base.rstrip("/")
    character_id, slug = _create_character(api_base, payload.project_id, payload.character)

    scene_cfg = payload.scene
    merged_names = list(scene_cfg.character_names)
    if slug not in merged_names:
        merged_names.append(slug)
    scene_cfg = SceneConfig(
        prompt=scene_cfg.prompt,
        video_prompt=scene_cfg.video_prompt,
        image_prompt=scene_cfg.image_prompt,
        transition_prompt=scene_cfg.transition_prompt,
        display_order=scene_cfg.display_order,
        chain_type=scene_cfg.chain_type,
        source=scene_cfg.source,
        parent_scene_id=scene_cfg.parent_scene_id,
        character_names=merged_names,
    )

    video_id = _create_video(api_base, payload.project_id, payload.title, payload.image_request.orientation)
    scene_id = _create_scene(api_base, video_id, scene_cfg)

    request_ids: dict[str, str] = {}
    if payload.generate_reference:
        req_ref = _submit_request(
            api_base=api_base,
            request_type="GENERATE_CHARACTER_IMAGE",
            project_id=payload.project_id,
            character_id=character_id,
        )
        request_ids["reference"] = req_ref["id"]
        ref_done = wait_request(
            api_base,
            req_ref["id"],
            poll_interval_s=payload.poll_interval_s,
            timeout_s=payload.timeout_s,
        )
        if ref_done.get("status") != "COMPLETED":
            raise FlowkitError(f"Reference generation failed: {ref_done.get('error_message')}")

    req_img = _submit_request(
        api_base=api_base,
        request_type=payload.image_request.request_type,
        project_id=payload.project_id,
        video_id=video_id,
        scene_id=scene_id,
        orientation=payload.image_request.orientation,
        source_media_id=payload.image_request.source_media_id,
    )
    request_ids["image"] = req_img["id"]
    img_done = wait_request(
        api_base,
        req_img["id"],
        poll_interval_s=payload.poll_interval_s,
        timeout_s=payload.timeout_s,
    )
    if img_done.get("status") != "COMPLETED":
        raise FlowkitError(f"Image generation failed: {img_done.get('error_message')}")

    scene_final = _http_json("GET", f"{api_base}/api/scenes/{scene_id}")
    image_url = scene_final.get("vertical_image_url") or scene_final.get("horizontal_image_url")
    downloads = _download_outputs(
        project_id=payload.project_id,
        video_id=video_id,
        scene_id=scene_id,
        image_url=image_url,
        video_url=None,
    )
    return {
        "project_id": payload.project_id,
        "video_id": video_id,
        "scene_id": scene_id,
        "character_id": character_id,
        "image_url": image_url,
        "downloaded_image_path": downloads["downloaded_image_path"],
        "download_dir": downloads["download_dir"],
        "request_ids": request_ids,
    }


def create_video_from_reference(payload: CreateVideoInput) -> dict[str, Any]:
    image_result = create_image_from_reference(payload)
    if not payload.generate_video:
        image_result["video_url"] = None
        return image_result

    api_base = payload.api_base.rstrip("/")
    req_vid = _submit_request(
        api_base=api_base,
        request_type=payload.video_request.request_type,
        project_id=payload.project_id,
        video_id=image_result["video_id"],
        scene_id=image_result["scene_id"],
        orientation=payload.video_request.orientation,
        source_media_id=payload.video_request.source_media_id,
    )
    image_result.setdefault("request_ids", {})["video"] = req_vid["id"]
    vid_done = wait_request(
        api_base,
        req_vid["id"],
        poll_interval_s=payload.poll_interval_s,
        timeout_s=payload.timeout_s,
    )
    if vid_done.get("status") != "COMPLETED":
        raise FlowkitError(f"Video generation failed: {vid_done.get('error_message')}")

    scene_final = _http_json("GET", f"{api_base}/api/scenes/{image_result['scene_id']}")
    image_result["video_url"] = scene_final.get("vertical_video_url") or scene_final.get("horizontal_video_url")
    image_result["image_url"] = scene_final.get("vertical_image_url") or scene_final.get("horizontal_image_url")
    downloads = _download_outputs(
        project_id=payload.project_id,
        video_id=image_result["video_id"],
        scene_id=image_result["scene_id"],
        image_url=image_result["image_url"],
        video_url=image_result["video_url"],
    )
    image_result["downloaded_image_path"] = downloads["downloaded_image_path"]
    image_result["downloaded_video_path"] = downloads["downloaded_video_path"]
    image_result["download_dir"] = downloads["download_dir"]
    return image_result

