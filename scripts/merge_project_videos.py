import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from agent.services.post_process import merge_videos

def main():
    slug = "unstoppable_rock_breaker"
    subclips_dir = BASE_DIR / "output" / slug / "subclips"
    video_paths = sorted([str(p) for p in subclips_dir.glob("scene_*.mp4")], 
                         key=lambda x: int(Path(x).stem.split('_')[1]))
    
    if not video_paths:
        print("No subclips found!")
        return

    output_path = BASE_DIR / "output" / slug / "final_video.mp4"
    print(f"Merging {len(video_paths)} videos into {output_path}...")
    
    success = merge_videos(video_paths, str(output_path))
    if success:
        print(f"Successfully merged! Video saved at: {output_path}")
    else:
        print("Failed to merge videos.")

if __name__ == "__main__":
    main()
