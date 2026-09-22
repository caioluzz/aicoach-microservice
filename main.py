import io
import hashlib
import logging
import os
import secrets
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi import Request
from fitparse import FitFile
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from pydantic import BaseModel, Field, ValidationError
from starlette.responses import JSONResponse

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

app = FastAPI(title="AI RunCoach")
logger = logging.getLogger(__name__)
workout_service = GarminWorkoutService()
adapter_api_key = os.getenv("GARMIN_ADAPTER_API_KEY", "")
garmin_login_lock = threading.RLock()


@app.middleware("http")
async def protect_adapter(request: Request, call_next):
    if adapter_api_key and request.url.path.startswith("/api/garmin/"):
        supplied = request.headers.get("X-Adapter-Key", "")
        if not secrets.compare_digest(supplied, adapter_api_key):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized adapter request"})
    return await call_next(request)

class LapModel(BaseModel):
    lap_number: int
    lap_type: Optional[str] = None
    start_time: Optional[str] = None
    duration_s: Optional[float] = None
    distance_km: Optional[float] = None
    avg_pace_s_per_km: Optional[int] = None
    avg_speed_kmh: Optional[float] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    avg_cadence: Optional[int] = None
    max_cadence: Optional[int] = None
    ascent_m: Optional[int] = None
    descent_m: Optional[int] = None


class ActivityRecordModel(BaseModel):
    ts: Optional[str] = None
    elapsed_s: Optional[int] = None
    distance_km: Optional[float] = None
    speed_kmh: Optional[float] = None
    pace_s_per_km: Optional[int] = None
    heart_rate: Optional[int] = None
    cadence: Optional[int] = None
    altitude_m: Optional[float] = None


class GarminBotResponseDTO(BaseModel):
    activity_id: int
    activity_name: str
    distance_meters: Optional[float] = None
    duration_seconds: Optional[float] = None
    started_at: Optional[str] = None
    average_heart_rate: Optional[int] = None
    average_speed: Optional[float] = None
    sport: Optional[str] = None
    sub_sport: Optional[str] = None
    is_vdot_test: bool = False
    max_speed_kmh: Optional[float] = None
    min_altitude_m: Optional[float] = None
    max_altitude_m: Optional[float] = None
    ended_at: Optional[str] = None
    best_pace_s_per_km: Optional[int] = None
    max_hr: Optional[int] = None
    avg_cadence: Optional[int] = None
    max_cadence: Optional[int] = None
    elevation_gain_m: Optional[int] = None
    elevation_loss_m: Optional[int] = None
    raw_file_path: Optional[str] = None
    lap_count: Optional[int] = None
    record_count: Optional[int] = None
    laps: List[LapModel] = Field(default_factory=list)
    activity_records: List[ActivityRecordModel] = Field(default_factory=list)


class GarminBotRequest(BaseModel):
    email: str
    password: str
    limit: int = Field(ge=1, le=100)


class ActivityDiscoveryRequest(GarminBotRequest):
    since: Optional[datetime] = None


class ActivityDownloadRequest(BaseModel):
    email: str
    password: str


class ActivityMetadata(BaseModel):
    activity_id: int
    activity_name: str
    distance_meters: Optional[float] = None
    duration_seconds: Optional[float] = None
    started_at: Optional[str] = None
    average_heart_rate: Optional[int] = None
    average_speed: Optional[float] = None
    sport: Optional[str] = None
    sub_sport: Optional[str] = None
    is_vdot_test: bool = False
    max_speed_kmh: Optional[float] = None
    min_altitude_m: Optional[float] = None
    max_altitude_m: Optional[float] = None
    max_hr: Optional[int] = None
    avg_cadence: Optional[int] = None
    max_cadence: Optional[int] = None
    elevation_gain_m: Optional[int] = None
    elevation_loss_m: Optional[int] = None


