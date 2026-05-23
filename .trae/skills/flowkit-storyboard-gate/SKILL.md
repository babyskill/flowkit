---
name: "flowkit-storyboard-gate"
description: "Storyboard (ảnh trước) + approval gate + model key rẻ. Invoke khi làm video Flowkit cần đồng bộ source hoặc cần tiết kiệm credits."
---

# Flowkit Storyboard Gate (Ảnh Trước, Video Sau)

## Mục tiêu

- Giảm credits bằng cách **tạo ảnh storyboard trước**, user duyệt rồi mới tạo video.
- Đồng bộ bối cảnh từ `source/*.mp4`: storyboard ghi rõ cảnh nào dùng tài nguyên có sẵn (SOURCE), cảnh nào là AI.
- Cho phép chạy video với model rẻ (fast/lite) khi preview/iterate, chỉ dùng high khi final.

## Khi nào dùng skill này

- User muốn làm video bằng Flowkit nhưng **khó kiểm soát quy trình**.
- User muốn **tiết kiệm credits** (tránh render video sớm).
- Dự án cần **đồng bộ source footage** (cắt frame từ mp4, giữ continuity).

## Nguyên tắc bắt buộc (Gate)

- Không generate video trước khi có storyboard trực quan.
- Storyboard phải có:
  - Poster image cho mỗi cảnh (frame từ source hoặc ảnh AI).
  - Nhãn nguồn: `SOURCE` vs `AI_IMAGE`.
  - `media_id` (đã upload lên Flow) cho poster để render video i2v/transition.
- Chỉ render video cho các card đã `approved=true`.

## Quality / Cost Control

- Preview: dùng model key rẻ (fast/lite).
- Final: chỉ nâng tier/model cho các cảnh “đáng tiền” (hook, climax, outro).
- Thực hiện bằng tham số `video_model_key` khi gọi generate video.

## Workflow chuẩn (thực thi)

### 1) Tạo storyboard (images-first)

- Tạo storyboard từ `source/` + (tuỳ chọn) intro/outro:

```bash
python scripts/flowkit_cli.py storyboard-create \
  --source-dir source \
  --project-name "Excavator Storyboard (Images First)" \
  --orientation AUTO \
  --intro-mp4 "/Users/trungkientn/Dev/NodeJS/flowkit/source/scene_0.mp4" \
  --outro-mp4 "/Users/trungkientn/Dev/NodeJS/flowkit/source/scene_7.mp4"
```

- Output:
  - `output/<slug>/storyboard/storyboard.json`
  - `output/<slug>/storyboard/preview.html`

### 2) User approve storyboard

- Approve toàn bộ:

```bash
python scripts/flowkit_cli.py storyboard-approve \
  --storyboard "/ABS/PATH/output/<slug>/storyboard/storyboard.json" \
  --all
```

- Hoặc approve theo label:

```bash
python scripts/flowkit_cli.py storyboard-approve \
  --storyboard "/ABS/PATH/output/<slug>/storyboard/storyboard.json" \
  --labels hook landing best_grab
```

### 3) Render video từ storyboard (có thể chọn model rẻ)

- Render bằng model key rẻ (fast/lite):

```bash
python scripts/flowkit_cli.py storyboard-render \
  --storyboard "/ABS/PATH/output/<slug>/storyboard/storyboard.json" \
  --video-model-key "veo_3_1_i2v_s_fast"
```

- Model keys tham khảo tại:
  - `agent/models.json` → `video_models.*.*.*`

## Gợi ý storyboard “đúng vibe” (source-synced)

- SOURCE cards: chọn đúng khoảnh khắc “kịch tính” (hook/landing/nhát gắp đẹp).
- AI_IMAGE cards: chỉ tạo các insert giúp tò mò nhưng không phá realism:
  - `mystery_scan`: HUD/scan nhẹ, hạn chế neon.
  - `hydraulic_macro`: cận piston/ống, dầu/kim loại rõ texture.
- Transition: ưu tiên “match-cut” + motion blur + dust continuity, không đổi lighting.

## Checklist nhanh (đỡ đốt credits)

- Storyboard preview HTML xem ổn chưa?
- Card nào không cần thiết thì exclude/không approve.
- Chạy fast/lite để kiểm nhịp dựng.
- Chỉ khi ổn mới chạy high cho 2–3 cảnh quan trọng.

## Watermark / Logo (Bottom-right)

### Mục tiêu

- Đặt logo góc phải dưới (bottom-right) để branding nhất quán.
- Mặc định: logo vuông `80x80`, cách lề `10px`.

### Cách dùng (CLI)

- Khi render từ storyboard:

```bash
python scripts/flowkit_cli.py storyboard-render \
  --storyboard "/ABS/PATH/output/<slug>/storyboard/storyboard.json" \
  --video-model-key "veo_3_1_i2v_s_fast" \
  --logo-path "assets/flowkit_logo.png" \
  --logo-w 100 --logo-h 100 --logo-margin 10
```

- Khi merge lại theo video_id:

```bash
python scripts/flowkit_cli.py merge-video \
  --video-id "<VIDEO_ID>" \
  --logo-path "assets/flowkit_logo.png" \
  --logo-w 100 --logo-h 100 --logo-margin 10
```

- Tắt watermark:

```bash
python scripts/flowkit_cli.py merge-video --video-id "<VIDEO_ID>" --no-watermark
```
