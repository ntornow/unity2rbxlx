"""
test_cloud_api.py -- Tests for Roblox Cloud API (mocked).
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestCloudAPI:
    def test_upload_image_no_api_key(self, tmp_path):
        from roblox.cloud_api import upload_image
        test_img = tmp_path / "test.png"
        test_img.write_bytes(b"\x89PNG\r\n")
        result = upload_image(test_img, "", "12345", "User", "test")
        assert result is None

    def test_upload_mesh_no_api_key(self, tmp_path):
        from roblox.cloud_api import upload_mesh
        test_mesh = tmp_path / "test.fbx"
        test_mesh.write_bytes(b"fake mesh data")
        result = upload_mesh(test_mesh, "", "12345", "User", "test")
        assert result is None

    @patch("roblox.cloud_api.requests.post")
    def test_upload_image_success(self, mock_post, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "path": "assets/123456789",
            "assetId": "123456789",
        }
        mock_resp.headers = {"x-ratelimit-remaining": "50"}
        mock_post.return_value = mock_resp

        from roblox.cloud_api import upload_image
        test_img = tmp_path / "test.png"
        test_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        result = upload_image(test_img, "test-api-key", "12345", "User", "test")
        assert result is not None

    @patch("roblox.cloud_api.requests.post")
    def test_upload_image_rate_limited(self, mock_post, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"message": "rate limited"}
        mock_resp.headers = {"retry-after": "1"}
        mock_resp.raise_for_status.side_effect = Exception("429")
        mock_post.return_value = mock_resp

        from roblox.cloud_api import upload_image
        test_img = tmp_path / "test.png"
        test_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        result = upload_image(test_img, "test-api-key", "12345", "User", "test")
        # Should handle gracefully (return None on failure)
        assert result is None
