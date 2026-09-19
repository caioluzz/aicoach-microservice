import hashlib
import json
import time
from datetime import date
from typing import Any, Callable, Literal

from garminconnect import Garmin
from garminconnect.exceptions import (
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator
from requests import ConnectionError as RequestsConnectionError
from requests import Timeout as RequestsTimeout


class WorkoutStepRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    order: int = Field(ge=1)
    kind: Literal["WARMUP", "WORK", "RECOVERY", "COOLDOWN"]
    duration_type: Literal["TIME", "DISTANCE"] = Field(alias="durationType")
    duration_value: int = Field(alias="durationValue", gt=0)
    target_zone: str | None = Field(default=None, alias="targetZone")
    target_pace_fastest_seconds_per_km: int | None = Field(
        default=None, alias="targetPaceFastestSecondsPerKm", gt=0
    )
    target_pace_slowest_seconds_per_km: int | None = Field(
        default=None, alias="targetPaceSlowestSecondsPerKm", gt=0
    )
    instruction: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_pace_range(self):
        fastest = self.target_pace_fastest_seconds_per_km
        slowest = self.target_pace_slowest_seconds_per_km
        if (fastest is None) != (slowest is None):
            raise ValueError("pace target requires both bounds")
        if fastest is not None and fastest > slowest:
            raise ValueError("fastest pace must not be slower than slowest pace")
        return self


class WorkoutBlockRequest(BaseModel):
    order: int = Field(ge=1)
    repetitions: int = Field(ge=1, le=99)
    steps: list[WorkoutStepRequest] = Field(min_length=1)


class WorkoutRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: Literal["workout.v1"] = Field(alias="schemaVersion")
    name: str = Field(min_length=1, max_length=200)
    scheduled_date: date = Field(alias="scheduledDate")
    blocks: list[WorkoutBlockRequest] = Field(min_length=1)


class GarminCredentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=500)


class DeliverWorkoutRequest(BaseModel):
    credentials: GarminCredentials
    idempotency_key: str = Field(alias="idempotencyKey", pattern=r"^[a-f0-9]{64}$")
    workout: WorkoutRequest


class UpdateWorkoutRequest(DeliverWorkoutRequest):
    scheduled_workout_id: int | None = Field(default=None, alias="scheduledWorkoutId", gt=0)


class ConfirmWorkoutRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    credentials: GarminCredentials
    workout_id: int = Field(alias="workoutId", gt=0)
    scheduled_workout_id: int | None = Field(default=None, alias="scheduledWorkoutId", gt=0)
    scheduled_date: date = Field(alias="scheduledDate")
    idempotency_key: str = Field(alias="idempotencyKey", pattern=r"^[a-f0-9]{64}$")


class CancelWorkoutRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    credentials: GarminCredentials
    scheduled_workout_id: int | None = Field(default=None, alias="scheduledWorkoutId", gt=0)


def workout_hash(workout: WorkoutRequest) -> str:
    canonical = json.dumps(
        workout.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _target(step: WorkoutStepRequest) -> dict[str, Any]:
    fastest = step.target_pace_fastest_seconds_per_km
    slowest = step.target_pace_slowest_seconds_per_km
    if fastest is None or slowest is None or step.target_zone == "REST":
        return {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1,
        }
    return {
        "workoutTargetTypeId": 6,
        "workoutTargetTypeKey": "pace.zone",
        "displayOrder": 6,
        "targetValueOne": round(1000 / slowest, 4),
        "targetValueTwo": round(1000 / fastest, 4),
        "zoneNumber": None,
    }


def _compile_step(step: WorkoutStepRequest, step_order: int) -> dict[str, Any]:
    step_types = {
        "WARMUP": (1, "warmup", 1),
        "WORK": (3, "interval", 3),
        "RECOVERY": (4, "recovery", 4),
        "COOLDOWN": (2, "cooldown", 2),
    }
    condition_types = {
        "TIME": (2, "time", 2),
        "DISTANCE": (3, "distance", 3),
    }
    step_type_id, step_type_key, step_display = step_types[step.kind]
    condition_id, condition_key, condition_display = condition_types[step.duration_type]
    result = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": {
            "stepTypeId": step_type_id,
            "stepTypeKey": step_type_key,
            "displayOrder": step_display,
        },
        "endCondition": {
            "conditionTypeId": condition_id,
            "conditionTypeKey": condition_key,
            "displayOrder": condition_display,
            "displayable": True,
        },
        "endConditionValue": float(step.duration_value),
        "targetType": _target(step),
    }
    if step.instruction:
        result["description"] = step.instruction
    return result


