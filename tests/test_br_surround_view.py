"""Tests for the Brazilian Hyundai surround-view capture contract."""

import base64
from unittest.mock import MagicMock, patch

import pytest
import requests

from hyundai_kia_connect_api.const import BRAND_HYUNDAI, BRANDS, REGION_BRAZIL, REGIONS
from hyundai_kia_connect_api.exceptions import APIError
from hyundai_kia_connect_api.HyundaiBlueLinkApiBR import HyundaiBlueLinkApiBR
from hyundai_kia_connect_api.Vehicle import Vehicle
from hyundai_kia_connect_api.VehicleManager import VehicleManager

_BR_REGION = next(key for key, value in REGIONS.items() if value == REGION_BRAZIL)
_HYUNDAI_BRAND = next(key for key, value in BRANDS.items() if value == BRAND_HYUNDAI)


def _response(payload: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


def _api() -> HyundaiBlueLinkApiBR:
    api = HyundaiBlueLinkApiBR(region=_BR_REGION, brand=_HYUNDAI_BRAND)
    api.session = MagicMock()
    return api


def _token() -> MagicMock:
    return MagicMock(device_id="registered-device", access_token="access-token", pin="1234")


def _vehicle() -> Vehicle:
    return Vehicle(id="vehicle-id", ccu_ccs2_protocol_support=1)


def _image_result(image: bytes = b"image-bytes", **metadata: object) -> dict:
    detail = {"svmImage": base64.b64encode(image).decode("ascii"), **metadata}
    return {"retCode": "S", "resCode": "0000", "resMsg": {"scsDetail": detail}}


def _video_result() -> dict:
    detail = {
        field_name: base64.b64encode(direction.encode("ascii")).decode("ascii")
        for direction, field_name in {
            "top": "svmVideoTop",
            "front": "svmVideoFront",
            "rear": "svmVideoRear",
            "right": "svmVideoRight",
            "left": "svmVideoLeft",
        }.items()
    }
    return {"retCode": "S", "resCode": "0000", "resMsg": detail}


def _capture(api: HyundaiBlueLinkApiBR, result: dict, **kwargs):
    api.session.post.return_value = _response({"retCode": "S", "msgId": "message-id"})
    api.session.get.return_value = _response(result)
    with (
        patch.object(api, "ensure_device_language") as ensure_language,
        patch.object(api, "_ensure_control_token") as ensure_control_token,
    ):
        capture = api.capture_surround_view(_token(), _vehicle(), **kwargs)
    return capture, ensure_language, ensure_control_token


class TestBrazilianSurroundView:
    def test_photo_uses_observed_host_contract_and_keeps_access_token(self):
        api = _api()
        token = _token()
        vehicle = _vehicle()
        api.session.post.return_value = _response({"retCode": "S", "msgId": "message-id"})
        api.session.get.return_value = _response(
            _image_result(
                b"photo",
                imageSize=[1, 2],
                gpsDetail={"lat": "private"},
            )
        )

        order = []
        with (
            patch.object(api, "ensure_device_language", side_effect=lambda _: order.append("language")),
            patch.object(api, "_ensure_control_token", side_effect=lambda _: order.append("pin")),
        ):
            capture = api.capture_surround_view(token, vehicle)

        assert order == ["language", "pin"]
        assert capture.message_id == "message-id"
        assert capture.media_type == "image"
        assert capture.image == b"photo"
        assert capture.videos == {}
        assert capture.metadata == {"imageSize": [1, 2]}
        assert "photo" not in repr(capture)

        (url,) = api.session.post.call_args.args
        request = api.session.post.call_args.kwargs
        assert url == (
            "https://apigw-ccs-h-br.goc-am.hmgmobility.com/"
            "api/v1/spa/vehicles/vehicle-id/svm/async"
        )
        assert "json" not in request
        assert "data" not in request
        assert request["headers"]["Host"] == "apigw-ccs-h-br.goc-am.hmgmobility.com"
        assert request["headers"]["Authorization"] == "Bearer access-token"
        assert request["headers"]["ccsp-service-id"] == api.ccsp_service_id
        assert request["headers"]["ccsp-application-id"] == api.ccsp_application_id
        assert request["headers"]["ccsp-device-id"] == "registered-device"
        assert request["headers"]["ccuCCS2ProtocolSupport"] == "1"

        (poll_url,) = api.session.get.call_args.args
        poll_request = api.session.get.call_args.kwargs
        assert poll_url == url
        assert poll_request["params"] == {"msgId": "message-id"}
        assert api.session.post.call_count == 1

    def test_pending_result_polls_only_the_existing_message_id(self):
        api = _api()
        api.session.post.return_value = _response({"retCode": "S", "msgId": "message-id"})
        api.session.get.side_effect = [
            _response({"retCode": "F", "resCode": "5911"}),
            _response(_image_result()),
        ]

        with (
            patch.object(api, "ensure_device_language"),
            patch.object(api, "_ensure_control_token"),
            patch("hyundai_kia_connect_api.HyundaiBlueLinkApiBR.sleep") as sleep,
        ):
            capture = api.capture_surround_view(_token(), _vehicle())

        assert capture.media_type == "image"
        assert sleep.call_args.args == (5,)
        assert api.session.post.call_count == 1
        assert api.session.get.call_count == 2
        assert all(
            call.kwargs["params"] == {"msgId": "message-id"}
            for call in api.session.get.call_args_list
        )

    def test_full_video_response_is_exposed_without_a_mode_parameter(self):
        api = _api()

        capture, _, _ = _capture(api, _video_result())

        assert capture.media_type == "video"
        assert capture.image is None
        assert capture.videos == {
            "top": b"top",
            "front": b"front",
            "rear": b"rear",
            "right": b"right",
            "left": b"left",
        }
        assert "json" not in api.session.post.call_args.kwargs

    def test_partial_or_mixed_media_is_rejected(self):
        api = _api()
        partial = {
            "retCode": "S",
            "resCode": "0000",
            "resMsg": {"svmVideoTop": base64.b64encode(b"top").decode("ascii")},
        }

        with pytest.raises(APIError, match="incomplete video"):
            _capture(api, partial)

        mixed = _video_result()
        mixed["resMsg"]["svmImage"] = base64.b64encode(b"image").decode("ascii")
        with pytest.raises(APIError, match="mixed image and video"):
            _capture(_api(), mixed)

    @pytest.mark.parametrize(
        "result",
        [
            {"retCode": "S", "resCode": "0000", "resMsg": {"scsDetail": {}}},
            {
                "retCode": "S",
                "resCode": "0000",
                "resMsg": {"scsDetail": {"svmImage": "not base64!"}},
            },
        ],
    )
    def test_missing_or_invalid_media_is_rejected(self, result):
        with pytest.raises(APIError):
            _capture(_api(), result)

    @pytest.mark.parametrize(
        ("res_code", "message"),
        [
            ("4292", "rate limited"),
            ("7779", "vehicle was unavailable"),
            ("7780", "camera was unavailable"),
            ("7781", "hazard lights"),
            ("7782", "battery is low"),
        ],
    )
    def test_known_provider_errors_are_sanitized(self, res_code, message):
        api = _api()
        api.session.post.return_value = _response({"retCode": "S", "msgId": "message-id"})
        api.session.get.return_value = _response(
            {"retCode": "F", "resCode": res_code, "resMsg": "private provider text"}
        )

        with (
            patch.object(api, "ensure_device_language"),
            patch.object(api, "_ensure_control_token"),
            pytest.raises(APIError, match=message) as caught,
        ):
            api.capture_surround_view(_token(), _vehicle())

        assert "private provider text" not in str(caught.value)
        assert api.session.post.call_count == 1

    def test_submission_failure_is_ambiguous_and_never_replayed(self):
        api = _api()
        api.session.post.side_effect = requests.ConnectionError("private network detail")

        with (
            patch.object(api, "ensure_device_language"),
            patch.object(api, "_ensure_control_token"),
            pytest.raises(APIError, match="outcome is unknown") as caught,
        ):
            api.capture_surround_view(_token(), _vehicle())

        assert "private network detail" not in str(caught.value)
        assert api.session.post.call_count == 1
        api.session.get.assert_not_called()

    def test_language_failure_stops_before_pin_or_submission(self):
        api = _api()
        api.ensure_device_language = MagicMock(side_effect=APIError("language unavailable"))
        api._ensure_control_token = MagicMock()

        with pytest.raises(APIError, match="language unavailable"):
            api.capture_surround_view(_token(), _vehicle())

        api._ensure_control_token.assert_not_called()
        api.session.post.assert_not_called()

    def test_timeout_does_not_submit_a_second_capture(self):
        api = _api()
        api.session.post.return_value = _response({"retCode": "S", "msgId": "message-id"})
        api.session.get.return_value = _response({"retCode": "F", "resCode": "5911"})

        with (
            patch.object(api, "ensure_device_language"),
            patch.object(api, "_ensure_control_token"),
            patch("hyundai_kia_connect_api.HyundaiBlueLinkApiBR.time.monotonic", side_effect=[0, 1]),
            pytest.raises(APIError, match="timed out"),
        ):
            api.capture_surround_view(_token(), _vehicle(), timeout_seconds=1)

        assert api.session.post.call_count == 1
        assert api.session.get.call_count == 1


def test_vehicle_manager_delegates_surround_view_capture():
    manager = object.__new__(VehicleManager)
    vehicle = _vehicle()
    manager.token = object()
    manager.vehicles = {vehicle.id: vehicle}
    manager.api = MagicMock()
    expected = MagicMock()
    manager.api.capture_surround_view.return_value = expected

    result = manager.capture_surround_view(
        vehicle.id, poll_seconds=7, timeout_seconds=60
    )

    assert result is expected
    manager.api.capture_surround_view.assert_called_once_with(
        manager.token, vehicle, poll_seconds=7, timeout_seconds=60
    )
