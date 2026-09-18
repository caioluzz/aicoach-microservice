import io
import zipfile

from fastapi.testclient import TestClient

import main


client = TestClient(main.app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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

        def login(self):
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