def compile_workout(workout: WorkoutRequest, idempotency_key: str) -> dict[str, Any]:
    compiled_steps: list[dict[str, Any]] = []
    order = 1
    estimated_seconds = 0
    for block in sorted(workout.blocks, key=lambda item: item.order):
        children = []
        for step in sorted(block.steps, key=lambda item: item.order):
            children.append(_compile_step(step, order + (1 if block.repetitions > 1 else 0)))
            order += 1
            if step.duration_type == "TIME":
                estimated_seconds += step.duration_value * block.repetitions
            elif step.target_pace_fastest_seconds_per_km and step.target_pace_slowest_seconds_per_km:
                average_pace = (
                    step.target_pace_fastest_seconds_per_km
                    + step.target_pace_slowest_seconds_per_km
                ) / 2
                estimated_seconds += round(step.duration_value * average_pace / 1000) * block.repetitions

        if block.repetitions == 1:
            compiled_steps.extend(children)
        else:
            repeat_order = children[0]["stepOrder"] - 1
            compiled_steps.append({
                "type": "RepeatGroupDTO",
                "stepOrder": repeat_order,
                "stepType": {
                    "stepTypeId": 6,
                    "stepTypeKey": "repeat",
                    "displayOrder": 6,
                },
                "numberOfIterations": block.repetitions,
                "workoutSteps": children,
                "endCondition": {
                    "conditionTypeId": 7,
                    "conditionTypeKey": "iterations",
                    "displayOrder": 7,
                    "displayable": False,
                },
                "endConditionValue": float(block.repetitions),
                "smartRepeat": False,
            })
            order += 1

    suffix = idempotency_key[:12]
    visible_name = workout.name.strip()[:82]
    return {
        "workoutName": f"{visible_name} [ARC-{suffix}]",
        "description": f"AI RunCoach idempotency={idempotency_key}",
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
        "estimatedDurationInSecs": max(estimated_seconds, 1),
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
            "workoutSteps": compiled_steps,
        }],
    }