class ActivityDiscoveryMetadata(BaseModel):
    activity_id: int
    activity_name: str
    started_at: Optional[str] = None
    is_vdot_test: bool = False

def meters_to_km(value):
    return value / 1000 if value is not None else None


def speed_to_kmh(value):
    return value * 3.6 if value is not None else None


def speed_to_pace_s_per_km(speed_mps):
    if not speed_mps or speed_mps <= 0:
        return None
    return int(1000 / speed_mps)


def format_datetime(dt):
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return None


def normalize_datetime(value):
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, str) and value.strip():
        try:
            normalized = value.strip().replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value.strip()
    return None


def message_to_dict(message):
    return {field.name: field.value for field in message}


def fit_sport_metadata(fit_bytes: bytes):
    fit = FitFile(io.BytesIO(fit_bytes))
    for message_name in ("session", "sport"):
        for message in fit.get_messages(message_name):
            row = message_to_dict(message)
            sport = row.get("sport")
            sub_sport = row.get("sub_sport")
            if sport or sub_sport:
                return (
                    str(sport) if sport is not None else None,
                    str(sub_sport) if sub_sport is not None else None,
                )
    return None, None


def process_fit_data(fit_bytes: bytes):
    fit = FitFile(io.BytesIO(fit_bytes))

    # Extrair Laps
    laps_data = []
    for idx, message in enumerate(fit.get_messages("lap"), start=1):
        row = message_to_dict(message)
        speed_mps = row.get("enhanced_avg_speed") or row.get("avg_speed")

        laps_data.append(LapModel(
            lap_number=idx,
            lap_type=row.get("raw_intensity"),
            start_time=format_datetime(row.get("start_time")),
            duration_s=row.get("total_elapsed_time") or row.get("total_timer_time"),
            distance_km=meters_to_km(row.get("total_distance")),
            avg_pace_s_per_km=speed_to_pace_s_per_km(speed_mps),
            avg_speed_kmh=speed_to_kmh(speed_mps),
            avg_hr=row.get("avg_heart_rate"),
            max_hr=row.get("max_heart_rate"),
            avg_cadence=row.get("avg_running_cadence") or row.get("avg_cadence"),
            max_cadence=row.get("max_running_cadence") or row.get("max_cadence"),
            ascent_m=row.get("total_ascent"),
            descent_m=row.get("total_descent")
        ))

    # Extrair Records (Telemetria)
    records_data = []
    start_ts = None

    for message in fit.get_messages("record"):
        row = message_to_dict(message)
        ts = row.get("timestamp")

        if not ts:
            continue

        if start_ts is None:
            start_ts = ts

        elapsed_s = int((ts - start_ts).total_seconds())
        speed_mps = row.get("enhanced_speed") or row.get("speed")

        records_data.append(ActivityRecordModel(
            ts=format_datetime(ts),
            elapsed_s=elapsed_s,
            distance_km=meters_to_km(row.get("distance")),
            speed_kmh=speed_to_kmh(speed_mps),
            pace_s_per_km=speed_to_pace_s_per_km(speed_mps),
            heart_rate=row.get("heart_rate"),
            cadence=row.get("cadence"),
            altitude_m=row.get("enhanced_altitude") or row.get("altitude")
        ))

    return laps_data, records_data


def extract_fit_bytes(raw_data: bytes) -> Optional[bytes]:
    if raw_data.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(raw_data)) as zf:
            fit_names = [name for name in zf.namelist() if name.lower().endswith(".fit")]
            return zf.read(fit_names[0]) if fit_names else None
    if len(raw_data) >= 12 and raw_data[8:12] == b".FIT":
        return raw_data
    return None


def garmin_tokenstore(email: str) -> str:
    configured = os.getenv("GARMIN_TOKEN_DIR")
    root = Path(configured) if configured else Path(
        os.getenv("LOCALAPPDATA") or tempfile.gettempdir()
    ) / "AIRunCoach" / "garmin-tokens"
    account_key = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
    return str(root / account_key)


