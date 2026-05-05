from __future__ import annotations

import json
import re
import ssl
import subprocess
import time
from pathlib import Path

from flowkit_client import FlowkitError, ensure_health


def slugify(text: str) -> str:
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


def http_json(method: str, url: str, data: dict | None = None, timeout: int = 120) -> dict:
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


def download(url: str, out_path: Path, timeout: int = 120) -> None:
    import urllib.request

    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out_path.write_bytes(resp.read())
            return
    except Exception:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as resp:
            out_path.write_bytes(resp.read())


def run_ffprobe_json(args: list[str]) -> dict:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FlowkitError(f"ffprobe failed: {proc.stderr[-400:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise FlowkitError(f"ffprobe returned non-JSON output: {proc.stdout[:200]}") from e


def probe_video(video_path: Path) -> tuple[int, int, float]:
    info = run_ffprobe_json([
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


def extract_frame(video_path: Path, t: float, out_path: Path) -> str:
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


def upload_local_image(api_base: str, file_path: str, *, project_id: str, file_name: str) -> str:
    res = http_json(
        "POST",
        f"{api_base.rstrip('/')}/api/flow/upload-image",
        {"file_path": file_path, "project_id": project_id, "file_name": file_name},
        timeout=120,
    )
    media_id = res.get("media_id")
    if not media_id:
        raise FlowkitError(f"upload-image returned no media_id: {res}")
    return media_id


def create_project(api_base: str, name: str, *, material: str, language: str) -> str:
    body = {"name": name, "material": material, "language": language}
    proj = http_json("POST", f"{api_base.rstrip('/')}/api/projects", body, timeout=60)
    pid = proj.get("id")
    if not pid:
        raise FlowkitError(f"Create project failed: {proj}")
    return pid


def create_video(api_base: str, project_id: str, title: str, orientation: str) -> str:
    vid = http_json(
        "POST",
        f"{api_base.rstrip('/')}/api/videos",
        {"project_id": project_id, "title": title, "orientation": orientation},
        timeout=60,
    )
    video_id = vid.get("id")
    if not video_id:
        raise FlowkitError(f"Create video failed: {vid}")
    return video_id


def create_scene(api_base: str, video_id: str, *, display_order: int, prompt: str,
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
    res = http_json("POST", f"{api_base.rstrip('/')}/api/scenes", body, timeout=60)
    sid = res.get("id")
    if not sid:
        raise FlowkitError(f"Create scene failed: {res}")
    return sid


def patch_scene(api_base: str, scene_id: str, patch: dict) -> dict:
    return http_json("PATCH", f"{api_base.rstrip('/')}/api/scenes/{scene_id}", patch, timeout=60)


def submit_request(api_base: str, *, request_type: str, project_id: str,
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
    res = http_json("POST", f"{api_base.rstrip('/')}/api/requests", body, timeout=60)
    rid = res.get("id")
    if not rid:
        raise FlowkitError(f"Submit request failed: {res}")
    return rid


def extract_operations(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("operations"), list):
        return payload["operations"]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("operations"), list):
        return data["operations"]
    return []


def extract_uuid_from_url(url: str) -> str | None:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url, re.I)
    return m.group(1) if m else None


def flow_generate_video(
    api_base: str,
    *,
    project_id: str,
    scene_id: str,
    orientation: str,
    start_image_media_id: str,
    prompt: str,
    end_image_media_id: str | None = None,
    user_paygate_tier: str = "PAYGATE_TIER_ONE",
    video_model_key: str | None = None,
) -> list[dict]:
    aspect_ratio = "VIDEO_ASPECT_RATIO_LANDSCAPE" if orientation == "HORIZONTAL" else "VIDEO_ASPECT_RATIO_PORTRAIT"
    body: dict = {
        "start_image_media_id": start_image_media_id,
        "prompt": prompt,
        "project_id": project_id,
        "scene_id": scene_id,
        "aspect_ratio": aspect_ratio,
        "user_paygate_tier": user_paygate_tier,
    }
    if end_image_media_id:
        body["end_image_media_id"] = end_image_media_id
    if video_model_key:
        body["video_model_key"] = video_model_key
    res = http_json("POST", f"{api_base.rstrip('/')}/api/flow/generate-video", body, timeout=60)
    ops = extract_operations(res) if isinstance(res, dict) else []
    if not ops and isinstance(res, dict) and isinstance(res.get("operations"), list):
        ops = res["operations"]
    if not ops:
        if isinstance(res, dict) and res.get("error"):
            raise FlowkitError(str(res["error"]))
        raise FlowkitError(f"generate-video returned no operations: {str(res)[:200]}")
    return ops


def flow_poll_video(api_base: str, operations: list[dict], *, poll_interval_s: int, timeout_s: int) -> dict:
    started = time.time()
    ops = operations
    while True:
        if time.time() - started > timeout_s:
            raise FlowkitError("Timeout waiting Flow operations")
        res = http_json(
            "POST",
            f"{api_base.rstrip('/')}/api/flow/check-status",
            {"operations": ops},
            timeout=30,
        )
        next_ops = extract_operations(res) if isinstance(res, dict) else []
        if next_ops:
            ops = next_ops
        statuses = [o.get("status", "") for o in ops if isinstance(o, dict)]
        if statuses and all("SUCCESS" in s for s in statuses):
            return {"operations": ops}
        if any("FAILED" in s for s in statuses):
            return {"operations": ops, "error": "Flow operation failed"}
        time.sleep(max(1, poll_interval_s))


def extract_video_output(ops_payload: dict) -> tuple[str | None, str | None]:
    ops = ops_payload.get("operations", []) if isinstance(ops_payload, dict) else []
    if not ops:
        return None, None
    meta = ops[0].get("operation", {}).get("metadata", {}).get("video", {}) if isinstance(ops[0], dict) else {}
    media_id = meta.get("mediaId")
    url = meta.get("fifeUrl") or meta.get("servingUri")
    if not media_id and url:
        media_id = extract_uuid_from_url(url)
    return media_id, url


def get_output_dir(api_base: str, project_id: str) -> tuple[str, Path]:
    out = http_json("GET", f"{api_base.rstrip('/')}/api/projects/{project_id}/output-dir", timeout=60)
    slug = out.get("slug") or slugify(project_id)
    rel = out.get("path") or f"output/{slug}"
    root = Path(__file__).resolve().parent.parent.parent
    out_dir = (root / rel).resolve()
    return slug, out_dir


def ensure_server(api_base: str) -> None:
    ensure_health(api_base.rstrip("/"))