def _external_id(value: Any, *names: str) -> int | None:
    if isinstance(value, dict):
        for name in names:
            candidate = value.get(name)
            if candidate is not None:
                try:
                    return int(candidate)
                except (TypeError, ValueError):
                    pass
        for nested in value.values():
            found = _external_id(nested, *names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _external_id(nested, *names)
            if found is not None:
                return found
    return None


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("workouts", "workoutList", "calendarItems", "items"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


class GarminWorkoutService:
    def __init__(
        self,
        client_factory: Callable[[str, str], Any] = Garmin,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.2,
    ):
        self.client_factory = client_factory
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds

    def _client(self, credentials: GarminCredentials):
        client = self.client_factory(credentials.email, credentials.password)
        self._retry(client.login)
        return client

    def _retry(self, operation: Callable[[], Any]):
        last_error = None
        for attempt in range(self.max_attempts):
            try:
                return operation()
            except GarminConnectNotFoundError:
                raise
            except GarminConnectConnectionError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status < 500 and status != 429:
                    raise
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(self.retry_delay_seconds * (2 ** attempt))
            except (
                GarminConnectTooManyRequestsError,
                RequestsConnectionError,
                RequestsTimeout,
                ConnectionError,
                TimeoutError,
            ) as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(self.retry_delay_seconds * (2 ** attempt))
        raise last_error  # type: ignore[misc]

    @staticmethod
    def _find_existing(client: Any, idempotency_key: str) -> dict[str, Any] | None:
        marker = f"ARC-{idempotency_key[:12]}"
        for item in _items(client.get_workouts(0, 100)):
            if marker in str(item.get("workoutName", "")) or idempotency_key in str(item.get("description", "")):
                return item
        return None

    @staticmethod
    def _find_scheduled(client: Any, workout_id: int, scheduled_date: date) -> dict[str, Any] | None:
        calendar = client.get_scheduled_workouts(scheduled_date.year, scheduled_date.month)
        for item in _items(calendar):
            item_workout_id = _external_id(item, "workoutId", "workout_id")
            item_date = item.get("date") or item.get("calendarDate") or item.get("startDate")
            if item_workout_id == workout_id and str(item_date).startswith(scheduled_date.isoformat()):
                return item
        return None

    def _schedule_idempotently(self, client: Any, workout_id: int, scheduled_date: date):
        existing = self._find_scheduled(client, workout_id, scheduled_date)
        if existing is not None:
            return existing
        return client.schedule_workout(workout_id, scheduled_date.isoformat())

    def _ignore_not_found(self, operation: Callable[[], Any]):
        try:
            return self._retry(operation)
        except GarminConnectNotFoundError:
            return None

    def deliver(self, request: DeliverWorkoutRequest) -> dict[str, Any]:
        client = self._client(request.credentials)
        payload = compile_workout(request.workout, request.idempotency_key)
        existing = self._find_existing(client, request.idempotency_key)
        uploaded = existing or self._retry(lambda: client.upload_workout(payload))
        workout_id = _external_id(uploaded, "workoutId", "workout_id", "id")
        if workout_id is None:
            raise ValueError("Garmin upload response did not contain a workout id")
        scheduled = self._retry(lambda: self._schedule_idempotently(
            client, workout_id, request.workout.scheduled_date
        ))
        scheduled_id = _external_id(
            scheduled, "scheduledWorkoutId", "calendarEventId", "workoutScheduleId", "id"
        )
        return {
            "workoutId": workout_id,
            "scheduledWorkoutId": scheduled_id,
            "scheduledDate": request.workout.scheduled_date,
            "idempotencyKey": request.idempotency_key,
            "reusedWorkout": existing is not None,
        }

    def update(self, workout_id: int, request: UpdateWorkoutRequest) -> dict[str, Any]:
        client = self._client(request.credentials)
        payload = compile_workout(request.workout, request.idempotency_key)
        self._retry(lambda: client.update_workout(workout_id, payload))
        scheduled_id = request.scheduled_workout_id
        if scheduled_id is not None:
            self._ignore_not_found(lambda: client.unschedule_workout(scheduled_id))
        scheduled = self._retry(lambda: self._schedule_idempotently(
            client, workout_id, request.workout.scheduled_date
        ))
        return {
            "workoutId": workout_id,
            "scheduledWorkoutId": _external_id(
                scheduled, "scheduledWorkoutId", "calendarEventId", "workoutScheduleId", "id"
            ),
            "scheduledDate": request.workout.scheduled_date,
            "idempotencyKey": request.idempotency_key,
        }

    def confirm(self, request: ConfirmWorkoutRequest) -> dict[str, Any]:
        client = self._client(request.credentials)
        calendar = self._retry(
            lambda: client.get_scheduled_workouts(
                request.scheduled_date.year, request.scheduled_date.month
            )
        )
        marker = f"ARC-{request.idempotency_key[:12]}"
        matched = None
        for item in _items(calendar):
            item_workout_id = _external_id(item, "workoutId", "workout_id")
            item_schedule_id = _external_id(
                item, "scheduledWorkoutId", "calendarEventId", "workoutScheduleId", "id"
            )
            item_date = item.get("date") or item.get("calendarDate") or item.get("startDate")
            date_matches = item_date is None or str(item_date).startswith(request.scheduled_date.isoformat())
            serialized = json.dumps(item, default=str)
            if date_matches and (item_workout_id == request.workout_id or (
                request.scheduled_workout_id is not None
                and item_schedule_id == request.scheduled_workout_id
            ) or marker in serialized):
                matched = item
                break
        return {
            "confirmed": matched is not None,
            "workoutId": request.workout_id,
            "scheduledWorkoutId": (
                _external_id(matched, "scheduledWorkoutId", "calendarEventId", "workoutScheduleId", "id")
                if matched else request.scheduled_workout_id
            ),
            "scheduledDate": request.scheduled_date,
        }

    def cancel(self, workout_id: int, request: CancelWorkoutRequest) -> dict[str, Any]:
        client = self._client(request.credentials)
        if request.scheduled_workout_id is not None:
            self._ignore_not_found(lambda: client.unschedule_workout(request.scheduled_workout_id))
        self._ignore_not_found(lambda: client.delete_workout(workout_id))
        return {"cancelled": True, "workoutId": workout_id}