def connect_garmin(email: str, password: str):
    client = Garmin(email, password)
    # A biblioteca persiste apenas tokens OAuth, com permissão restrita ao
    # usuário do processo. Isso evita um login SSO novo a cada request e reduz
    # bloqueios do Garmin por excesso de autenticações.
    with garmin_login_lock:
        client.login(garmin_tokenstore(email))
    return client


def raise_garmin_http_error(exc: Exception, operation: str):
    logger.warning("Falha Garmin operation=%s error_type=%s", operation, type(exc).__name__)
    if isinstance(exc, GarminConnectAuthenticationError):
        raise HTTPException(
            status_code=401,
            detail="O Garmin rejeitou as credenciais ou exige uma validação adicional na conta.",
        ) from None
    if isinstance(exc, GarminConnectTooManyRequestsError):
        raise HTTPException(
            status_code=429,
            detail="O Garmin limitou temporariamente as tentativas. Aguarde alguns minutos e tente novamente.",
        ) from None
    if isinstance(exc, GarminConnectNotFoundError):
        raise HTTPException(status_code=404, detail="Atividade Garmin não encontrada para esta conta.") from None
    if isinstance(exc, GarminConnectConnectionError):
        raise HTTPException(
            status_code=503,
            detail="O Garmin Connect está indisponível ou recusou a conexão temporariamente.",
        ) from None
    raise HTTPException(status_code=502, detail=f"Falha inesperada ao {operation} no Garmin.") from None


def activity_type_metadata(activity: dict, summary: dict):
    candidates = (
        activity.get("activityType"),
        activity.get("activityTypeDTO"),
        activity.get("sportType"),
        activity.get("sportTypeDTO"),
        summary.get("activityType"),
        summary.get("activityTypeDTO"),
        summary.get("sportType"),
        summary.get("sportTypeDTO"),
    )
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        sport = candidate.get("typeKey") or candidate.get("sportTypeKey")
        sub_sport = candidate.get("parentTypeKey") or candidate.get("subSportTypeKey")
        if sport or sub_sport:
            return sport, sub_sport
    return None, None


def map_discovery_metadata(activity: dict) -> ActivityDiscoveryMetadata:
    summary = activity.get("summaryDTO") if isinstance(activity.get("summaryDTO"), dict) else {}
    name = activity.get("activityName") or summary.get("activityName") or "Sem Nome"
    return ActivityDiscoveryMetadata(
        activity_id=activity.get("activityId") or summary.get("activityId"),
        activity_name=name,
        started_at=normalize_datetime(
            activity.get("startTimeLocal") or summary.get("startTimeLocal")
            or activity.get("startTimeGMT") or summary.get("startTimeGMT")
        ),
        is_vdot_test="vdot" in name.lower() or "teste 3km" in name.lower(),
    )


def map_activity_metadata(activity: dict) -> ActivityMetadata:
    summary = activity.get("summaryDTO") if isinstance(activity.get("summaryDTO"), dict) else {}
    sport, sub_sport = activity_type_metadata(activity, summary)
    name = activity.get("activityName") or summary.get("activityName") or "Sem Nome"
    heart_rate = activity.get("averageHR") or summary.get("averageHR")
    return ActivityMetadata(
        activity_id=activity.get("activityId") or summary.get("activityId"),
        activity_name=name,
        distance_meters=activity.get("distance") or summary.get("distance"),
        duration_seconds=activity.get("duration") or summary.get("duration"),
        started_at=normalize_datetime(
            activity.get("startTimeLocal") or summary.get("startTimeLocal")
            or activity.get("startTimeGMT") or summary.get("startTimeGMT")
        ),
        average_heart_rate=int(heart_rate) if heart_rate else None,
        average_speed=activity.get("averageSpeed") or summary.get("averageSpeed"),
        sport=sport,
        sub_sport=sub_sport,
        is_vdot_test="vdot" in name.lower() or "teste 3km" in name.lower(),
        max_speed_kmh=speed_to_kmh(activity.get("maxSpeed")),
        min_altitude_m=activity.get("minElevation"),
        max_altitude_m=activity.get("maxElevation"),
        max_hr=activity.get("maxHR"),
        avg_cadence=activity.get("averageRunningCadenceInStepsPerMinute"),
        max_cadence=activity.get("maxRunningCadenceInStepsPerMinute"),
        elevation_gain_m=activity.get("elevationGain"),
        elevation_loss_m=activity.get("elevationLoss"),
    )


