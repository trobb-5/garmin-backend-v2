from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import garth
import sqlite3
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth
from datetime import datetime

load_dotenv()

app = FastAPI(title="Garmin Backend for Nutrient Sync")

# Initialize Firebase
cred = credentials.Certificate("firebase-adminsdk.json")
firebase_admin.initialize_app(cred)

# SQLite database for session persistence
conn = sqlite3.connect("sessions.db", check_same_thread=False)
conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        firebase_uid TEXT PRIMARY KEY,
        garmin_dump TEXT,
        last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
conn.commit()

class GarminLoginRequest(BaseModel):
    username: str
    password: str
    mfa_code: str | None = None

@app.post("/garmin/login")
async def garmin_login(request: GarminLoginRequest, authorization: str = Header(...)):
    try:
        id_token = authorization.replace("Bearer ", "")
        decoded = auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]

        # FIX: Garth updated the class name from GarthClient to Client
        client = garth.Client()

        if request.mfa_code:
            client.login(request.username, request.password, prompt_mfa=lambda: request.mfa_code)
        else:
            client.login(request.username, request.password)

        dump = client.dump()

        conn.execute(
            "INSERT OR REPLACE INTO users (firebase_uid, garmin_dump) VALUES (?, ?)",
            (firebase_uid, dump)
        )
        conn.commit()

        return {"status": "success", "message": "Garmin connected successfully"}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/garmin/today")
async def garmin_today(authorization: str = Header(...)):
    try:
        id_token = authorization.replace("Bearer ", "")
        decoded = auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]

        row = conn.execute(
            "SELECT garmin_dump FROM users WHERE firebase_uid = ?", 
            (firebase_uid,)
        ).fetchone()

        if not row or not row[0]:
            raise HTTPException(status_code=404, detail="No Garmin session found.")

        client = garth.Client.from_dump(row[0])
        today = datetime.now().strftime("%Y-%m-%d")

        summary = client.get(f"https://connect.garmin.com/modern/proxy/usersummary-service/usersummary/daily/{today}").json()
        sleep = client.get(f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailySleepData/{today}").json()
        hrv = client.get(f"https://connect.garmin.com/modern/proxy/hrv-service/hrv/{today}").json()
        hr = client.get(f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailyHeartRate/{today}").json()

        return {
            "summary": summary,
            "sleep": sleep,
            "hrv": hrv,
            "hr": hr
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
