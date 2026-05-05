#!/usr/bin/env python3
"""MCP server exposing a simple FlowKit create-video tool."""

from __future__ import annotations

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

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - runtime guard
    raise SystemExit(
        "Missing dependency 'mcp'. Install with: pip install mcp"
    ) from exc


mcp = FastMCP("flowkit-local")


@mcp.tool()
def create_image(
    project_id: str,
    title: str,
    prompt: str,
    character_name: str,
    character_description: str,
    reference_image_url: str,
    api_base: str = "http://127.0.0.1:8100",
    video_prompt: str | None = None,
    scene_image_prompt: str | None = None,
    transition_prompt: str | None = None,
    display_order: int = 0,
    chain_type: str = "ROOT",
    scene_source: str | None = None,
    parent_scene_id: str | None = None,
    character_names: list[str] | None = None,
    entity_type: str = "character",
    character_image_prompt: str = "Keep exact character identity from reference image, full body, centered.",
    voice_description: str | None = None,
    link_to_project: bool = True,
    generate_reference: bool = True,
    image_request_type: str = "GENERATE_IMAGE",
    image_orientation: str = "VERTICAL",
    image_source_media_id: str | None = None,
    poll_interval_s: int = 3,
    timeout_s: int = 1800,
) -> dict:
    """Create one scene image from reference with full scene/request params."""
    payload = CreateImageInput(
        api_base=api_base.rstrip("/"),
        project_id=project_id,
        title=title,
        character=CharacterConfig(
            name=character_name,
            description=character_description,
            reference_image_url=reference_image_url,
            entity_type=entity_type,
            image_prompt=character_image_prompt,
            voice_description=voice_description,
            link_to_project=link_to_project,
        ),
        scene=SceneConfig(
            prompt=prompt,
            video_prompt=video_prompt,
            image_prompt=scene_image_prompt,
            transition_prompt=transition_prompt,
            display_order=display_order,
            chain_type=chain_type,
            source=scene_source,
            parent_scene_id=parent_scene_id,
            character_names=character_names or [],
        ),
        image_request=RequestConfig(
            request_type=image_request_type,
            orientation=image_orientation,
            source_media_id=image_source_media_id,
        ),
        generate_reference=generate_reference,
        poll_interval_s=poll_interval_s,
        timeout_s=timeout_s,
    )
    try:
        return create_image_from_reference(payload)
    except FlowkitError as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def create_video(
    project_id: str,
    title: str,
    prompt: str,
    character_name: str,
    character_description: str,
    reference_image_url: str,
    api_base: str = "http://127.0.0.1:8100",
    video_prompt: str | None = None,
    scene_image_prompt: str | None = None,
    transition_prompt: str | None = None,
    display_order: int = 0,
    chain_type: str = "ROOT",
    scene_source: str | None = None,
    parent_scene_id: str | None = None,
    character_names: list[str] | None = None,
    entity_type: str = "character",
    character_image_prompt: str = "Keep exact character identity from reference image, full body, centered.",
    voice_description: str | None = None,
    link_to_project: bool = True,
    generate_reference: bool = True,
    image_request_type: str = "GENERATE_IMAGE",
    image_orientation: str = "VERTICAL",
    image_source_media_id: str | None = None,
    video_request_type: str = "GENERATE_VIDEO",
    video_orientation: str = "VERTICAL",
    video_source_media_id: str | None = None,
    generate_video: bool = True,
    poll_interval_s: int = 3,
    timeout_s: int = 1800,
) -> dict:
    """Create one video from reference with full scene/request params."""
    payload = CreateVideoInput(
        api_base=api_base.rstrip("/"),
        project_id=project_id,
        title=title,
        character=CharacterConfig(
            name=character_name,
            description=character_description,
            reference_image_url=reference_image_url,
            entity_type=entity_type,
            image_prompt=character_image_prompt,
            voice_description=voice_description,
            link_to_project=link_to_project,
        ),
        scene=SceneConfig(
            prompt=prompt,
            video_prompt=video_prompt,
            image_prompt=scene_image_prompt,
            transition_prompt=transition_prompt,
            display_order=display_order,
            chain_type=chain_type,
            source=scene_source,
            parent_scene_id=parent_scene_id,
            character_names=character_names or [],
        ),
        image_request=RequestConfig(
            request_type=image_request_type,
            orientation=image_orientation,
            source_media_id=image_source_media_id,
        ),
        video_request=RequestConfig(
            request_type=video_request_type,
            orientation=video_orientation,
            source_media_id=video_source_media_id,
        ),
        generate_reference=generate_reference,
        generate_video=generate_video,
        poll_interval_s=poll_interval_s,
        timeout_s=timeout_s,
    )
    try:
        return create_video_from_reference(payload)
    except FlowkitError as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run()