def metadata_to_response(metadata: ActivityMetadata, laps, records) -> GarminBotResponseDTO:
    started_at = metadata.started_at
    if started_at is None:
        started_at = next((record.ts for record in records if record.ts), None)
    if started_at is None:
        started_at = next((lap.start_time for lap in laps if lap.start_time), None)
    return GarminBotResponseDTO(
        **(metadata.model_dump() | {"started_at": started_at}),
        lap_count=len(laps),
        record_count=len(records),
        laps=laps,
        activity_records=records,
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/garmin/workouts/preview")
def preview_workout(workout: WorkoutRequest):
    digest = workout_hash(workout)
    return {
        "hash": digest,
        "idempotencyKey": digest,
        "scheduledDate": workout.scheduled_date,
        "payload": compile_workout(workout, digest),
    }


@app.post("/api/garmin/workouts/deliver")
def deliver_workout(request: DeliverWorkoutRequest):
    try:
        return workout_service.deliver(request)
    except Exception as exc:
        logger.warning("Falha ao entregar workout Garmin (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Falha na entrega do workout Garmin") from None


@app.put("/api/garmin/workouts/{workout_id}")
def update_workout(workout_id: int, request: UpdateWorkoutRequest):
    try:
        return workout_service.update(workout_id, request)
    except Exception as exc:
        logger.warning("Falha ao atualizar workout Garmin (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Falha na atualizacao do workout Garmin") from None


@app.post("/api/garmin/workouts/confirm")
def confirm_workout(request: ConfirmWorkoutRequest):
    try:
        return workout_service.confirm(request)
    except Exception as exc:
        logger.warning("Falha ao confirmar workout Garmin (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Falha na confirmacao do workout Garmin") from None


@app.post("/api/garmin/workouts/{workout_id}/cancel")
def cancel_workout(workout_id: int, request: CancelWorkoutRequest):
    try:
        return workout_service.cancel(workout_id, request)
    except Exception as exc:
        logger.warning("Falha ao cancelar workout Garmin (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Falha no cancelamento do workout Garmin") from None

@app.post("/api/garmin/activities", response_model=List[GarminBotResponseDTO])
def fetch_activities(req: GarminBotRequest):
    try:
        client = connect_garmin(req.email, req.password)

        raw_activities = client.get_activities(0, req.limit)

        results = []

        for act in raw_activities:
            act_id = act.get("activityId")
            act_name = act.get("activityName", "Sem Nome")

            is_vdot = "vdot" in act_name.lower() or "teste 3km" in act_name.lower()

            try:
                raw_data = client.download_activity(act_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)

                fit_bytes = extract_fit_bytes(raw_data)
                laps, records = process_fit_data(fit_bytes) if fit_bytes else ([], [])

            except Exception as e:
                logger.warning(
                    "Falha ao processar FIT da atividade %s (%s)",
                    act_id,
                    type(e).__name__,
                )
                laps, records = [], []

            hr = act.get("averageHR")

            dto = GarminBotResponseDTO(
                activity_id=act_id,
                activity_name=act_name,
                distance_meters=act.get("distance"),
                duration_seconds=act.get("duration"),
                started_at=act.get("startTimeLocal"),  # Ex: "2023-10-01 10:00:00"
                average_heart_rate=int(hr) if hr else None,
                average_speed=act.get("averageSpeed"),
                sport=act.get("activityType", {}).get("typeKey"),
                sub_sport=act.get("activityType", {}).get("parentTypeKey"),
                is_vdot_test=is_vdot,
                max_speed_kmh=speed_to_kmh(act.get("maxSpeed")),
                min_altitude_m=act.get("minElevation"),
                max_altitude_m=act.get("maxElevation"),
                max_hr=act.get("maxHR"),
                avg_cadence=act.get("averageRunningCadenceInStepsPerMinute"),
                max_cadence=act.get("maxRunningCadenceInStepsPerMinute"),
                elevation_gain_m=act.get("elevationGain"),
                elevation_loss_m=act.get("elevationLoss"),
                lap_count=len(laps),
                record_count=len(records),
                laps=laps,
                activity_records=records
            )
            results.append(dto)

        return results

    except HTTPException:
        raise
    except Exception as exc:
        raise_garmin_http_error(exc, "extrair atividades")


@app.post("/api/garmin/activities/discover", response_model=List[ActivityDiscoveryMetadata])
def discover_activities(req: ActivityDiscoveryRequest):
    try:
        client = connect_garmin(req.email, req.password)
        raw_activities = client.get_activities(0, req.limit)
        if not isinstance(raw_activities, list):
            raise HTTPException(
                status_code=502,
                detail="O Garmin devolveu um formato inesperado ao listar atividades.",
            )
        activities = []
        for index, item in enumerate(raw_activities):
            if not isinstance(item, dict):
                logger.warning(
                    "Atividade Garmin ignorada index=%s reason=invalid_item_type", index
                )
                continue
            try:
                activities.append(map_discovery_metadata(item))
            except ValidationError as exc:
                error_types = sorted({error["type"] for error in exc.errors()})
                logger.warning(
                    "Atividade Garmin ignorada index=%s reason=validation error_types=%s",
                    index,
                    ",".join(error_types),
                )
        if raw_activities and not activities:
            raise HTTPException(
                status_code=502,
                detail="Nenhuma atividade da resposta Garmin possuía identificador utilizável.",
            )
        if req.since is None:
            return activities
        since = req.since.replace(tzinfo=None)
        return [
            item for item in activities
            if item.started_at is not None
            and datetime.fromisoformat(item.started_at).replace(tzinfo=None) >= since
        ]
    except Exception as exc:
        raise_garmin_http_error(exc, "listar atividades")


@app.post("/api/garmin/activities/{activity_id}/download", response_model=GarminBotResponseDTO)
def download_activity(activity_id: int, req: ActivityDownloadRequest):
    try:
        client = connect_garmin(req.email, req.password)
        raw_activity = client.get_activity(activity_id)
        if raw_activity is None:
            raise HTTPException(status_code=404, detail="Atividade Garmin não encontrada")
        raw_data = client.download_activity(
            activity_id,
            dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL,
        )
        fit_bytes = extract_fit_bytes(raw_data)
        if fit_bytes is None:
            raise HTTPException(status_code=422, detail="Arquivo FIT inválido")
        laps, records = process_fit_data(fit_bytes)
        metadata = map_activity_metadata(raw_activity)
        if metadata.sport is None:
            fit_sport, fit_sub_sport = fit_sport_metadata(fit_bytes)
            metadata = metadata.model_copy(update={
                "sport": fit_sport,
                "sub_sport": metadata.sub_sport or fit_sub_sport,
            })
        response = metadata_to_response(metadata, laps, records)
        if response.started_at is None:
            raise HTTPException(
                status_code=422,
                detail="A atividade Garmin não informou data de início no resumo nem no FIT.",
            )
        return response
    except HTTPException:
        raise
    except Exception as exc:
        raise_garmin_http_error(exc, "baixar a atividade")
