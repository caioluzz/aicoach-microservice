import io
import math
import zipfile
from datetime import datetime
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fitparse import FitFile
from garminconnect import Garmin
from pydantic import BaseModel, Field

app = FastAPI(title="AI RunCoach Garmin Bot")

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
    lap_count: Optional[int] = None
    record_count: Optional[int] = None
    laps: List[LapModel] = []
    activity_records: List[ActivityRecordModel] = Field(default_factory=list)


class GarminBotRequest(BaseModel):
    email: str
    password: str
    limit: int

def meters_to_km(value):
    return value / 1000 if value else None


def speed_to_kmh(value):
    return value * 3.6 if value else None


def speed_to_pace_s_per_km(speed_mps):
    if not speed_mps or speed_mps <= 0:
        return None
    return int(1000 / speed_mps)


def format_datetime(dt):
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return None


def message_to_dict(message):
    return {field.name: field.value for field in message}


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

@app.post("/api/garmin/activities", response_model=List[GarminBotResponseDTO])
def fetch_activities(req: GarminBotRequest):
    try:
        client = Garmin(req.email, req.password)
        client.login()

        raw_activities = client.get_activities(0, req.limit)

        results = []

        for act in raw_activities:
            act_id = act.get("activityId")
            act_name = act.get("activityName", "Sem Nome")

            is_vdot = "vdot" in act_name.lower() or "teste 3km" in act_name.lower()

            try:
                raw_data = client.download_activity(act_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)

                # Descompactar em memória se for ZIP
                if raw_data.startswith(b"PK"):
                    with zipfile.ZipFile(io.BytesIO(raw_data)) as zf:
                        fit_names = [name for name in zf.namelist() if name.lower().endswith('.fit')]
                        if fit_names:
                            fit_bytes = zf.read(fit_names[0])
                            laps, records = process_fit_data(fit_bytes)
                        else:
                            laps, records = [], []
                elif raw_data.startswith(b".FIT"):  # Caso já venha como FIT
                    laps, records = process_fit_data(raw_data)
                else:
                    laps, records = [], []

            except Exception as e:
                print(f"Erro ao processar arquivo FIT da atividade {act_id}: {e}")
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
                is_vdot_test=is_vdot,
                lap_count=len(laps),
                record_count=len(records),
                laps=laps,
                activity_records=records
            )
            results.append(dto)

        return results

    except Exception as e:
        print(f"Erro ao buscar dados na Garmin: {e}")
        raise HTTPException(status_code=400, detail="Falha na extração da Garmin")