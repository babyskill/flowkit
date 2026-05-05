import sqlite3
import os
import requests
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "flow_agent.db"

def slugify(v):
    import re
    v = v.lower()
    v = re.sub(r'[^a-z0-9]+', '_', v)
    return v.strip('_')

def sync_videos():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get all completed horizontal videos
    query = """
    SELECT 
        s.id as scene_id,
        s.display_order,
        s.horizontal_video_url,
        p.name as project_name
    FROM scene s
    JOIN video v ON s.video_id = v.id
    JOIN project p ON v.project_id = p.id
    WHERE s.horizontal_video_status = 'COMPLETED'
      AND s.horizontal_video_url IS NOT NULL
    """
    cursor.execute(query)
    rows = cursor.fetchall()

    print(f"Found {len(rows)} completed videos to sync.")

    for row in rows:
        slug = slugify(row['project_name'])
        url = row['horizontal_video_url']
        order = row['display_order']
        
        output_dir = BASE_DIR / "output" / slug / "subclips"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        target_path = output_dir / f"scene_{order}.mp4"
        
        if target_path.exists():
            print(f"  [Skip] Scene {order} already exists: {target_path}")
            continue

        print(f"  [Sync] Downloading Scene {order} to {target_path}...")
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                target_path.write_bytes(resp.content)
                print(f"  [Done] Scene {order} saved.")
            else:
                print(f"  [Error] Failed to download Scene {order}: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  [Error] Failed to download Scene {order}: {e}")

    conn.close()

if __name__ == "__main__":
    sync_videos()
