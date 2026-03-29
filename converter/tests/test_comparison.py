"""
test_comparison.py -- Tests for the comparison system.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestStateDumper:
    def test_dump_unity_state(self, simple_scene_yaml):
        from unity.scene_parser import parse_scene
        from comparison.state_dumper import dump_unity_state

        scene = parse_scene(Path(__file__).parent / "fixtures" / "simple_scene.yaml")
        state = dump_unity_state(scene)
        assert len(state) > 0
        # Should have entries for each node
        assert any("Cube" in key for key in state)
        assert any("MainCamera" in key for key in state)

    def test_roblox_state_luau(self):
        from comparison.state_dumper import dump_roblox_state_luau
        luau = dump_roblox_state_luau()
        assert "GetDescendants" in luau or "workspace" in luau


class TestStateDiff:
    def test_identical_states(self):
        from comparison.state_diff import diff_states
        state = {
            "Cube": {"position": [1, 2, 3], "size": [1, 1, 1]},
            "Sphere": {"position": [4, 5, 6], "size": [2, 2, 2]},
        }
        result = diff_states(state, state)
        assert result.matched_objects == 2
        assert result.mean_position_error == 0.0

    def test_missing_objects(self):
        from comparison.state_diff import diff_states
        unity = {"A": {"position": [0, 0, 0], "size": [1, 1, 1]}}
        roblox = {"B": {"position": [0, 0, 0], "size": [1, 1, 1]}}
        result = diff_states(unity, roblox)
        assert result.matched_objects == 0
        assert "A" in result.unmatched_unity
        assert "B" in result.unmatched_roblox

    def test_position_diff(self):
        from comparison.state_diff import diff_states
        unity = {"Obj": {"position": [0, 0, 0], "size": [1, 1, 1]}}
        roblox = {"Obj": {"position": [1, 0, 0], "size": [1, 1, 1]}}
        result = diff_states(unity, roblox)
        assert result.matched_objects == 1
        assert result.mean_position_error > 0


class TestVisualDiff:
    """Tests for the pure-numpy SSIM and visual diff utilities."""

    def test_identical_images_ssim_is_one(self, tmp_path):
        """SSIM of an image compared to itself should be ~1.0."""
        from PIL import Image
        import numpy as np
        from comparison.visual_diff import compute_ssim

        # Create a non-trivial test image (gradient + noise)
        rng = np.random.RandomState(42)
        arr = np.zeros((100, 100), dtype=np.uint8)
        for y in range(100):
            arr[y, :] = int(y * 2.55)
        arr = np.clip(arr.astype(np.int16) + rng.randint(-10, 10, arr.shape), 0, 255).astype(np.uint8)

        img = Image.fromarray(arr, mode="L")
        path_a = tmp_path / "identical_a.png"
        path_b = tmp_path / "identical_b.png"
        img.save(path_a)
        img.save(path_b)

        score = compute_ssim(path_a, path_b)
        assert score > 0.99, f"Identical images should have SSIM ~1.0, got {score}"

    def test_different_images_low_ssim(self, tmp_path):
        """SSIM of very different images should be low."""
        from PIL import Image
        import numpy as np
        from comparison.visual_diff import compute_ssim

        white = Image.fromarray(np.full((100, 100), 255, dtype=np.uint8), mode="L")
        black = Image.fromarray(np.zeros((100, 100), dtype=np.uint8), mode="L")
        path_a = tmp_path / "white.png"
        path_b = tmp_path / "black.png"
        white.save(path_a)
        black.save(path_b)

        score = compute_ssim(path_a, path_b)
        assert score < 0.1, f"Black vs white should have very low SSIM, got {score}"

    def test_similar_images_moderate_ssim(self, tmp_path):
        """Slightly noisy version of an image should have moderate-to-high SSIM."""
        from PIL import Image
        import numpy as np
        from comparison.visual_diff import compute_ssim

        rng = np.random.RandomState(42)
        base = rng.randint(0, 256, (100, 100), dtype=np.uint8)
        noisy = np.clip(base.astype(np.int16) + rng.randint(-30, 30, base.shape), 0, 255).astype(np.uint8)

        path_a = tmp_path / "base.png"
        path_b = tmp_path / "noisy.png"
        Image.fromarray(base, mode="L").save(path_a)
        Image.fromarray(noisy, mode="L").save(path_b)

        score = compute_ssim(path_a, path_b)
        assert 0.3 < score < 0.99, f"Noisy images should have moderate SSIM, got {score}"

    def test_ssim_rgb(self, tmp_path):
        """RGB SSIM should work and return a value in [0, 1]."""
        from PIL import Image
        import numpy as np
        from comparison.visual_diff import compute_ssim_rgb

        rng = np.random.RandomState(42)
        base = rng.randint(0, 256, (80, 80, 3), dtype=np.uint8)
        noisy = np.clip(base.astype(np.int16) + rng.randint(-20, 20, base.shape), 0, 255).astype(np.uint8)

        path_a = tmp_path / "rgb_a.png"
        path_b = tmp_path / "rgb_b.png"
        Image.fromarray(base).save(path_a)
        Image.fromarray(noisy).save(path_b)

        score = compute_ssim_rgb(path_a, path_b)
        assert 0.0 <= score <= 1.0

    def test_diff_heatmap_created(self, tmp_path):
        """generate_diff_heatmap should produce a PNG file."""
        from PIL import Image
        import numpy as np
        from comparison.visual_diff import generate_diff_heatmap

        rng = np.random.RandomState(42)
        a = rng.randint(0, 256, (50, 50, 3), dtype=np.uint8)
        b = rng.randint(0, 256, (50, 50, 3), dtype=np.uint8)

        pa = tmp_path / "a.png"
        pb = tmp_path / "b.png"
        Image.fromarray(a).save(pa)
        Image.fromarray(b).save(pb)

        heatmap = generate_diff_heatmap(pa, pb, tmp_path / "heatmap.png")
        assert heatmap.exists()
        assert Image.open(heatmap).size == (50, 50)

    def test_pixel_diff(self, tmp_path):
        """compute_pixel_diff should return reasonable percentage."""
        from PIL import Image
        import numpy as np
        from comparison.visual_diff import compute_pixel_diff

        white = np.full((50, 50, 3), 255, dtype=np.uint8)
        black = np.zeros((50, 50, 3), dtype=np.uint8)
        pa = tmp_path / "w.png"
        pb = tmp_path / "b.png"
        Image.fromarray(white).save(pa)
        Image.fromarray(black).save(pb)

        pct, heatmap = compute_pixel_diff(pa, pb)
        assert pct > 99.0  # Should be ~100% different
        assert heatmap.exists()

    def test_compare_images_full(self, tmp_path):
        """compare_images should return a dict with all expected keys."""
        from PIL import Image
        import numpy as np
        from comparison.visual_diff import compare_images

        rng = np.random.RandomState(42)
        a = rng.randint(0, 256, (80, 80, 3), dtype=np.uint8)
        b = np.clip(a.astype(np.int16) + 30, 0, 255).astype(np.uint8)

        pa = tmp_path / "a.png"
        pb = tmp_path / "b.png"
        Image.fromarray(a).save(pa)
        Image.fromarray(b).save(pb)

        result = compare_images(pa, pb, tmp_path / "out")
        assert "ssim" in result
        assert "ssim_rgb" in result
        assert "pixel_diff_pct" in result
        assert "heatmap_path" in result
        assert "quality_label" in result
        assert 0.0 <= result["ssim"] <= 1.0

    def test_crop_viewport(self, tmp_path):
        """crop_viewport should reduce image dimensions."""
        from PIL import Image
        import numpy as np
        from comparison.visual_diff import crop_viewport

        img = Image.fromarray(np.zeros((200, 300, 3), dtype=np.uint8))
        path = tmp_path / "full.png"
        img.save(path)

        cropped = crop_viewport(path, margin_pct=0.1)
        cropped_img = Image.open(cropped)
        assert cropped_img.size[0] < 300
        assert cropped_img.size[1] < 200

    def test_different_size_images(self, tmp_path):
        """SSIM should handle images of different sizes by resizing."""
        from PIL import Image
        import numpy as np
        from comparison.visual_diff import compute_ssim

        rng = np.random.RandomState(42)
        a = rng.randint(0, 256, (100, 100), dtype=np.uint8)
        b = rng.randint(0, 256, (80, 120), dtype=np.uint8)

        pa = tmp_path / "a.png"
        pb = tmp_path / "b.png"
        Image.fromarray(a, mode="L").save(pa)
        Image.fromarray(b, mode="L").save(pb)

        score = compute_ssim(pa, pb)
        assert 0.0 <= score <= 1.0  # Should not crash


class TestScreenshotCapture:
    """Tests for screenshot_capture helpers."""

    def test_unity_camera_to_roblox(self):
        from comparison.screenshot_capture import unity_camera_to_roblox

        result = unity_camera_to_roblox(
            position=(10.0, 5.0, -3.0),
            rotation_euler=(30.0, 45.0, 0.0),
            fov=60.0,
        )
        # Z should be negated
        assert result["position"] == (10.0, 5.0, 3.0)
        # X and Y euler negated, Z kept
        assert result["rotation"] == (-30.0, -45.0, 0.0)
        assert result["fov"] == 60.0

    def test_generate_roblox_camera_luau(self):
        from comparison.screenshot_capture import generate_roblox_camera_luau

        luau = generate_roblox_camera_luau(
            position=(10.0, 5.0, 3.0),
            rotation_euler=(-30.0, -45.0, 0.0),
            fov=60.0,
        )
        assert "CurrentCamera" in luau
        assert "CameraType" in luau
        assert "FieldOfView" in luau
        assert "60" in luau

    def test_capture_roblox_screenshot_writes_script(self, tmp_path):
        from comparison.screenshot_capture import capture_roblox_screenshot

        out = capture_roblox_screenshot(
            output_path=tmp_path / "roblox.png",
            camera_position=(1.0, 2.0, 3.0),
            camera_rotation=(10.0, 20.0, 0.0),
            fov=70.0,
        )
        assert out == tmp_path / "roblox.png"
        script = tmp_path / "position_camera.luau"
        assert script.exists()
        content = script.read_text()
        assert "CurrentCamera" in content


class TestReport:
    def test_generate_report(self, tmp_path):
        from comparison.state_diff import StateDiffResult
        from comparison.report import generate_report

        diff = StateDiffResult(
            matched_objects=10,
            unmatched_unity=["MissingA"],
            unmatched_roblox=["ExtraB"],
            position_diffs={"Cube": 0.5},
            rotation_diffs={},
            size_diffs={},
            mean_position_error=0.5,
            mean_rotation_error=0.0,
        )
        report_path = generate_report(
            visual_score=0.85,
            state_diff=diff,
            screenshots={},
            heatmap_path=None,
            output_dir=tmp_path,
        )
        assert report_path.exists()
        content = report_path.read_text()
        assert "0.85" in content
        assert "10" in content
