import io
import zipfile
from datetime import datetime

from fastapi.testclient import TestClient

import main


client = TestClient(main.app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_optional_adapter_key_protects_garmin_routes(monkeypatch):
    monkeypatch.setattr(main, "adapter_api_key", "shared-secret")
    denied = client.post("/api/garmin/workouts/preview", json={})
    allowed = client.post(
        "/api/garmin/workouts/preview",
        headers={"X-Adapter-Key": "shared-secret"},
        json={},
    )
    assert denied.status_code == 401
    assert allowed.status_code == 422
    assert client.get("/health").status_code == 200


def test_request_rejects_limit_outside_supported_range():
    response = client.post(
        "/api/garmin/activities",
        json={"email": "runner@example.test", "password": "secret", "limit": 0},
    )
    assert response.status_code == 422


def test_extract_fit_bytes_recognizes_native_fit_header():
    raw_fit = b"\x0e\x00\x00\x00\x00\x00\x00\x00.FITpayload"
    assert main.extract_fit_bytes(raw_fit) == raw_fit


def test_unit_conversions_preserve_zero_values():
    assert main.meters_to_km(0) == 0
    assert main.speed_to_kmh(0) == 0
    assert main.speed_to_pace_s_per_km(0) is None


def test_normalize_datetime_accepts_garmin_iso_fraction():
    assert main.normalize_datetime("2026-09-21T17:48:18.0") == "2026-09-21 17:48:18"


def test_metadata_accepts_activity_type_dto_used_by_activity_details():
    metadata = main.map_activity_metadata({
        "activityId": 100,
        "activityName": "Teste 3km",
        "activityTypeDTO": {
            "typeKey": "running",
            "parentTypeKey": "generic",
        },
    })

    assert metadata.sport == "running"
    assert metadata.sub_sport == "generic"


def test_discovery_metadata_ignores_decimal_detail_fields():
    metadata = main.map_discovery_metadata({
        "activityId": 100,
        "activityName": "Corrida",
        "startTimeLocal": "2026-09-21T17:48:18.0",
        "averageRunningCadenceInStepsPerMinute": 84.7,
        "elevationGain": 12.35,
    })

    assert metadata.activity_id == 100
    assert metadata.started_at == "2026-09-21 17:48:18"


def test_extract_fit_bytes_reads_fit_from_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("activity.fit", b"fit-content")
    assert main.extract_fit_bytes(buffer.getvalue()) == b"fit-content"


def test_fetch_activities_maps_summary_and_fit_details(monkeypatch):
    class FakeGarmin:
        ActivityDownloadFormat = main.Garmin.ActivityDownloadFormat

        def __init__(self, email, password):
            assert email == "runner@example.test"
            assert password == "secret"

        def login(self, *_args):
            return None

        def get_activities(self, start, limit):
            assert (start, limit) == (0, 1)
            return [{
                "activityId": 99,
                "activityName": "Teste 3km",
                "distance": 3000.0,
                "duration": 720.0,
                "startTimeLocal": "2026-09-18 06:30:00",
                "averageHR": 170,
                "averageSpeed": 4.16,
                "maxSpeed": 5.0,
                "maxHR": 185,
                "activityType": {"typeKey": "running"},
            }]

        def download_activity(self, activity_id, dl_fmt):
            assert activity_id == 99
            return b"\x0e\x00\x00\x00\x00\x00\x00\x00.FITpayload"

    lap = main.LapModel(lap_number=1, duration_s=720.0, distance_km=3.0)
    record = main.ActivityRecordModel(ts="2026-09-18 06:30:00", elapsed_s=0)
    monkeypatch.setattr(main, "Garmin", FakeGarmin)
    monkeypatch.setattr(main, "process_fit_data", lambda _: ([lap], [record]))

    response = client.post(
        "/api/garmin/activities",
        json={"email": "runner@example.test", "password": "secret", "limit": 1},
    )

    assert response.status_code == 200
    activity = response.json()[0]
    assert activity["activity_id"] == 99
    assert activity["is_vdot_test"] is True
    assert activity["lap_count"] == 1
    assert activity["record_count"] == 1
    assert activity["max_speed_kmh"] == 18.0


def test_discovery_returns_metadata_without_downloading_fit(monkeypatch):
    calls = {"downloads": 0}

    class FakeGarmin:
        def __init__(self, email, password):
            assert (email, password) == ("runner@example.test", "secret")

        def login(self, *_args):
            return None

        def get_activities(self, start, limit):
            assert (start, limit) == (0, 10)
            return [
                {
                    "activityId": 100,
                    "activityName": "Corrida nova",
                    "startTimeLocal": "2026-09-18 07:00:00",
                    "activityType": {"typeKey": "running"},
                },
                {
                    "activityId": 99,
                    "activityName": "Corrida antiga",
                    "startTimeLocal": "2026-09-10 07:00:00",
                    "activityType": {"typeKey": "running"},
                },
            ]

        def download_activity(self, *_args, **_kwargs):
            calls["downloads"] += 1

    monkeypatch.setattr(main, "Garmin", FakeGarmin)
    response = client.post(
        "/api/garmin/activities/discover",
        json={
            "email": "runner@example.test",
            "password": "secret",
            "limit": 10,
            "since": datetime(2026, 9, 17).isoformat(),
        },
    )

    assert response.status_code == 200
    assert [item["activity_id"] for item in response.json()] == [100]
    assert calls["downloads"] == 0


def test_discovery_skips_only_item_without_activity_id(monkeypatch):
    class FakeGarmin:
        def __init__(self, _email, _password):
            pass

        def login(self, *_args):
            return None

        def get_activities(self, _start, _limit):
            return [
                {"activityName": "Entrada incompleta"},
                {
                    "activityId": 101,
                    "activityName": "Corrida válida",
                    "startTimeLocal": "2026-09-21 07:00:00",
                },
            ]

    monkeypatch.setattr(main, "Garmin", FakeGarmin)
    response = client.post(
        "/api/garmin/activities/discover",
        json={"email": "runner@example.test", "password": "secret", "limit": 10},
    )

    assert response.status_code == 200
    assert [item["activity_id"] for item in response.json()] == [101]


def test_download_fetches_and_processes_only_requested_activity(monkeypatch):
    class FakeGarmin:
        ActivityDownloadFormat = main.Garmin.ActivityDownloadFormat

        def __init__(self, email, password):
            assert (email, password) == ("runner@example.test", "secret")

        def login(self, *_args):
            return None

        def get_activity(self, activity_id):
            assert activity_id == 100
            return {
                "activityId": 100,
                "activityName": "Corrida nova",
                "startTimeLocal": "2026-09-18 07:00:00",
                "activityType": {"typeKey": "running"},
            }

        def download_activity(self, activity_id, dl_fmt):
            assert activity_id == 100
            return b"\x0e\x00\x00\x00\x00\x00\x00\x00.FITpayload"

    monkeypatch.setattr(main, "Garmin", FakeGarmin)
    monkeypatch.setattr(main, "process_fit_data", lambda _: ([], []))
    response = client.post(
        "/api/garmin/activities/100/download",
        json={"email": "runner@example.test", "password": "secret"},
    )

    assert response.status_code == 200
    assert response.json()["activity_id"] == 100
    assert response.json()["record_count"] == 0


def test_download_reports_missing_activity(monkeypatch):
    class FakeGarmin:
        def __init__(self, _email, _password):
            pass

        def login(self, *_args):
            return None

        def get_activity(self, _activity_id):
            return None

    monkeypatch.setattr(main, "Garmin", FakeGarmin)
    response = client.post(
        "/api/garmin/activities/404/download",
        json={"email": "runner@example.test", "password": "secret"},
    )

    assert response.status_code == 404


def test_discovery_classifies_authentication_failure(monkeypatch):
    class FakeGarmin:
        def __init__(self, _email, _password):
            pass

        def login(self, *_args):
            raise main.GarminConnectAuthenticationError("secret upstream detail")

    monkeypatch.setattr(main, "Garmin", FakeGarmin)
    response = client.post(
        "/api/garmin/activities/discover",
        json={"email": "runner@example.test", "password": "wrong", "limit": 10},
    )

    assert response.status_code == 401
    assert "credenciais" in response.json()["detail"]
    assert "secret upstream detail" not in response.text


def test_connect_uses_stable_account_specific_tokenstore(monkeypatch, tmp_path):
    captured = []

    class FakeGarmin:
        def __init__(self, _email, _password):
            pass

        def login(self, tokenstore):
            captured.append(tokenstore)

    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr(main, "Garmin", FakeGarmin)

    main.connect_garmin("Runner@Example.Test", "secret")
    main.connect_garmin("runner@example.test", "secret")

    assert captured[0] == captured[1]
    assert "runner@example.test" not in captured[0]


def test_download_uses_first_fit_record_when_summary_has_no_start_time(monkeypatch):
    class FakeGarmin:
        ActivityDownloadFormat = main.Garmin.ActivityDownloadFormat

        def __init__(self, _email, _password):
            pass

        def login(self, *_args):
            return None

        def get_activity(self, activity_id):
            return {
                "activityId": activity_id,
                "activityName": "Teste 3km",
                "activityType": {"typeKey": "running"},
            }

        def download_activity(self, _activity_id, dl_fmt):
            assert dl_fmt == main.Garmin.ActivityDownloadFormat.ORIGINAL
            return b"\x0e\x00\x00\x00\x00\x00\x00\x00.FITpayload"

    record = main.ActivityRecordModel(ts="2026-09-21 07:30:00", elapsed_s=0)
    monkeypatch.setattr(main, "Garmin", FakeGarmin)
    monkeypatch.setattr(main, "process_fit_data", lambda _: ([], [record]))

    response = client.post(
        "/api/garmin/activities/24448621354/download",
        json={"email": "runner@example.test", "password": "secret"},
    )

    assert response.status_code == 200
    assert response.json()["started_at"] == "2026-09-21 07:30:00"


def test_download_uses_fit_sport_when_activity_detail_omits_type(monkeypatch):
    class FakeGarmin:
        ActivityDownloadFormat = main.Garmin.ActivityDownloadFormat

        def __init__(self, _email, _password):
            pass

        def login(self, *_args):
            return None

        def get_activity(self, activity_id):
            return {
                "activityId": activity_id,
                "activityName": "Teste 3km",
                "startTimeLocal": "2026-09-21 20:48:18",
            }

        def download_activity(self, _activity_id, dl_fmt):
            assert dl_fmt == main.Garmin.ActivityDownloadFormat.ORIGINAL
            return b"\x0e\x00\x00\x00\x00\x00\x00\x00.FITpayload"

    monkeypatch.setattr(main, "Garmin", FakeGarmin)
    monkeypatch.setattr(main, "process_fit_data", lambda _: ([], []))
    monkeypatch.setattr(main, "fit_sport_metadata", lambda _: ("running", "generic"))

    response = client.post(
        "/api/garmin/activities/24448621354/download",
        json={"email": "runner@example.test", "password": "secret"},
    )

    assert response.status_code == 200
    assert response.json()["sport"] == "running"
    assert response.json()["sub_sport"] == "generic"
