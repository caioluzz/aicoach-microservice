from datetime import date

import main
from workout_delivery import (
    CancelWorkoutRequest,
    ConfirmWorkoutRequest,
    DeliverWorkoutRequest,
    GarminWorkoutService,
    UpdateWorkoutRequest,
    WorkoutRequest,
    compile_workout,
    workout_hash,
)


def sample_workout():
    return WorkoutRequest.model_validate({
        "schemaVersion": "workout.v1",
        "name": "Intervalado T",
        "scheduledDate": "2026-10-06",
        "blocks": [
            {
                "order": 1,
                "repetitions": 1,
                "steps": [{
                    "order": 1,
                    "kind": "WARMUP",
                    "durationType": "TIME",
                    "durationValue": 600,
                    "targetZone": "E_PACE",
                    "targetPaceFastestSecondsPerKm": 330,
                    "targetPaceSlowestSecondsPerKm": 360,
                    "instruction": "Leve",
                }],
            },
            {
                "order": 2,
                "repetitions": 4,
                "steps": [
                    {
                        "order": 1,
                        "kind": "WORK",
                        "durationType": "DISTANCE",
                        "durationValue": 1000,
                        "targetZone": "T_PACE",
                        "targetPaceFastestSecondsPerKm": 240,
                        "targetPaceSlowestSecondsPerKm": 250,
                    },
                    {
                        "order": 2,
                        "kind": "RECOVERY",
                        "durationType": "TIME",
                        "durationValue": 90,
                        "targetZone": "REST",
                    },
                ],
            },
            {
                "order": 3,
                "repetitions": 1,
                "steps": [{
                    "order": 1,
                    "kind": "COOLDOWN",
                    "durationType": "TIME",
                    "durationValue": 600,
                    "targetZone": "E_PACE",
                }],
            },
        ],
    })


def credentials():
    return {"email": "runner@example.test", "password": "secret"}


def test_compiler_maps_steps_pace_and_repetitions():
    workout = sample_workout()
    digest = workout_hash(workout)
    payload = compile_workout(workout, digest)

    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert payload["workoutName"].endswith(f"[ARC-{digest[:12]}]")
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[0]["endCondition"]["conditionTypeKey"] == "time"
    assert steps[0]["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert steps[1]["type"] == "RepeatGroupDTO"
    assert steps[1]["numberOfIterations"] == 4
    assert steps[1]["workoutSteps"][0]["endCondition"]["conditionTypeKey"] == "distance"
    assert steps[1]["workoutSteps"][1]["stepType"]["stepTypeKey"] == "recovery"
    assert steps[2]["stepType"]["stepTypeKey"] == "cooldown"


def test_hash_is_deterministic_and_does_not_include_credentials():
    first = workout_hash(sample_workout())
    second = workout_hash(sample_workout())
    assert first == second
    assert len(first) == 64
    assert "secret" not in first


class FakeGarmin:
    workouts = []
    scheduled = []
    calls = []

    def __init__(self, email, password):
        assert email == "runner@example.test"
        assert password == "secret"

    @classmethod
    def reset(cls):
        cls.workouts = []
        cls.scheduled = []
        cls.calls = []

    def login(self):
        self.calls.append("login")

    def get_workouts(self, start, limit):
        return self.workouts

    def upload_workout(self, payload):
        item = payload | {"workoutId": 41}
        self.workouts.append(item)
        self.calls.append("upload")
        return item

    def schedule_workout(self, workout_id, scheduled_date):
        item = {
            "scheduledWorkoutId": 81 + len(self.scheduled),
            "workoutId": workout_id,
            "date": scheduled_date,
        }
        self.scheduled.append(item)
        self.calls.append("schedule")
        return item

    def update_workout(self, workout_id, payload):
        self.calls.append(("update", workout_id, payload))
        return payload | {"workoutId": workout_id}

    def unschedule_workout(self, scheduled_workout_id):
        self.calls.append(("unschedule", scheduled_workout_id))
        self.scheduled = [
            item for item in self.scheduled
            if item["scheduledWorkoutId"] != scheduled_workout_id
        ]
        type(self).scheduled = self.scheduled

    def delete_workout(self, workout_id):
        self.calls.append(("delete", workout_id))

    def get_scheduled_workouts(self, year, month):
        assert (year, month) == (2026, 10)
        return self.scheduled


def delivery_request():
    workout = sample_workout()
    return DeliverWorkoutRequest.model_validate({
        "credentials": credentials(),
        "idempotencyKey": workout_hash(workout),
        "workout": workout.model_dump(by_alias=True),
    })


def test_service_uploads_and_schedules_then_reuses_workout():
    FakeGarmin.reset()
    service = GarminWorkoutService(FakeGarmin, retry_delay_seconds=0)

    first = service.deliver(delivery_request())
    second = service.deliver(delivery_request())

    assert first["workoutId"] == 41
    assert first["scheduledWorkoutId"] == 81
    assert first["reusedWorkout"] is False
    assert second["reusedWorkout"] is True
    assert FakeGarmin.calls.count("upload") == 1
    assert FakeGarmin.calls.count("schedule") == 1


def test_service_updates_reschedules_confirms_and_cancels():
    FakeGarmin.reset()
    service = GarminWorkoutService(FakeGarmin, retry_delay_seconds=0)
    delivered = service.deliver(delivery_request())
    update = UpdateWorkoutRequest.model_validate(
        delivery_request().model_dump(by_alias=True) | {"scheduledWorkoutId": 81}
    )

    updated = service.update(41, update)
    confirmation = service.confirm(ConfirmWorkoutRequest.model_validate({
        "credentials": credentials(),
        "workoutId": 41,
        "scheduledWorkoutId": updated["scheduledWorkoutId"],
        "scheduledDate": "2026-10-06",
        "idempotencyKey": delivery_request().idempotency_key,
    }))
    cancelled = service.cancel(41, CancelWorkoutRequest.model_validate({
        "credentials": credentials(),
        "scheduledWorkoutId": updated["scheduledWorkoutId"],
    }))

    assert delivered["scheduledDate"] == date(2026, 10, 6)
    assert updated["scheduledWorkoutId"] == 81
    assert confirmation["confirmed"] is True
    assert cancelled == {"cancelled": True, "workoutId": 41}
    assert ("unschedule", 81) in FakeGarmin.calls
    assert ("delete", 41) in FakeGarmin.calls


def test_http_endpoints_use_service_without_leaking_failure(monkeypatch):
    class FakeService:
        def deliver(self, request):
            return {"workoutId": 1, "scheduledWorkoutId": 2}

    monkeypatch.setattr(main, "workout_service", FakeService())
    request = delivery_request().model_dump(mode="json", by_alias=True)
    response = main.app.router.routes
    from fastapi.testclient import TestClient

    api = TestClient(main.app)
    delivered = api.post("/api/garmin/workouts/deliver", json=request)
    assert delivered.status_code == 200
    assert delivered.json()["workoutId"] == 1


def test_preview_rejects_unsupported_schema():
    from fastapi.testclient import TestClient

    api = TestClient(main.app)
    payload = sample_workout().model_dump(mode="json", by_alias=True)
    payload["schemaVersion"] = "workout.v2"
    response = api.post("/api/garmin/workouts/preview", json=payload)
    assert response.status_code == 422
