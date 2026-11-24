from datetime import datetime, timezone
import hashlib
import logging
from threading import Lock
from time import gmtime, monotonic, strftime
from typing import Any

import requests

from .const import (
    ACCOUNT_BASE_URL,
    API_BASE_URL,
    BATTERY_CHART_API_URI,
    BATTERY_HEALTH_API_URI,
    CYCLING_STATISTICS_API_URI,
    FIRMWARE_API_URI,
    LOGIN_URI,
    MESSAGE_LIST_API_URI,
    MOTOR_BATTERY_API_URI,
    MOTOR_INDEX_API_URI,
    MOTOINFO_ALL_API_URI,
    MOTOINFO_LIST_API_URI,
    TRACK_DETAIL_API_URI,
    TRACK_LIST_API_URI,
)

_LOGGER = logging.getLogger(__name__)

APP_ID = "niu_ktdrr960"
REQUEST_TIMEOUT = (10, 30)
TOKEN_REFRESH_MARGIN = 5 * 60
TOKEN_REFRESH_FALLBACK = 22 * 60 * 60
TRACK_DETAIL_RETRY_INTERVAL = 6 * 60 * 60
# Larger pages make NIU skip parts of the ride history.
TRACK_PAGE_SIZE = 5


class NiuApiError(Exception):
    """Base exception for NIU API failures."""


class NiuConnectionError(NiuApiError):
    """The NIU service could not be reached."""


class NiuAuthenticationError(NiuApiError):
    """NIU rejected the current authentication."""


class NiuHttpError(NiuApiError):
    """NIU returned an unexpected HTTP client error."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"NIU API returned HTTP {status_code}")
        self.status_code = status_code


class NiuServerError(NiuApiError):
    """NIU returned a server-side error."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"NIU API returned server error HTTP {status_code}")
        self.status_code = status_code


class NiuRateLimitError(NiuApiError):
    """NIU rate-limited the client."""

    def __init__(self, retry_after: int | None) -> None:
        message = "NIU API rate limit exceeded"
        if retry_after is not None:
            message += f"; retry after {retry_after} seconds"
        super().__init__(message)
        self.retry_after = retry_after


class NiuResponseError(NiuApiError):
    """NIU returned an invalid JSON document or an API-level error."""

    def __init__(self, message: str, niu_status: object | None = None) -> None:
        super().__init__(message)
        self.niu_status = niu_status


class NiuVehicleNotFoundError(NiuApiError):
    """The selected vehicle index does not exist in the NIU account."""

    def __init__(self, scooter_id: int) -> None:
        super().__init__(f"NIU vehicle index {scooter_id} does not exist")
        self.scooter_id = scooter_id


