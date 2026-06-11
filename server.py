import os
import json
import secrets
import time
from datetime import date as _date, timedelta
from pathlib import Path

import keyring
from keyring.backend import KeyringBackend


class _EnvKeyring(KeyringBackend):
    """Read Zepp credentials from env vars instead of system keyring."""
    priority = 100

    def get_password(self, service, username):
        if service != "zepp-life-mcp":
            return None
        return {
            "zepp_auth_token": os.environ.get("ZEPP_APP_TOKEN"),
            "zepp_auth_user_id": os.environ.get("ZEPP_USER_ID"),
        }.get(username)

    def set_password(self, service, username, password):
        pass

    def delete_password(self, service, username):
        pass


keyring.set_keyring(_EnvKeyring())

# Write config.json before importing zepp modules
_config_dir = Path.home() / ".config" / "zepp-life-mcp"
_config_dir.mkdir(parents=True, exist_ok=True)
_data_dir = Path(os.environ.get("ZEPP_DATA_DIR", "/data"))
_data_dir.mkdir(parents=True, exist_ok=True)

(_config_dir / "config.json").write_text(json.dumps({
    "mode": "cloud_session",
    "region": os.environ.get("ZEPP_REGION", "eu"),
    "timezone": os.environ.get("ZEPP_TIMEZONE", "UTC"),
    "database_path": str(_data_dir / "zepp.db"),
    "logs_path": str(_data_dir / "zepp.log"),
    "export_path": None,
    "auto_sync_on_start": False,
    "stale_after_minutes": 60,
    "store_raw_payloads": True,
    "default_lookback_days": int(os.environ.get("SYNC_LOOKBACK_DAYS", "30")),
}, indent=2))

from zepp_life_mcp.config import load_config
from zepp_life_mcp.auth import load_token
from zepp_life_mcp.adapters.cloud_session import CloudSessionAdapter
from zepp_life_mcp.storage import Database
from zepp_life_mcp.services.sync_service import SyncService
from zepp_life_mcp.services.query_service import QueryService
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationCode,
    RefreshToken,
    AccessToken,
    AuthorizationParams,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

_cfg = load_config()
_db = Database(_cfg.database_path)
_app_token, _user_id = load_token()
_adapter = CloudSessionAdapter(app_token=_app_token, user_id=_user_id, region=_cfg.region)
_sync_svc = SyncService(_adapter, _db)
_query_svc = QueryService(_db, _user_id)

port = int(os.environ.get("PORT", "8080"))

_raw_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
SERVER_URL = (
    f"https://{_raw_domain}"
    if _raw_domain and not _raw_domain.startswith("http")
    else _raw_domain or f"http://localhost:{port}"
)


class _BypassOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Single-user bypass OAuth. Auto-approves all authorization requests.
    Credentials are stored server-side in env vars — no user login needed."""

    def __init__(self):
        self._clients: dict = {}
        self._auth_codes: dict = {}
        self._access_tokens: dict = {}
        self._refresh_tokens: dict = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code and code.expires_at > time.time():
            return code
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)
        access_token = secrets.token_urlsafe(32)
        refresh_token_str = secrets.token_urlsafe(32)
        expires_in = 365 * 24 * 3600
        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + expires_in,
        )
        self._refresh_tokens[refresh_token_str] = RefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
        )
        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=expires_in,
            refresh_token=refresh_token_str,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return self._refresh_tokens.get(refresh_token)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list
    ) -> OAuthToken:
        access_token = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        expires_in = 365 * 24 * 3600
        effective_scopes = scopes or refresh_token.scopes
        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=effective_scopes,
            expires_at=int(time.time()) + expires_in,
        )
        self._refresh_tokens.pop(refresh_token.token, None)
        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=effective_scopes,
        )
        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=expires_in,
            refresh_token=new_refresh,
            scope=" ".join(effective_scopes) if effective_scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        at = self._access_tokens.get(token)
        if at and (at.expires_at is None or at.expires_at > time.time()):
            return at
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        t = getattr(token, "token", None)
        if t:
            self._access_tokens.pop(t, None)
            self._refresh_tokens.pop(t, None)


_oauth = _BypassOAuthProvider()

app = FastMCP(
    "Zepp Life MCP",
    host="0.0.0.0",
    port=port,
    auth_server_provider=_oauth,
    auth=AuthSettings(
        issuer_url=SERVER_URL,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        resource_server_url=SERVER_URL,
    ),
)


@app.tool()
async def sync_data(
    data_types: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    force_full_sync: bool = False,
) -> dict:
    """Sync health data from Zepp Life cloud.
    data_types: daily_activity | sleep | workouts | body_measurements | heart_rate
    Dates: YYYY-MM-DD format. Defaults to last 30 days."""
    adapter = CloudSessionAdapter(app_token=_app_token, user_id=_user_id, region=_cfg.region)
    if not await adapter.connect():
        return {"error": "Cannot connect to Zepp Life API. Verify ZEPP_APP_TOKEN is valid."}
    sync_svc = SyncService(adapter, _db)
    types = data_types or ["daily_activity", "sleep", "workouts", "body_measurements", "heart_rate"]
    results = []
    for dt in types:
        r = await sync_svc.sync_data_type(
            dt, start_date=start_date, end_date=end_date, force_full=force_full_sync
        )
        results.append(r)
    return {"synced": results}


@app.tool()
def get_connection_status() -> dict:
    """Get connection and data availability status."""
    try:
        coverage = _query_svc.get_data_coverage()
        return {
            "mode": "cloud_session",
            "app_token_set": bool(_app_token),
            "user_id": _user_id,
            "region": _cfg.region,
            "data_coverage": coverage,
        }
    except Exception as e:
        return {"error": str(e)}


@app.tool()
def get_profile() -> dict:
    """Get Zepp Life user profile info."""
    return {
        "user_id": _user_id,
        "region": _cfg.region,
        "timezone": _cfg.timezone,
    }


@app.tool()
def get_daily_summary(
    for_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Get daily activity summary (steps, calories, distance, active minutes).
    Provide for_date (YYYY-MM-DD) for a single day, or start_date/end_date for a range."""
    if for_date:
        start_date = end_date = for_date
    elif not start_date or not end_date:
        today = str(_date.today())
        start_date = end_date = today
    return {"summaries": _query_svc.get_daily_summaries(start_date, end_date)}


