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

    @patch("roblox.cloud_api.time.sleep")
    @patch("roblox.cloud_api.requests.post")
    def test_upload_retries_after_429_rate_limit_reset(
        self, mock_post, mock_sleep, tmp_path,
    ):
        """When the first POST returns 429 with a rate-limit reset header,
        the uploader must wait out the reset window and retry once —
        otherwise the asset gets permanently dropped despite us having
        paid the wait cost. Regression flagged by Codex review.
        """
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": "1",
        }
        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"assetId": "987654321"}
        success.headers = {"x-ratelimit-remaining": "50"}
        mock_post.side_effect = [rate_limited, success]

        from roblox.cloud_api import upload_image
        test_img = tmp_path / "test.png"
        test_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        result = upload_image(test_img, "test-api-key", "12345", "User", "test")
        assert result == "987654321"
        # Two POSTs: the throttled attempt + the retry after sleeping.
        assert mock_post.call_count == 2


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


class TestUploadPlaceContentType:
    """upload_place picks Content-Type from the file extension."""

    @patch("roblox.cloud_api.requests.post")
    def test_xml_content_type_for_rbxlx(self, mock_post, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_post.return_value = mock_resp

        from roblox.cloud_api import upload_place
        rbxlx = tmp_path / "test.rbxlx"
        rbxlx.write_text("<roblox></roblox>")
        assert upload_place(rbxlx, "k", "1", "2") is True

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Content-Type"] == "application/xml"

    @patch("roblox.cloud_api.requests.post")
    def test_octet_stream_for_rbxl(self, mock_post, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_post.return_value = mock_resp

        from roblox.cloud_api import upload_place
        rbxl = tmp_path / "test.rbxl"
        rbxl.write_bytes(b"<roblox!\x89\xff\x0d\x0a\x1a\x0a")
        assert upload_place(rbxl, "k", "1", "2") is True

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Content-Type"] == "application/octet-stream"


class TestPollOperationErrorSurfacing:
    """``_poll_operation`` reads Roblox's async-upload status. When the
    operation completes without an asset ID — Roblox-side failure — we
    must surface ``data.error.code`` and ``data.error.message`` so users
    can distinguish a moderation reject from a transient outage from a
    malformed asset. The previous implementation logged "Upload op done
    but no numeric asset ID: {}" which threw away the only diagnostic
    Roblox returned.
    """

    def test_done_with_error_surfaces_code_and_message(self, caplog):
        import logging
        from roblox.cloud_api import _poll_operation
        with patch("roblox.cloud_api.requests.get") as mock_get, \
             patch("roblox.cloud_api.time.sleep"):
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {
                "path": "operations/abc",
                "operationId": "abc",
                "done": True,
                "error": {"code": "Unknown", "message": "Unknown Error", "details": []},
                "response": {},
            }
            mock_get.return_value = mock_resp
            with caplog.at_level(logging.WARNING, logger="roblox.cloud_api"):
                result = _poll_operation("abc", "fakekey", max_polls=1, poll_interval=0)
            assert result is None
            warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
            joined = "\n".join(warnings)
            assert "code=Unknown" in joined or "Unknown" in joined, (
                f"error.code must be in the log; got: {joined!r}"
            )
            assert "Unknown Error" in joined, (
                f"error.message must be in the log; got: {joined!r}"
            )

    def test_done_without_error_field_logs_missing_asset_id(self, caplog):
        # Roblox responds done=True with no error and no assetId — the
        # warning should still fire so silent drops are visible, but with
        # a synthetic ``MissingAssetId`` code instead of crashing on the
        # missing ``error`` field.
        import logging
        from roblox.cloud_api import _poll_operation
        with patch("roblox.cloud_api.requests.get") as mock_get, \
             patch("roblox.cloud_api.time.sleep"):
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {
                "path": "operations/xyz",
                "done": True,
                "response": {},
            }
            mock_get.return_value = mock_resp
            with caplog.at_level(logging.WARNING, logger="roblox.cloud_api"):
                result = _poll_operation("xyz", "fakekey", max_polls=1, poll_interval=0)
            assert result is None
            assert any("MissingAssetId" in r.message for r in caplog.records), (
                "warning must label a missing-error case so users can grep for it"
            )

    def test_done_with_asset_id_returns_id_no_warning(self, caplog):
        import logging
        from roblox.cloud_api import _poll_operation
        with patch("roblox.cloud_api.requests.get") as mock_get, \
             patch("roblox.cloud_api.time.sleep"):
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {
                "done": True,
                "response": {"assetId": "12345"},
            }
            mock_get.return_value = mock_resp
            with caplog.at_level(logging.WARNING, logger="roblox.cloud_api"):
                result = _poll_operation("op", "fakekey", max_polls=1, poll_interval=0)
            assert result == "12345"
            # Successful path must not log error warnings.
            assert not any(
                "code=" in r.message for r in caplog.records
            ), "successful upload should not emit error-code warnings"
