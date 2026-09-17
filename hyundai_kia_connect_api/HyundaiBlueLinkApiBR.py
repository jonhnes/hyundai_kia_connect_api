"""HyundaiBlueLinkApiBR.py"""

# pylint:disable=logging-fstring-interpolation,invalid-name,broad-exception-caught,unused-argument,missing-function-docstring,line-too-long

import base64
import binascii
import datetime as dt
import logging
import re
import time
import typing as ty
import uuid
from datetime import timedelta
from time import sleep
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from requests import Response

from .ApiImpl import (
    ApiImplSession,
    ClimateRequestOptions,
    SurroundViewCapture,
    WindowRequestOptions,
)
from .ApiImplType1 import ApiImplType1
from .const import (
    BRAND_HYUNDAI,
    BRANDS,
    DOMAIN,
    ENGINE_TYPES,
    ORDER_STATUS,
    VEHICLE_LOCK_ACTION,
    WINDOW_STATE,
)
from .exceptions import APIError, AuthenticationError
from .Token import Token
from .utils import get_index_into_hex_temp
from .Vehicle import DayTripCounts, DayTripInfo, MonthTripInfo, TripInfo, Vehicle

_LOGGER = logging.getLogger(__name__)

_BRAZIL_HTTP_LANGUAGE = "pt-BR"
_BRAZIL_DEVICE_LANGUAGE = "BR-PT"
_BRAZIL_LANGUAGE_ALIASES = frozenset({"pt", "pt-br", "br-pt"})
_SVM_API_URL = "https://apigw-ccs-h-br.goc-am.hmgmobility.com/"
_SVM_PENDING_CODE = "5911"
_SVM_VIDEO_FIELDS = {
    "top": "svmVideoTop",
    "front": "svmVideoFront",
    "rear": "svmVideoRear",
    "right": "svmVideoRight",
    "left": "svmVideoLeft",
}
_SVM_METADATA_FIELDS = (
    "imageSize",
    "boundaryArea",
    "installAngle",
    "validAngleofView",
    "doorOpen",
    "sidemirrorOpen",
    "trunkOpen",
)
_SVM_ERROR_MESSAGES = {
    "4292": "Brazilian Hyundai surround-view request was rate limited.",
    "7779": "Brazilian Hyundai surround-view vehicle was unavailable.",
    "7780": "Brazilian Hyundai surround-view camera was unavailable.",
    "7781": "Brazilian Hyundai surround-view is unavailable while hazard lights are on.",
    "7782": "Brazilian Hyundai surround-view is unavailable because the vehicle battery is low.",
}

# The Brazilian signin endpoint returns {"step": N} (HTTP 200, no redirectUrl)
# when the account must complete an action in the Bluelink app / web portal
# before OAuth can proceed. The step numbers map to the routes handled by
# toStep() in the login SPA bundle (/web/v1/user static JS).
_SIGNIN_STEP_MESSAGES = {
    0: "the account must accept the terms of service",
    3: "the account must accept the data-access agreement",
    4: "the account must re-accept updated terms of service",
    5: "the account password has expired and must be reset",
    6: "the account is not activated yet",
    7: "identity verification is required",
    8: "identity verification is required",
    9: "the account is blocked",
    10: "email verification is required",
    11: "the account email must be changed",
    12: "identity verification is required",
    13: "email verification is required",
}


