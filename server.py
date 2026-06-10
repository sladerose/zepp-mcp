import os
import json
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

_cfg = load_config()
_db = Database(_cfg.database_path)
_app_token, _user_id = load_token()
_adapter = CloudSessionAdapter(app_token=_app_token, user_id=_user_id, region=_cfg.region)
_sync_svc = SyncService(_adapter, _db)
_query_svc = QueryService(_db, _user_id)

port = int(os.environ.get("PORT", "8080"))
app = FastMCP("Zepp Life MCP", host="0.0.0.0", port=port)


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
    if not await _adapter.connect():
        return {"error": "Cannot connect to Zepp Life API. Verify ZEPP_APP_TOKEN is valid."}
    types = data_types or ["daily_activity", "sleep", "workouts", "body_measurements", "heart_rate"]
    results = []
    for dt in types:
        r = await _sync_svc.sync_data_type(
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
