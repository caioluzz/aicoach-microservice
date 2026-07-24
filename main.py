from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from garminconnect import Garmin
import uvicorn

app = FastAPI(title="AI RunCoach")


class GarminBotRequest(BaseModel):
    email: str
    password: str
    limit: int


@app.post("/api/garmin/activities")
def fetch_activities(req: GarminBotRequest):
    try:
        client = Garmin(req.email, req.password)
        client.login()

        raw_activities = client.get_activities(0, req.limit)

        formatted_activities = []
        for act in raw_activities:
            hr = act.get("averageHR")

            formatted_activities.append({
                "activity_id": act.get("activityId"),
                "activity_name": act.get("activityName", "Atividade Sem Nome"),
                "distance_meters": act.get("distance"),
                "duration_seconds": act.get("duration"),
                "started_at": act.get("startTimeLocal"),
                "average_heart_rate": int(hr) if hr is not None else 0,
                "average_speed": act.get("averageSpeed")
            })

        return formatted_activities

    except Exception as e:
        print(f"Erro ao buscar dados na Garmin: {e}")
        raise HTTPException(status_code=400, detail="Falha na autenticação ou extração da Garmin")