class HyundaiBlueLinkApiBR(ApiImplType1):
    """Brazilian Hyundai BlueLink API implementation.

    Extends ApiImplType1 to reuse its CCS2 status parser
    (``_update_vehicle_properties_ccs2``); BR overrides its own auth, headers,
    endpoint selection and force-refresh flow.
    """

    supports_window_control: bool = True
    # BR does not implement valet mode; keep it off (ApiImplType1 defaults True).
    supports_valet_mode: bool = False
    data_timezone = dt.timezone(dt.timedelta(hours=-3))  # Brazil (BRT/BRST)

    @staticmethod
    def _normalize_language(language: str | None) -> str:
        normalized = (
            (language or _BRAZIL_HTTP_LANGUAGE).strip().lower().replace("_", "-")
        )
        if normalized not in _BRAZIL_LANGUAGE_ALIASES:
            raise APIError("Unsupported Brazilian Hyundai language.")
        return _BRAZIL_HTTP_LANGUAGE

    def __init__(self, region: int, brand: int, language: str = "pt-BR"):
        if BRANDS[brand] != BRAND_HYUNDAI:
            raise APIError(
                f"Unknown brand {BRANDS[brand]} for region Brazil. "
                "Only Hyundai is supported."
            )

        self.language = self._normalize_language(language)
        self.device_language = _BRAZIL_DEVICE_LANGUAGE
        self.base_url = "br-ccapi.hyundai.com.br"
        self.api_url = f"https://{self.base_url}/api/v1/"
        self.api_v2_url = f"https://{self.base_url}/api/v2/"
        self.svm_api_url = _SVM_API_URL
        self.ccsp_device_id: str | None = None
        self._registration_uuid = str(uuid.uuid4())
        self.ccsp_service_id = "03f7df9b-7626-4853-b7bd-ad1e8d722bd5"
        self.ccsp_application_id = "213a491a-0d7c-4d6a-ac03-a2df127d73b0"
        self.basic_authorization_header = (
            "Basic MDNmN2RmOWItNzYyNi00ODUzLWI3YmQtYWQxZThkNzIyYmQ1On"
            "lRejJiYzZDbjhPb3ZWT1I3UkRXd3hUcVZ3V0czeUtCWUZEZzBIc09Yc3l4eVBsSA=="
        )

        self.api_headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "br;q=1.0, gzip;q=0.9, deflate;q=0.8",
            "Accept-Language": f"{self.language};q=1.0, en-US;q=0.9",
            "User-Agent": "BR_BlueLink/1.0.14 (com.hyundai.bluelink.br; build:10132; iOS 18.4.0) Alamofire/5.9.1",
            "Host": self.base_url,
            "offset": "-3",
            "ccuCCS2ProtocolSupport": "0",
        }

        self.session = ApiImplSession()
        self.temperature_range = range(62, 82)

    def _build_api_url(self, path: str) -> str:
        """Build full API URL from path."""
        return urljoin(self.api_url, path.lstrip("/"))

    def _build_api_v2_url(self, path: str) -> str:
        """Build API v2 URL from path."""
        return urljoin(self.api_v2_url, path.lstrip("/"))

    def _build_svm_api_url(self, path: str) -> str:
        """Build a SVM URL on the host used by the current BR mobile app."""
        return urljoin(self.svm_api_url, path.lstrip("/"))

    def _get_device_id(self, stamp: str | None = None) -> str:
        """Register and cache a Brazilian Bluelink device identifier.

        The Android application registers a local UUID with the notification
        endpoint before authenticating. The returned ``deviceId`` is required
        by subsequent vehicle reads and control commands. ``stamp`` is accepted
        for compatibility with the shared device-id retry interface; Brazil
        does not send a Stamp header for this request.
        """
        del stamp
        if self.ccsp_device_id:
            return self.ccsp_device_id

        url = self._build_api_url("/spa/notifications/register")
        headers = {
            "Accept": "application/json",
            "Accept-Language": self.language,
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "okhttp/4.12.0",
            "ccsp-service-id": self.ccsp_service_id,
            "ccsp-application-id": self.ccsp_application_id,
            "offset": "-3",
        }
        payload = {
            "uuid": self._registration_uuid,
            "pushRegId": f"dummy-push-{int(time.time() * 1000)}",
            "pushType": "GCM",
        }

        _LOGGER.debug("%s - Registering Brazilian Bluelink device", DOMAIN)
        response = self.session.post(url, json=payload, headers=headers)
        try:
            data = response.json()
        except ValueError as exc:
            raise APIError(
                "Brazilian Hyundai device registration returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise APIError(
                "Brazilian Hyundai device registration returned an unexpected JSON response."
            )

        if response.status_code >= 400:
            raise APIError(
                "Brazilian Hyundai device registration failed: "
                f"HTTP {response.status_code}, retCode={data.get('retCode')!r}, "
                f"resCode={data.get('resCode')!r}."
            )

        if data.get("retCode") != "S" or data.get("resCode") != "0000":
            raise APIError(
                "Brazilian Hyundai device registration was rejected: "
                f"retCode={data.get('retCode')!r}, resCode={data.get('resCode')!r}."
            )

        response_message = data.get("resMsg")
        device_id = (
            response_message.get("deviceId")
            if isinstance(response_message, dict)
            else None
        )
        if not isinstance(device_id, str) or not device_id:
            raise APIError(
                "Brazilian Hyundai device registration did not return a deviceId."
            )

        self.ccsp_device_id = device_id
        return device_id

    def _get_authenticated_headers(self, token: Token) -> dict:
        """Get headers with authentication."""
        headers = dict(self.api_headers)
        device_id = token.device_id or self.ccsp_device_id
        headers["ccsp-device-id"] = device_id
        headers["ccsp-application-id"] = self.ccsp_application_id
        headers["Authorization"] = f"Bearer {token.access_token}"
        return headers

    @staticmethod
    def _read_language_response(
        response: Response, *, require_language: bool
    ) -> str | None:
        """Validate a device-language response without exposing its body."""
        if response.status_code >= 400:
            raise APIError(
                "Brazilian Hyundai device-language request failed "
                f"with HTTP {response.status_code}."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise APIError(
                "Brazilian Hyundai device-language request returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise APIError(
                "Brazilian Hyundai device-language request returned an invalid response."
            )
        if data.get("retCode") != "S" or data.get("resCode") != "0000":
            raise APIError("Brazilian Hyundai device-language request was rejected.")

        response_message = data.get("resMsg")
        current_language = (
            response_message.get("language")
            if isinstance(response_message, dict)
            else None
        )
        if require_language and (
            not isinstance(current_language, str) or not current_language.strip()
        ):
            raise APIError(
                "Brazilian Hyundai device-language response omitted the language."
            )
        return current_language if isinstance(current_language, str) else None

    def ensure_device_language(self, token: Token) -> bool:
        """Ensure the registered BR device uses Brazilian Portuguese.

        Returns ``True`` only when the server-side setting needed an update.
        The operation is idempotent and never retries automatically.
        """
        device_id = token.device_id or self.ccsp_device_id
        if not isinstance(device_id, str) or not device_id:
            raise APIError(
                "Brazilian Hyundai device-language sync requires a device ID."
            )

        url = self._build_api_url(f"/spa/devices/{device_id}/setting/language")
        headers = self._get_authenticated_headers(token)
        current_response = self.session.get(url, headers=headers)
        current_language = self._read_language_response(
            current_response, require_language=True
        )
        normalized_current = current_language.strip().lower().replace("_", "-")
        if normalized_current in _BRAZIL_LANGUAGE_ALIASES:
            return False

        update_response = self.session.post(
            url,
            json={"language": self.device_language},
            headers=headers,
        )
        self._read_language_response(update_response, require_language=False)
        return True

    def _raise_auth_error(self, response: Response, context: str) -> None:
        """Surface a readable auth error for non-2xx BR auth responses.

        Reads the response body (JSON: ``{step}``, ``{errCode, errMsg}``, or
        arbitrary) and raises ``AuthenticationError``. Falls back to a short body
        snippet when the body is not JSON. No-op for status < 400. Only
        step/errCode/errMsg are surfaced — the whole body is never dumped.
        """
        if response.status_code < 400:
            return
        try:
            data = response.json()
        except ValueError:
            snippet = (response.text or "")[:200]
            raise AuthenticationError(
                f"Brazilian Hyundai {context} failed: HTTP {response.status_code}. "
                f"Response not JSON: {snippet!r}"
            ) from None
        step = data.get("step")
        reason = _SIGNIN_STEP_MESSAGES.get(step)
        if reason is not None:
            raise AuthenticationError(
                f"Brazilian Hyundai login incomplete: {reason} "
                f"({context} step={step}). Complete this in the Bluelink app "
                "or web portal, then retry."
            )
        err_code = data.get("errCode") or data.get("errorCode")
        err_msg = data.get("errMsg") or data.get("errorMessage")
        if err_code or err_msg:
            raise AuthenticationError(
                f"Brazilian Hyundai {context} failed: "
                f"errCode={err_code!r}, errMsg={err_msg!r}"
            )
        raise AuthenticationError(
            f"Brazilian Hyundai {context} failed: HTTP {response.status_code} "
            f"(keys={sorted(data.keys())})"
        )

    def _get_cookies(self) -> dict:
        """Request cookies from the API for authentication."""
        params = {
            "response_type": "code",
            "client_id": self.ccsp_service_id,
            "redirect_uri": self._build_api_url("/user/oauth2/redirect"),
        }

        url = self._build_api_url("/user/oauth2/authorize")
        _LOGGER.debug(f"{DOMAIN} - Requesting cookies from {url}")
        response = self.session.get(url, params=params)
        self._raise_auth_error(response, "cookie request")
        # The session cookie ('account') is set during the 302 redirect, so it
        # lives in the session cookie jar rather than on the final response.
        return self.session.cookies.get_dict()

    def _get_authorization_code(
        self, cookies: dict, username: str, password: str
    ) -> str:
        """Get authorization code from redirect URL."""
        url = self._build_api_url("/user/signin")
        data = {"email": username, "password": password}

        headers = {
            "Referer": "https://br-ccapi.hyundai.com.br/web/v1/user/signin",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Content-Type": "text/plain;charset=UTF-8",
            "Host": self.api_headers["Host"],
            "Accept-Language": "pt-BR,en-US;q=0.9,en;q=0.8",
            "Origin": "https://br-ccapi.hyundai.com.br",
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148_CCS_APP_iOS"
            ),
        }

        response = self.session.post(url, json=data, cookies=cookies, headers=headers)
        self._raise_auth_error(response, "signin")
        response_data = response.json()

        redirect_url = response_data.get("redirectUrl")
        if not redirect_url:
            # The account authenticated but must complete an action before
            # OAuth can proceed (see _SIGNIN_STEP_MESSAGES). Surface a clear
            # message instead of a raw KeyError on "redirectUrl".
            step = response_data.get("step")
            reason = _SIGNIN_STEP_MESSAGES.get(step)
            if reason is not None:
                raise AuthenticationError(
                    f"Brazilian Hyundai login incomplete: {reason} "
                    f"(signin step={step}). Complete this in the Bluelink app "
                    "or web portal, then retry."
                )
            raise AuthenticationError(
                "Brazilian Hyundai login failed: no redirectUrl in signin "
                f"response (keys={sorted(response_data.keys())}). "
                "Check your username and password."
            )

        _LOGGER.debug(f"{DOMAIN} - Got redirect URL")
        parsed_url = urlparse(redirect_url)
        code_list = parse_qs(parsed_url.query).get("code")
        if not code_list:
            raise AuthenticationError(
                "Brazilian Hyundai login failed: no authorization code in redirect URL."
            )
        return code_list[0]

    def _get_auth_response(self, authorization_code: str) -> dict:
        """Request access token from the API."""
        url = self._build_api_url("/user/oauth2/token")
        body = {
            "client_id": self.ccsp_service_id,
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": self._build_api_url("/user/oauth2/redirect"),
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": self.api_headers["User-Agent"],
            "Authorization": self.basic_authorization_header,
        }

        response = self.session.post(url, data=body, headers=headers)
        self._raise_auth_error(response, "token request")
        return response.json()

    def login(
        self,
        username: str,
        password: str,
        otp_handler: ty.Callable[[dict], dict] | None = None,
        pin: str | None = None,
    ) -> Token:
        """Login to Brazilian Hyundai API."""
        _LOGGER.debug(f"{DOMAIN} - Logging in to Brazilian API")

        device_id = self._get_device_id()
        cookies = self._get_cookies()
        authorization_code = self._get_authorization_code(cookies, username, password)
        auth_response = self._get_auth_response(authorization_code)

        expires_in_seconds = auth_response["expires_in"]
        expires_at = dt.datetime.now(dt.UTC) + timedelta(seconds=expires_in_seconds)

        token = Token(
            access_token=auth_response["access_token"],
            refresh_token=auth_response["refresh_token"],
            valid_until=expires_at,
            username=username,
            password=password,
            device_id=device_id,
            pin=pin,
        )
        try:
            self.ensure_device_language(token)
        except APIError:
            # Language must never make read-only access unavailable. Remote
            # commands call ensure_device_language again and fail closed.
            _LOGGER.warning(
                "%s - language_sync_failed stage=login status=provider_error",
                DOMAIN,
            )
        return token

    def get_vehicles(self, token: Token) -> list:
        """Get list of vehicles."""
        url = self._build_api_url("/spa/vehicles")
        headers = self._get_authenticated_headers(token)

        response = self.session.get(url, headers=headers)
        response.raise_for_status()
        response_data = response.json()
        _LOGGER.debug(f"{DOMAIN} - Got vehicles response")
        if "resMsg" not in response_data or "vehicles" not in response_data.get(
            "resMsg", {}
        ):
            raise APIError("Missing resMsg or vehicles in response")
        result = []
        for entry in response_data["resMsg"]["vehicles"]:
            # Map vehicle type to engine type
            vehicle_type = entry["type"]
            if vehicle_type == "GN":
                entry_engine_type = ENGINE_TYPES.ICE
            elif vehicle_type == "EV":
                entry_engine_type = ENGINE_TYPES.EV
            elif vehicle_type in ["PHEV", "PE"]:
                entry_engine_type = ENGINE_TYPES.PHEV
            elif vehicle_type == "HV":
                entry_engine_type = ENGINE_TYPES.HEV
            else:
                entry_engine_type = ENGINE_TYPES.ICE

            vehicle = Vehicle(
                id=entry["vehicleId"],
                name=entry["nickname"],
                model=entry["vehicleName"],
                registration_date=entry["regDate"],
                VIN=entry["vin"],
                timezone=self.data_timezone,
                engine_type=entry_engine_type,
                ccu_ccs2_protocol_support=entry.get("ccuCCS2ProtocolSupport", 0),
            )
            result.append(vehicle)

        return result

    def _get_cached_vehicle_state(self, token: Token, vehicle: Vehicle) -> dict:
        """Return the server-cached CCS2 vehicle status (does not wake the car).

        BR vehicles report ``ccuCCS2ProtocolSupport: 0`` but the cached status
        is served in CCS2 format at ``/ccs2/carstatus/latest`` (including
        location). The legacy ``/status/latest`` endpoint is the remote-control
        command result on BR and always returns 503 / resCode 5031.
        """
        url = self._build_api_url(f"/spa/vehicles/{vehicle.id}/ccs2/carstatus/latest")
        headers = self._get_authenticated_headers(token)
        response = self.session.get(url, headers=headers)
        response.raise_for_status()
        return response.json()["resMsg"]["state"]["Vehicle"]

    def update_vehicle_with_cached_state(self, token: Token, vehicle: Vehicle) -> None:
        """Update with the server-cached CCS2 state (does not wake the car)."""
        state = self._get_cached_vehicle_state(token, vehicle)
        self._update_vehicle_properties_ccs2(vehicle, state)

    def force_refresh_vehicle_state(self, token: Token, vehicle: Vehicle) -> None:
        """Force a fresh reading from the vehicle (wakes the car).

        BR CCS2 force is asynchronous: ``GET /ccs2/carstatus`` only acknowledges,
        then the vehicle pushes a fresh snapshot to ``/ccs2/carstatus/latest``.
        Wake, wait for the car to report, then read the cached snapshot. If the
        snapshot's ``lastUpdateTime`` did not advance, the car did not report in
        time — do not apply stale data; raise so the coordinator surfaces
        ``UpdateFailed`` and entities go unavailable until the next poll.
        """
        headers = self._get_authenticated_headers(token)
        latest_url = self._build_api_url(
            f"/spa/vehicles/{vehicle.id}/ccs2/carstatus/latest"
        )
        trigger_url = self._build_api_url(f"/spa/vehicles/{vehicle.id}/ccs2/carstatus")

        # Baseline: current cached lastUpdateTime before waking.
        pre = self.session.get(latest_url, headers=headers)
        pre.raise_for_status()
        baseline_ts = pre.json()["resMsg"].get("lastUpdateTime")

        # Wake the vehicle; errors propagate so a failed wake does not fall
        # through to a stale /latest apply.
        self.session.get(trigger_url, headers=headers).raise_for_status()
        sleep(25)

        response = self.session.get(latest_url, headers=headers)
        response.raise_for_status()
        resmsg = response.json()["resMsg"]
        if resmsg.get("lastUpdateTime") == baseline_ts:
            raise APIError(
                "Brazilian Hyundai force refresh did not return fresh data "
                "in time; vehicle may be unreachable."
            )
        self._update_vehicle_properties_ccs2(vehicle, resmsg["state"]["Vehicle"])

    def _ensure_control_token(self, token: Token) -> str:
        """Ensure we have a valid control token for remote commands."""
        control_token = getattr(token, "control_token", None)
        expires_at = getattr(token, "control_token_expires_at", None)
        if (
            control_token
            and expires_at
            and expires_at - dt.timedelta(seconds=5) > dt.datetime.now(dt.UTC)
        ):
            return control_token

        if not token.pin:
            raise APIError("PIN is required for remote commands.")

        device_id = token.device_id or self.ccsp_device_id
        token.device_id = device_id

        url = self._build_api_url("/user/pin")
        headers = self._get_authenticated_headers(token)
        payload = {"pin": token.pin, "deviceId": device_id}

        response = self.session.put(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

        if data.get("controlToken") is None:
            raise APIError("Failed to obtain control token.")

        control_token = f"Bearer {data['controlToken']}"
        expires_in = data.get("expiresTime", 0)
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=expires_in or 600)

        token.control_token = control_token
        token.control_token_expires_at = expires_at
        return control_token

    def _get_svm_authenticated_headers(self, token: Token, vehicle: Vehicle) -> dict:
        """Return the app-equivalent headers for the dedicated SVM host.

        The currently observed Android SVM interceptor authenticates with the
        OAuth access token. A control token is still obtained before the
        operation as a local PIN gate, but is deliberately not substituted for
        ``Authorization`` here: it was not part of the observed SVM contract.
        """
        headers = self._get_authenticated_headers(token)
        headers["Host"] = urlparse(self.svm_api_url).netloc
        headers["ccsp-service-id"] = self.ccsp_service_id
        ccs2_support = getattr(vehicle, "ccu_ccs2_protocol_support", 0) or 0
        headers["ccuCCS2ProtocolSupport"] = str(ccs2_support)
        return headers

    @staticmethod
    def _read_svm_payload(response: Response, *, stage: str) -> dict:
        """Validate a SVM HTTP response without including its body in errors."""
        if response.status_code >= 400:
            raise APIError(
                f"Brazilian Hyundai surround-view {stage} failed with HTTP "
                f"{response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise APIError(
                f"Brazilian Hyundai surround-view {stage} returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise APIError(
                f"Brazilian Hyundai surround-view {stage} returned an invalid response."
            )
        return payload

    @staticmethod
    def _raise_for_svm_provider_error(payload: dict) -> None:
        """Raise a sanitized error for a non-pending SVM provider result."""
        result_code = str(payload.get("resCode") or "")
        if result_code == _SVM_PENDING_CODE:
            return
        if payload.get("retCode") == "F":
            raise APIError(
                _SVM_ERROR_MESSAGES.get(
                    result_code,
                    "Brazilian Hyundai surround-view request was rejected "
                    f"(resCode={result_code or 'unknown'}).",
                )
            )
        if payload.get("retCode") not in (None, "S"):
            raise APIError("Brazilian Hyundai surround-view returned an unknown status.")

    @staticmethod
    def _decode_svm_base64(value: object, *, field_name: str) -> bytes:
        """Decode one provider media field without exposing its content."""
        if not isinstance(value, str) or not value:
            raise APIError(
                f"Brazilian Hyundai surround-view omitted {field_name} media."
            )
        try:
            decoded = base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
            raise APIError(
                f"Brazilian Hyundai surround-view returned invalid {field_name} media."
            ) from exc
        if not decoded:
            raise APIError(
                f"Brazilian Hyundai surround-view returned empty {field_name} media."
            )
        return decoded

    @staticmethod
    def _svm_result_containers(payload: dict) -> tuple[dict, dict]:
        """Return the SVM result and its optional image-detail object."""
        result = payload.get("resMsg")
        if not isinstance(result, dict):
            raise APIError("Brazilian Hyundai surround-view result omitted resMsg.")
        detail = result.get("scsDetail")
        return result, detail if isinstance(detail, dict) else result

    @staticmethod
    def _svm_metadata(result: dict, detail: dict) -> dict[str, object]:
        """Keep only non-location SVM metadata for callers and diagnostics."""
        metadata: dict[str, object] = {}
        for field_name in _SVM_METADATA_FIELDS:
            if field_name in detail:
                metadata[field_name] = detail[field_name]
            elif field_name in result:
                metadata[field_name] = result[field_name]
        return metadata

    def _parse_svm_capture(
        self, payload: dict, message_id: str
    ) -> SurroundViewCapture:
        """Convert a completed SVM response into one strictly typed result."""
        self._raise_for_svm_provider_error(payload)
        result, detail = self._svm_result_containers(payload)
        image_value = detail.get("svmImage")
        if image_value is None and detail is not result:
            image_value = result.get("svmImage")

        video_values: dict[str, object] = {}
        for direction, field_name in _SVM_VIDEO_FIELDS.items():
            value = detail.get(field_name)
            if value is None and detail is not result:
                value = result.get(field_name)
            if value is not None:
                video_values[direction] = value

        if image_value is not None and video_values:
            raise APIError(
                "Brazilian Hyundai surround-view returned mixed image and video media."
            )
        if image_value is not None:
            return SurroundViewCapture(
                message_id=message_id,
                media_type="image",
                image=self._decode_svm_base64(image_value, field_name="image"),
                metadata=self._svm_metadata(result, detail),
            )
        if video_values:
            if set(video_values) != set(_SVM_VIDEO_FIELDS):
                raise APIError(
                    "Brazilian Hyundai surround-view returned incomplete video media."
                )
            return SurroundViewCapture(
                message_id=message_id,
                media_type="video",
                videos={
                    direction: self._decode_svm_base64(value, field_name="video")
                    for direction, value in video_values.items()
                },
                metadata=self._svm_metadata(result, detail),
            )
        raise APIError("Brazilian Hyundai surround-view result did not contain media.")

    def capture_surround_view(
        self,
        token: Token,
        vehicle: Vehicle,
        *,
        poll_seconds: float = 5,
        timeout_seconds: float = 145,
    ) -> SurroundViewCapture:
        """Request one SVM capture and poll its existing ``msgId`` only.

        The POST has a physical effect: on supported vehicles it can unfold the
        mirrors and use the surround cameras. It is submitted at most once.
        No request body or mode parameter is sent because the observed mobile
        contract exposes neither.
        """
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be greater than zero.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")

        # Keep the existing BR mutable-operation safeguards. The token is a
        # PIN gate only; SVM's observed Authorization remains the access token.
        self.ensure_device_language(token)
        self._ensure_control_token(token)

        url = self._build_svm_api_url(
            f"/api/v1/spa/vehicles/{vehicle.id}/svm/async"
        )
        headers = self._get_svm_authenticated_headers(token, vehicle)
        try:
            response = self.session.post(url, headers=headers)
        except (APIError, requests.RequestException) as exc:
            raise APIError(
                "Brazilian Hyundai surround-view submission outcome is unknown; "
                "do not retry automatically."
            ) from exc
        submission = self._read_svm_payload(response, stage="submission")
        self._raise_for_svm_provider_error(submission)
        message_id = submission.get("msgId")
        if not isinstance(message_id, str) or not message_id:
            raise APIError(
                "Brazilian Hyundai surround-view submission did not return a msgId."
            )

        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                response = self.session.get(
                    url,
                    params={"msgId": message_id},
                    headers=headers,
                )
            except (APIError, requests.RequestException) as exc:
                raise APIError(
                    "Brazilian Hyundai surround-view polling failed; "
                    "the capture outcome remains unknown."
                ) from exc
            result = self._read_svm_payload(response, stage="polling")
            if str(result.get("resCode") or "") != _SVM_PENDING_CODE:
                return self._parse_svm_capture(result, message_id)
            if time.monotonic() >= deadline:
                raise APIError(
                    "Brazilian Hyundai surround-view timed out before media was available."
                )
            sleep(poll_seconds)

    def lock_action(
        self, token: Token, vehicle: Vehicle, action: VEHICLE_LOCK_ACTION
    ) -> str:
        """Lock or unlock the vehicle."""
        self.ensure_device_language(token)
        control_token = self._ensure_control_token(token)
        device_id = token.device_id or self.ccsp_device_id

        url = self._build_api_v2_url(
            f"spa/vehicles/{vehicle.id}/control/door",
        )
        headers = self._get_authenticated_headers(token)
        headers["Authorization"] = control_token
        headers["ccsp-device-id"] = device_id
        headers["ccuCCS2ProtocolSupport"] = str(vehicle.ccu_ccs2_protocol_support or 0)

        payload = {"deviceId": device_id, "action": action.value}
        _LOGGER.debug("%s - Lock action request prepared: %s", DOMAIN, action.value)

        response = self.session.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        _LOGGER.debug("%s - Lock action response received", DOMAIN)

        if data.get("retCode") != "S":
            raise APIError(
                f"Lock action failed: {data.get('resCode')} {data.get('resMsg')}"
            )

        return data.get("msgId")

    def check_action_status(
        self,
        token: Token,
        vehicle: Vehicle,
        action_id: str,
        synchronous: bool = False,
        timeout: int = 0,
    ) -> ORDER_STATUS:
        """Check status of a previously submitted remote command."""
        if synchronous:
            if timeout < 1:
                raise APIError("Timeout must be 1 or higher for synchronous checks.")

            end_time = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=timeout)
            while dt.datetime.now(dt.UTC) < end_time:
                state = self.check_action_status(
                    token, vehicle, action_id, synchronous=False
                )
                if state == ORDER_STATUS.PENDING:
                    sleep(5)
                    continue
                return state

            return ORDER_STATUS.TIMEOUT

        url = self._build_api_url(f"/spa/notifications/{vehicle.id}/records")
        headers = self._get_authenticated_headers(token)

        response = self.session.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        _LOGGER.debug(f"{DOMAIN} - Action status response: %s", data)

        records = data.get("resMsg", [])
        for record in records:
            if record.get("recordId") != action_id:
                continue

            result = (record.get("result") or "").lower()
            if result == "success":
                return ORDER_STATUS.SUCCESS
            if result == "fail":
                return ORDER_STATUS.FAILED
            if result == "non-response":
                return ORDER_STATUS.TIMEOUT
            if result in ("", "pending", None):
                return ORDER_STATUS.PENDING

        return ORDER_STATUS.UNKNOWN

    def set_windows_state(
        self, token: Token, vehicle: Vehicle, options: WindowRequestOptions
    ) -> str:
        """Open or close all windows (BR API controls all windows together)."""
        self.ensure_device_language(token)
        control_token = self._ensure_control_token(token)
        device_id = token.device_id or self.ccsp_device_id

        url = self._build_api_v2_url(f"spa/vehicles/{vehicle.id}/control/window")

        # Brazilian API uses simple action for all windows at once
        # Check if any window should be open, otherwise close
        action = "open"
        if (
            options.front_left == WINDOW_STATE.CLOSED
            or options.front_right == WINDOW_STATE.CLOSED
            or options.back_left == WINDOW_STATE.CLOSED
            or options.back_right == WINDOW_STATE.CLOSED
        ):
            action = "close"

        headers = self._get_authenticated_headers(token)
        headers["Authorization"] = control_token
        headers["ccsp-device-id"] = device_id
        headers["ccuCCS2ProtocolSupport"] = str(vehicle.ccu_ccs2_protocol_support or 0)

        payload = {"action": action, "deviceId": device_id}
        _LOGGER.debug("%s - Window action request prepared: %s", DOMAIN, action)

        response = self.session.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        _LOGGER.debug("%s - Window action response received", DOMAIN)

        if data.get("retCode") != "S":
            raise APIError(
                f"Window action failed: {data.get('resCode')} {data.get('resMsg')}"
            )

        return data.get("msgId")

    def start_hazard_lights(self, token: Token, vehicle: Vehicle) -> str:
        """Turn on hazard lights (lights only, no horn)."""
        self.ensure_device_language(token)
        control_token = self._ensure_control_token(token)
        device_id = token.device_id or self.ccsp_device_id

        url = self._build_api_v2_url(f"spa/vehicles/{vehicle.id}/control/light")
        headers = self._get_authenticated_headers(token)
        headers["Authorization"] = control_token
        headers["ccsp-device-id"] = device_id
        headers["ccuCCS2ProtocolSupport"] = str(vehicle.ccu_ccs2_protocol_support or 0)

        _LOGGER.debug(f"{DOMAIN} - Hazard lights request")

        response = self.session.post(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        _LOGGER.debug("%s - Hazard lights response received", DOMAIN)

        if data.get("retCode") != "S":
            raise APIError(
                f"Hazard lights failed: {data.get('resCode')} {data.get('resMsg')}"
            )

        return data.get("msgId")

    def get_notification_history(self, token: Token, vehicle: Vehicle) -> list:
        """Get notification history (for debugging and tracking command results)."""
        url = self._build_api_url(f"/spa/notifications/{vehicle.id}/history")
        headers = self._get_authenticated_headers(token)

        response = self.session.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        _LOGGER.debug(f"{DOMAIN} - Notification history response")

        return data.get("resMsg", [])

    def start_climate(
        self, token: Token, vehicle: Vehicle, options: ClimateRequestOptions
    ) -> str:
        """Start climate control with temperature and seat heating settings."""
        self.ensure_device_language(token)
        control_token = self._ensure_control_token(token)
        device_id = token.device_id or self.ccsp_device_id

        url = self._build_api_v2_url(f"spa/vehicles/{vehicle.id}/control/engine")

        # Set defaults
        if options.duration is None:
            options.duration = 10  # 10 minutes default
        if options.defrost is None:
            options.defrost = False
        if options.climate is None:
            options.climate = True
        if options.heating is None:
            options.heating = 0
        if options.front_left_seat is None:
            options.front_left_seat = 0

        # Most BR vehicles use direct Celsius values. Some models expose a
        # vehicle-specific scale (including LOW/HIGH) instead; callers that
        # have that confirmed mapping can supply its already-encoded code.
        if options.temp_code is not None:
            temp_code = options.temp_code.upper()
            if re.fullmatch(r"[0-9A-F]{2}H", temp_code) is None:
                raise ValueError(
                    "temp_code must be a two-digit hexadecimal code ending in H"
                )
        else:
            if options.set_temp is None:
                options.set_temp = 21  # 21°C default
            temp_celsius = int(options.set_temp)
            temp_code = get_index_into_hex_temp(temp_celsius)

        # Map seat heating level (0-5 in ClimateRequestOptions to 0-8 for BR API)
        # 0=off, 1-3=heat levels, 4-5=cool levels (BR uses similar mapping)
        seat_heat_cmd = options.front_left_seat if options.front_left_seat else 0

        headers = self._get_authenticated_headers(token)
        headers["Authorization"] = control_token
        headers["ccsp-device-id"] = device_id
        headers["ccuCCS2ProtocolSupport"] = str(vehicle.ccu_ccs2_protocol_support or 0)

        payload = {
            "action": "start",
            "options": {
                "airCtrl": 1 if options.climate else 0,
                "heating1": int(options.heating),
                "seatHeaterVentCMD": {"drvSeatOptCmd": seat_heat_cmd},
                "defrost": options.defrost,
                "igniOnDuration": options.duration,
            },
            "hvacType": 1,
            "deviceId": device_id,
            "tempCode": temp_code,
            "unit": "C",
        }

        _LOGGER.debug("%s - Start climate request prepared", DOMAIN)

        response = self.session.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        _LOGGER.debug("%s - Start climate response received", DOMAIN)

        if data.get("retCode") != "S":
            raise APIError(
                f"Start climate failed: {data.get('resCode')} {data.get('resMsg')}"
            )

        return data.get("msgId")

    def stop_climate(self, token: Token, vehicle: Vehicle) -> str:
        """Stop climate control."""
        self.ensure_device_language(token)
        control_token = self._ensure_control_token(token)
        device_id = token.device_id or self.ccsp_device_id

        url = self._build_api_v2_url(f"spa/vehicles/{vehicle.id}/control/engine")

        headers = self._get_authenticated_headers(token)
        headers["Authorization"] = control_token
        headers["ccsp-device-id"] = device_id
        headers["ccuCCS2ProtocolSupport"] = str(vehicle.ccu_ccs2_protocol_support or 0)

        payload = {"action": "stop", "deviceId": device_id}

        _LOGGER.debug("%s - Stop climate request prepared", DOMAIN)

        response = self.session.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        _LOGGER.debug("%s - Stop climate response received", DOMAIN)

        if data.get("retCode") != "S":
            raise APIError(
                f"Stop climate failed: {data.get('resCode')} {data.get('resMsg')}"
            )

        return data.get("msgId")

    def update_month_trip_info(
        self, token: Token, vehicle: Vehicle, yyyymm_string: str
    ) -> None:
        """Update monthly trip info."""
        url = self._build_api_url(f"/spa/vehicles/{vehicle.id}/tripinfo")
        data = {"tripPeriodType": 0, "setTripMonth": yyyymm_string}

        headers = self._get_authenticated_headers(token)
        response = self.session.post(url, json=data, headers=headers)

        try:
            response.raise_for_status()
            trip_data = response.json()["resMsg"]

            if trip_data.get("monthTripDayCnt", 0) > 0:
                result = MonthTripInfo(
                    yyyymm=yyyymm_string,
                    day_list=[],
                    summary=TripInfo(
                        drive_time=trip_data.get("tripDrvTime"),
                        idle_time=trip_data.get("tripIdleTime"),
                        distance=trip_data.get("tripDist"),
                        avg_speed=trip_data.get("tripAvgSpeed"),
                        max_speed=trip_data.get("tripMaxSpeed"),
                    ),
                )

                for day in trip_data.get("tripDayList", []):
                    processed_day = DayTripCounts(
                        yyyymmdd=day["tripDayInMonth"],
                        trip_count=day["tripCntDay"],
                    )
                    result.day_list.append(processed_day)

                vehicle.month_trip_info = result
        except Exception as e:
            _LOGGER.warning(f"{DOMAIN} - Failed to get month trip info: {e}")

    def update_day_trip_info(
        self, token: Token, vehicle: Vehicle, yyyymmdd_string: str
    ) -> None:
        """Update daily trip info."""
        url = self._build_api_url(f"/spa/vehicles/{vehicle.id}/tripinfo")
        data = {"tripPeriodType": 1, "setTripDay": yyyymmdd_string}

        headers = self._get_authenticated_headers(token)
        response = self.session.post(url, json=data, headers=headers)

        try:
            response.raise_for_status()
            trip_data = response.json()["resMsg"]
            day_trip_list = trip_data.get("dayTripList", [])

            if len(day_trip_list) > 0:
                msg = day_trip_list[0]
                result = DayTripInfo(
                    yyyymmdd=yyyymmdd_string,
                    trip_list=[],
                    summary=TripInfo(
                        drive_time=msg.get("tripDrvTime"),
                        idle_time=msg.get("tripIdleTime"),
                        distance=msg.get("tripDist"),
                        avg_speed=msg.get("tripAvgSpeed"),
                        max_speed=msg.get("tripMaxSpeed"),
                    ),
                )

                for trip in msg.get("tripList", []):
                    processed_trip = TripInfo(
                        hhmmss=trip.get("tripTime"),
                        drive_time=trip.get("tripDrvTime"),
                        idle_time=trip.get("tripIdleTime"),
                        distance=trip.get("tripDist"),
                        avg_speed=trip.get("tripAvgSpeed"),
                        max_speed=trip.get("tripMaxSpeed"),
                    )
                    result.trip_list.append(processed_trip)

                vehicle.day_trip_info = result
        except Exception as e:
            _LOGGER.warning(f"{DOMAIN} - Failed to get day trip info: {e}")
