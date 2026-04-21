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


class TestProbeAssetAvailability:
    """`probe_asset_availability` classifies an uploaded asset as
    approved / rejected / unknown by hitting the assets metadata endpoint.
    The intent is that a rejection (e.g. music moderation) can be caught
    before the broken ID lands in the rbxlx — but inconclusive responses
    should fail soft to "unknown" so the probe never regresses a working
    upload.
    """

    @patch("roblox.cloud_api.requests.get")
    def test_http_403_is_rejected(self, mock_get):
        from roblox.cloud_api import probe_asset_availability
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_get.return_value = mock_resp
        assert probe_asset_availability("12345", "k") == "rejected"

    @patch("roblox.cloud_api.requests.get")
    def test_http_404_is_rejected(self, mock_get):
        from roblox.cloud_api import probe_asset_availability
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp
        assert probe_asset_availability("12345", "k") == "rejected"

    @patch("roblox.cloud_api.requests.get")
    def test_moderation_rejected_payload(self, mock_get):
        from roblox.cloud_api import probe_asset_availability
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "moderationResult": {"moderationState": "Rejected"}
        }
        mock_get.return_value = mock_resp
        assert probe_asset_availability("12345", "k") == "rejected"

    @patch("roblox.cloud_api.requests.get")
    def test_approved_payload(self, mock_get):
        from roblox.cloud_api import probe_asset_availability
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "moderationResult": {"moderationState": "Approved"}
        }
        mock_get.return_value = mock_resp
        assert probe_asset_availability("12345", "k") == "approved"

    @patch("roblox.cloud_api.requests.get")
    def test_no_moderation_field_is_approved(self, mock_get):
        from roblox.cloud_api import probe_asset_availability
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "12345"}
        mock_get.return_value = mock_resp
        assert probe_asset_availability("12345", "k") == "approved"

    @patch("roblox.cloud_api.requests.get")
    def test_network_error_is_unknown(self, mock_get):
        import requests as _requests
        from roblox.cloud_api import probe_asset_availability
        mock_get.side_effect = _requests.RequestException("boom")
        assert probe_asset_availability("12345", "k") == "unknown"

    @patch("roblox.cloud_api.requests.get")
    def test_pending_moderation_is_unknown(self, mock_get):
        from roblox.cloud_api import probe_asset_availability
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "moderationResult": {"moderationState": "Pending"}
        }
        mock_get.return_value = mock_resp
        assert probe_asset_availability("12345", "k") == "unknown"

    def test_non_numeric_id_is_unknown(self):
        from roblox.cloud_api import probe_asset_availability
        assert probe_asset_availability("", "k") == "unknown"
        assert probe_asset_availability("dc5c29d6-34b4-46c8", "k") == "unknown"
        assert probe_asset_availability("not-a-number", "k") == "unknown"
        assert probe_asset_availability("/path/to/file.fbx", "k") == "unknown"

    @patch("roblox.cloud_api.requests.get")
    def test_rbxassetid_prefix_stripped(self, mock_get):
        from roblox.cloud_api import probe_asset_availability
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "12345"}
        mock_get.return_value = mock_resp
        assert probe_asset_availability("rbxassetid://12345", "k") == "approved"
        assert "12345" in mock_get.call_args[0][0]