@app.tool()
def query_metric_series(
    metric: str,
    start_date: str,
    end_date: str,
    granularity: str = "day",
    aggregation: str = "sum",
) -> dict:
    """Query a metric as a time series.
    metric: steps | distance_m | active_kcal | weight_kg | sleep_minutes
    granularity: day | week | month
    aggregation: sum | avg | min | max | latest"""
    return {"series": _query_svc.get_metric_series(metric, start_date, end_date, granularity, aggregation)}


@app.tool()
def query_sleep(
    start_date: str,
    end_date: str,
    include_naps: bool = True,
    include_stages: bool = True,
) -> dict:
    """Query sleep sessions with duration, quality score, and optionally sleep stage breakdown."""
    sessions = _query_svc.get_sleep_sessions(start_date, end_date, include_naps=include_naps)
    if not include_stages:
        for s in sessions:
            s.pop("stages", None)
    return {"sessions": sessions}


@app.tool()
def query_workouts(
    start_date: str,
    end_date: str,
    activity_types: list[str] | None = None,
    min_duration_minutes: int | None = None,
    min_distance_km: float | None = None,
) -> dict:
    """Query workout sessions. Filter by activity type, minimum duration (minutes), or minimum distance (km)."""
    return {"workouts": _query_svc.get_workouts(
        start_date, end_date,
        activity_types=activity_types,
        min_duration=min_duration_minutes,
        min_distance_km=min_distance_km,
    )}


@app.tool()
def query_heart_rate(
    start_date: str,
    end_date: str,
    sample_type: str | None = None,
    limit: int | None = None,
) -> dict:
    """Query heart rate samples.
    sample_type: resting | active | passive | workout"""
    return {"samples": _query_svc.get_heart_rate_samples(
        start_date, end_date, sample_type=sample_type, limit=limit
    )}


@app.tool()
def query_body_measurements(
    start_date: str | None = None,
    end_date: str | None = None,
    metrics: list[str] | None = None,
    latest_only: bool = False,
) -> dict:
    """Query body composition from Xiaomi Mi Body Composition Scale via Zepp Life.
    metrics: weight_kg | bmi | body_fat_pct | muscle_mass_kg | water_pct
    Set latest_only=true to return only the most recent reading."""
    today = _date.today()
    if not end_date:
        end_date = str(today)
    if not start_date:
        start_date = str(today - timedelta(days=_cfg.default_lookback_days))
    measurements = _query_svc.get_body_measurements(start_date, end_date, metrics=metrics)
    if latest_only and measurements:
        measurements = [measurements[-1]]
    return {"measurements": measurements}


@app.tool()
def get_data_coverage(data_types: list[str] | None = None) -> dict:
    """Get data availability — which dates have records per health metric category."""
    return {"coverage": _query_svc.get_data_coverage(data_types=data_types)}


def main():
    app.run(transport="sse")


if __name__ == "__main__":
    main()