class NiuApi:
    def __init__(
        self,
        username,
        password,
        scooter_id,
        language="en-US",
        timezone="UTC",
    ) -> None:
        self.username = username
        self.password = password
        self.scooter_id = int(scooter_id)
        self.language = language
        self.timezone = timezone

        self.token = None
        self.refresh_token = None
        self._token_refresh_at = None
        self._auth_generation = 0
        self._auth_lock = Lock()

        self.dataBat = None
        self.dataMoto = None
        self.dataMotoInfo = None
        self.dataTrackInfo = None
        self.track_points: list[tuple[float, float]] = []
        self.track_points_id: str | None = None
        self._track_detail_failed_id: str | None = None
        self._track_detail_retry_at = 0.0
        self.sn = None
        self.sensor_prefix = ""

    @classmethod
    def from_hass(cls, hass, username, password, scooter_id):
        """Create NiuApi with locale settings from Home Assistant config."""
        language = hass.config.language
        # Only append country if language is a bare code (e.g. "en"),
        # not if it already includes a region (e.g. "en-GB", "zh-Hans")
        if hass.config.country and "-" not in language:
            language = f"{language}-{hass.config.country}"
        return cls(
            username,
            password,
            scooter_id,
            language=language,
            timezone=str(hass.config.time_zone),
        )

    def initialize(self):
        """Authenticate and load vehicle metadata without polling entity data."""
        self.get_token()
        vehicles = self.get_vehicles_info(MOTOINFO_LIST_API_URI)
        try:
            items = vehicles["data"]["items"]
        except (KeyError, TypeError) as err:
            raise NiuResponseError("NIU vehicle list has an unexpected format") from err

        if not isinstance(items, list):
            raise NiuResponseError("NIU vehicle list has an unexpected format")
        if self.scooter_id < 0 or self.scooter_id >= len(items):
            raise NiuVehicleNotFoundError(self.scooter_id)

        try:
            vehicle = items[self.scooter_id]
            sn = vehicle["sn_id"]
            scooter_name = vehicle["scooter_name"]
        except (KeyError, TypeError) as err:
            raise NiuResponseError("NIU vehicle list has an unexpected format") from err
        if not sn or not scooter_name:
            raise NiuResponseError("NIU vehicle list has an unexpected format")

        self.sn = str(sn)
        self.sensor_prefix = str(scooter_name)

    def get_token(self):
        """Authenticate with username and password and return an access token."""
        self._password_login()
        return self.token

    def refresh_access_token(self):
        """Renew the current access token using NIU's refresh token grant."""
        if not self.refresh_token:
            raise NiuAuthenticationError("NIU did not provide a refresh token")

        data = {
            "app_id": APP_ID,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        self._store_token(self._request_token(data))
        return self.token

    def _password_login(self):
        password_hash = hashlib.md5(self.password.encode("utf-8")).hexdigest()
        data = {
            "account": self.username,
            "password": password_hash,
            "grant_type": "password",
            "scope": "base",
            "app_id": APP_ID,
        }
        self._store_token(self._request_token(data))

    def _request_token(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = requests.post(
                ACCOUNT_BASE_URL + LOGIN_URI,
                data=data,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as err:
            raise NiuConnectionError("Could not reach NIU authentication service") from err

        payload = self._parse_response(response, authentication_request=True)
        try:
            token_data = payload["data"]["token"]
            access_token = token_data["access_token"]
        except (KeyError, TypeError) as err:
            raise NiuAuthenticationError(
                "NIU authentication response did not contain an access token"
            ) from err
        if not isinstance(token_data, dict) or not access_token:
            raise NiuAuthenticationError("NIU returned an invalid access token")
        return token_data

    def _store_token(self, token_data: dict[str, Any]) -> None:
        self.token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token")
        if refresh_token:
            self.refresh_token = refresh_token

        refresh_delay = TOKEN_REFRESH_FALLBACK
        expires_in = token_data.get("expires_in")
        if expires_in is not None:
            try:
                refresh_delay = max(0, int(expires_in) - TOKEN_REFRESH_MARGIN)
            except (TypeError, ValueError):
                _LOGGER.debug("NIU returned an invalid expires_in value")
        self._token_refresh_at = monotonic() + refresh_delay
        self._auth_generation += 1

    def _ensure_access_token(self) -> None:
        if not self.token:
            with self._auth_lock:
                if not self.token:
                    self._password_login()
            return

        if self._token_refresh_at is None or monotonic() < self._token_refresh_at:
            return

        with self._auth_lock:
            if self._token_refresh_at is None or monotonic() < self._token_refresh_at:
                return
            try:
                if self.refresh_token:
                    self.refresh_access_token()
                else:
                    self._password_login()
            except NiuAuthenticationError:
                self._password_login()
            except NiuApiError as err:
                # The existing token may still be valid. Let the data request decide;
                # a 401 response will trigger the full recovery path below.
                _LOGGER.warning("Could not proactively refresh NIU token: %s", err)

    def _recover_authentication(self, attempted_generation: int) -> None:
        with self._auth_lock:
            if self._auth_generation != attempted_generation:
                return

            if self.refresh_token:
                try:
                    self.refresh_access_token()
                    return
                except NiuApiError as err:
                    _LOGGER.warning("NIU token refresh failed; logging in again: %s", err)

            self._password_login()

    def _authenticated_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        self._ensure_access_token()
        attempted_generation = self._auth_generation
        response = self._perform_request(method, path, headers=headers, **kwargs)

        if response.status_code == 401:
            self._recover_authentication(attempted_generation)
            response = self._perform_request(method, path, headers=headers, **kwargs)
            if response.status_code == 401:
                raise NiuAuthenticationError(
                    "NIU rejected authentication after token recovery"
                )

        return self._parse_response(response)

    def _perform_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs,
    ):
        request_headers = {**(headers or {}), "token": self.token}
        request_method = requests.get if method == "GET" else requests.post
        try:
            return request_method(
                API_BASE_URL + path,
                headers=request_headers,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
        except requests.exceptions.RequestException as err:
            raise NiuConnectionError("Could not communicate with NIU API") from err

    @staticmethod
    def _parse_retry_after(response) -> int | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    def _parse_response(
        self, response, *, authentication_request: bool = False
    ) -> dict[str, Any]:
        status_code = response.status_code
        if status_code == 429:
            raise NiuRateLimitError(self._parse_retry_after(response))
        if status_code >= 500:
            raise NiuServerError(status_code)
        if status_code in (401, 403) or (
            authentication_request and status_code == 400
        ):
            raise NiuAuthenticationError(
                f"NIU rejected authentication with HTTP {status_code}"
            )
        if status_code >= 400:
            raise NiuHttpError(status_code)

        try:
            payload = response.json()
        except (ValueError, requests.exceptions.RequestException) as err:
            raise NiuResponseError("NIU returned invalid JSON") from err
        if not isinstance(payload, dict):
            raise NiuResponseError("NIU returned an unexpected JSON document")

        niu_status = payload.get("status", 0)
        if niu_status != 0:
            description = payload.get("desc") or "unknown NIU API error"
            if authentication_request:
                raise NiuAuthenticationError(
                    f"NIU authentication failed with status {niu_status}: {description}"
                )
            raise NiuResponseError(
                f"NIU API returned status {niu_status}: {description}",
                niu_status=niu_status,
            )
        return payload

    def _locale_headers(self) -> dict[str, str]:
        """Build the locale-aware headers shared by the GET endpoints."""
        is_chinese = self.language.startswith("zh")
        client_id = "Domestic" if is_chinese else "Overseas"
        return {
            "Accept-Language": self.language,
            "user-agent": f"manager/4.10.4 (android; IN2020 11);lang={self.language};clientIdentifier={client_id};timezone={self.timezone};model=IN2020;deviceName=IN2020;ostype=android",
        }

    def get_vehicles_info(self, path):
        return self._authenticated_request(
            "GET", path, headers=self._locale_headers()
        )

    def get_info(
        self,
        path,
    ):
        params = {"sn": self.sn}
        return self._authenticated_request(
            "GET", path, headers=self._locale_headers(), params=params
        )

    def post_info(
        self,
        path,
    ):
        headers = {"Accept-Language": self.language}
        return self._authenticated_request(
            "POST", path, headers=headers, data={"sn": self.sn}
        )

    def _track_headers(self) -> dict[str, str]:
        is_chinese = self.language.startswith("zh")
        client_id = "Domestic" if is_chinese else "Overseas"
        return {
            "Accept-Language": self.language,
            "User-Agent": f"manager/1.0.0 (identifier);clientIdentifier={client_id}",
        }

    def post_info_track(self, path, index: int = 0, pagesize: int = TRACK_PAGE_SIZE):
        return self._authenticated_request(
            "POST",
            path,
            headers=self._track_headers(),
            json={
                "index": str(index),
                "pagesize": pagesize,
                "sn": self.sn,
                # NIU's own apps repeat the token in the body.
                "token": self.token,
            },
        )

    def get_tracks(self, page: int = 0, pagesize: int = TRACK_PAGE_SIZE) -> list[dict]:
        """Return one page of rides, newest first.

        NIU's ``index`` is the page number (0 = newest rides), not an offset
        in rides: offsets skip whole stretches of the history.
        """
        return track_items(
            self.post_info_track(TRACK_LIST_API_URI, index=page, pagesize=pagesize)
        )

    def _post_json(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST a JSON body with the token repeated in it, as NIU's apps do."""
        payload = {"sn": self.sn, **(body or {})}
        self._ensure_access_token()
        payload["token"] = self.token
        return self._authenticated_request(
            "POST", path, headers=self._locale_headers(), json=payload
        )

    def get_cycling_statistics(self, sortby: int = 3) -> list[dict]:
        """Ridden kilometres per period: 1 = day, 2 = week, 3 = month."""
        result = self._authenticated_request(
            "GET",
            CYCLING_STATISTICS_API_URI,
            headers=self._locale_headers(),
            params={"sn": self.sn, "sortby": str(sortby)},
        )
        items = _data_items(result)
        periods = []
        for item in items:
            period = str(item.get("date") or "")
            start, _, end = period.partition("/")
            # NIU spells it "everdayMileage".
            km = _coordinate(item.get("everdayMileage", item.get("everydayMileage")))
            if len(start) != 8 or km is None:
                continue
            periods.append({"start": start, "end": end or start, "km": round(km, 2)})
        periods.sort(key=lambda period: period["start"])
        return periods

    def get_battery_health(self) -> dict[str, dict]:
        """Health report per battery compartment: grade history and faults."""
        result = self._authenticated_request(
            "GET",
            BATTERY_HEALTH_API_URI,
            headers=self._locale_headers(),
            params={"sn": self.sn},
        )
        try:
            batteries = result["data"]["batteries"]
        except (KeyError, TypeError):
            return {}
        if not isinstance(batteries, dict):
            return {}
        return {
            name: value for name, value in batteries.items() if isinstance(value, dict)
        }

    def get_battery_chart(self, battery: int = 1, page: int = 1) -> list[dict]:
        """Charge against distance for a battery (1 = compartment A).

        Returns points ``{"km": ..., "pct": ...}`` as NIU sends them
        (``m`` = kilometres, ``b`` = state of charge).
        """
        result = self._authenticated_request(
            "GET",
            BATTERY_CHART_API_URI,
            headers=self._locale_headers(),
            params={
                "sn": self.sn,
                "bmsId": str(battery),
                "page": str(page),
                "page_size": "B",
                "pageLength": "1",
            },
        )
        data = result.get("data") if isinstance(result, dict) else None
        items = data.get("items1") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        points = []
        for item in items:
            if not isinstance(item, dict):
                continue
            km, pct = _coordinate(item.get("m")), _coordinate(item.get("b"))
            if km is None or pct is None:
                continue
            points.append({"km": km, "pct": pct})
        return points

    def get_firmware(self) -> dict[str, Any] | None:
        result = self._post_json(FIRMWARE_API_URI)
        data = result.get("data")
        return data if isinstance(data, dict) else None

    def get_messages(self) -> list[dict]:
        """Return NIU's notification feed (alarms, ride summaries, ...)."""
        return _data_items(self.get_messages_raw())

    def get_messages_raw(self) -> dict[str, Any]:
        result = self._post_json(MESSAGE_LIST_API_URI)
        _LOGGER.debug("NIU message feed (json): %s", describe_shape(result))
        if not _data_items(result):
            # The Android app sends this one form-encoded; try that as well.
            self._ensure_access_token()
            form = self._authenticated_request(
                "POST",
                MESSAGE_LIST_API_URI,
                headers=self._locale_headers(),
                data={"sn": self.sn, "token": self.token},
            )
            _LOGGER.debug("NIU message feed (form): %s", describe_shape(form))
            if _data_items(form):
                return form
        return result

    def is_charging(self) -> bool | None:
        """Whether a charger is really connected.

        NIU's ``isCharging`` stays 1 while the ECU's internal battery tops
        itself up from the main pack, even while riding. ``charging_time``
        (seconds into the current charge session) only counts while an
        external charger is connected.
        """
        if self.dataMoto is None:
            return None
        charging_time = _coordinate(self.getDataMoto("charging_time"))
        if charging_time is not None:
            return charging_time > 0
        left_time = _coordinate(self.getDataMoto("leftTime"))
        if left_time is not None:
            return left_time > 0
        return None

    def getMotoTimestamp(self, id_field: str) -> datetime | None:
        """A millisecond epoch field of the motor data as an aware datetime."""
        value = _coordinate(self.getDataMoto(id_field))
        if not value or value < 0:
            return None
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    def _detail_header_variants(self) -> list[tuple[str, dict[str, str]]]:
        is_chinese = self.language.startswith("zh")
        client_id = "Domestic" if is_chinese else "Overseas"
        return [
            (
                "app",
                {
                    "Accept-Language": self.language,
                    "User-Agent": (
                        "manager/4.10.4 (iPhone; iOS 16.4.1);brand=Unknown;"
                        f"model=Unknown;clientIdentifier={client_id};lang={self.language}"
                    ),
                },
            ),
            ("locale", self._locale_headers()),
            ("track", self._track_headers()),
        ]

    def post_track_detail(self, track_id: str, track_date: str):
        """Fetch one track's GPS points the way NIU's own apps ask for them.

        For overseas accounts NIU currently answers with an empty point
        list; the camera then falls back to tracing the route from NIU's
        thumbnail.
        """
        _, headers = self._detail_header_variants()[0]
        return self._authenticated_request(
            "POST",
            TRACK_DETAIL_API_URI,
            headers=headers,
            json={
                "sn": self.sn,
                "trackId": track_id,
                "date": track_date,
                "token": self.token,
            },
        )

    def getDataBatA(self, id_field):
        try:
            return self.dataBat["data"]["batteries"]["compartmentA"][id_field]
        except (KeyError, TypeError):
            return None

    def hasSecondBattery(self):
        try:
            return "compartmentB" in self.dataBat["data"]["batteries"]
        except (KeyError, TypeError):
            return False

    def getDataBatB(self, id_field):
        try:
            return self.dataBat["data"]["batteries"]["compartmentB"][id_field]
        except (KeyError, TypeError):
            return None

    def getDataMoto(self, id_field):
        try:
            return self.dataMoto["data"][id_field]
        except (KeyError, TypeError):
            return None

    def getDataDist(self, id_field):
        try:
            return self.dataMoto["data"]["lastTrack"][id_field]
        except (KeyError, TypeError):
            return None

    def getBatteryCompartments(self) -> dict[str, dict]:
        """Return the raw battery compartments reported by the scooter."""
        try:
            compartments = self.dataBat["data"]["batteries"]
        except (KeyError, TypeError):
            return {}
        if not isinstance(compartments, dict):
            return {}
        return {
            name: data
            for name, data in compartments.items()
            if isinstance(data, dict)
        }

    def getDataPos(self, id_field):
        try:
            return self.dataMoto["data"]["postion"][id_field]
        except (KeyError, TypeError):
            return None

    def getDataOverall(self, id_field):
        try:
            return self.dataMotoInfo["data"][id_field]
        except (KeyError, TypeError):
            return None

    def getLatestTrack(self) -> dict | None:
        """Return the newest track from the track list, if any."""
        tracks = track_items(self.dataTrackInfo)
        return tracks[0] if tracks else None

    def getLatestTrackEnds(self) -> tuple[tuple[float, float], tuple[float, float]] | None:
        """Return the (lat, lng) start and end of the newest track."""
        track = self.getLatestTrack()
        if track is None:
            return None
        ends = []
        for key in ("startPoint", "lastPoint"):
            point = track.get(key)
            if not isinstance(point, dict):
                return None
            try:
                lat, lng = float(point["lat"]), float(point["lng"])
            except (KeyError, TypeError, ValueError):
                return None
            if lat == 0 and lng == 0:
                return None
            ends.append((lat, lng))
        return ends[0], ends[1]

    def getDataTrack(self, id_field):
        track = self.getLatestTrack()
        if track is None or id_field not in track:
            return None
        value = track[id_field]

        if id_field in ("startTime", "endTime"):
            try:
                return datetime.fromtimestamp(value / 1000).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            except (OSError, TypeError, ValueError):
                return None
        if id_field == "ridingtime":
            try:
                return strftime("%H:%M:%S", gmtime(value))
            except (OSError, TypeError, ValueError):
                return None
        if id_field == "track_thumb":
            if not isinstance(value, str):
                return None
            thumburl = value
            # Rewrite domestic CDN URLs to overseas; skip if already overseas
            if "app-api.niucache.com" in thumburl:
                thumburl = thumburl.replace(
                    "app-api.niucache.com", "app-api-fk.niu.com"
                )
            if "/track/thumb/" in thumburl and "/track/overseas/thumb/" not in thumburl:
                thumburl = thumburl.replace("/track/thumb/", "/track/overseas/thumb/")
            return thumburl
        return value

    def updateBat(self):
        result = self.get_info(MOTOR_BATTERY_API_URI)
        if result is not None:
            self.dataBat = result

    def updateMoto(self):
        result = self.get_info(MOTOR_INDEX_API_URI)
        if result is not None:
            self.dataMoto = result

    def updateMotoInfo(self):
        result = self.post_info(MOTOINFO_ALL_API_URI)
        if result is not None:
            self.dataMotoInfo = result

    def updateTrackInfo(self):
        result = self.post_info_track(TRACK_LIST_API_URI)
        if result is not None:
            self.dataTrackInfo = result

    def updateTrackDetail(self):
        """Fetch the GPS points of the newest track once per track."""
        track = self.getLatestTrack()
        if track is None:
            return
        track_id = track.get("trackId")
        track_date = track.get("date")
        if not track_id:
            return
        track_id = str(track_id)
        if track_id == self.track_points_id:
            return
        if (
            track_id == self._track_detail_failed_id
            and monotonic() < self._track_detail_retry_at
        ):
            return
        if not track_date and track.get("startTime"):
            try:
                track_date = datetime.fromtimestamp(
                    int(track["startTime"]) / 1000
                ).strftime("%Y%m%d")
            except (OSError, TypeError, ValueError):
                track_date = None

        _LOGGER.debug(
            "Fetching NIU track detail (date %s, track fields: %s)",
            track_date,
            ", ".join(sorted(track)),
        )
        try:
            result = self.post_track_detail(track_id, str(track_date or ""))
            points = parse_track_points(result)
            if len(points) < 2:
                _LOGGER.debug(
                    "NIU track detail response shape: %s", describe_shape(result)
                )
        except NiuRateLimitError:
            raise
        except NiuApiError as err:
            _LOGGER.warning("Could not fetch NIU track details: %s", err)
            points = []

        if len(points) < 2:
            _LOGGER.debug("NIU track %s has no usable GPS points", track_id)
            self._track_detail_failed_id = track_id
            self._track_detail_retry_at = monotonic() + TRACK_DETAIL_RETRY_INTERVAL
            return

        _LOGGER.debug("Fetched %d GPS points for NIU track", len(points))
        self.track_points = points
        self.track_points_id = track_id
        self._track_detail_failed_id = None


def _data_items(payload) -> list[dict]:
    """The item list of a response whose ``data`` is a list or ``{items: [...]}``."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def track_items(payload) -> list[dict]:
    """The rides in a /v5/track/list/v2 response, newest first."""
    return _data_items(payload)


def _coordinate(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


LAT_KEYS = ("lat", "latitude", "la")
LNG_KEYS = ("lng", "lon", "longitude", "lo")
TIME_KEYS = ("date", "time", "timestamp", "gpsTime", "gps_time", "t")


def describe_shape(value, depth: int = 0) -> str:
    """Describe the structure of a JSON value without its contents."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        inner = ", ".join(
            f"{key}: {describe_shape(item, depth + 1)}"
            for key, item in list(value.items())[:15]
        )
        return "{" + inner + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[{len(value)} x {describe_shape(value[0], depth + 1)}]"
    return type(value).__name__


def _first(item: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in item:
            return item[key]
    return None


def _point_lists(value, depth: int = 0):
    """Yield every list of dicts that look like GPS points, at any depth."""
    if depth > 5:
        return
    if isinstance(value, list):
        dicts = [item for item in value if isinstance(item, dict)]
        if dicts and any(
            _first(item, LAT_KEYS) is not None and _first(item, LNG_KEYS) is not None
            for item in dicts[:5]
        ):
            yield dicts
        for item in value[:5]:
            yield from _point_lists(item, depth + 1)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _point_lists(item, depth + 1)


def parse_track_points(payload) -> list[tuple[float, float]]:
    """Turn a /v5/track/detail response into (lat, lng) points in ride order."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("trackItems"), list):
        candidates = [data["trackItems"]]
    else:
        candidates = list(_point_lists(data))
    if not candidates:
        return []
    items = max(candidates, key=len)

    timed: list[tuple[float, int, float, float]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        lat = _coordinate(_first(item, LAT_KEYS))
        lng = _coordinate(_first(item, LNG_KEYS))
        if lat is None or lng is None:
            continue
        if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
            continue
        stamp = _coordinate(_first(item, TIME_KEYS))
        timed.append((stamp if stamp is not None else float("nan"), index, lat, lng))

    if not timed:
        return []
    if all(entry[0] == entry[0] for entry in timed):  # every point has a timestamp
        timed.sort(key=lambda entry: (entry[0], entry[1]))
    else:
        # NIU lists the end point first.
        timed.reverse()
    return [(lat, lng) for _, _, lat, lng in timed]
