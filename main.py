from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import garth
import sqlite3
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth, firestore
from datetime import datetime
import io

load_dotenv()

app = FastAPI(title="Garmin Backend for Nutrient Sync")

# Initialize Firebase & Firestore
cred = credentials.Certificate("firebase-adminsdk.json")
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()

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

        client = garth.Client()

        if request.mfa_code:
            await client.login(request.username, request.password, prompt_mfa=lambda: request.mfa_code)
        else:
            await client.login(request.username, request.password)

        # Save session dump
        dump = client.dump()

        db.collection("users").document(firebase_uid).set({
            "garmin_dump": dump,
            "last_sync": firestore.SERVER_TIMESTAMP
        }, merge=True)

        return {"status": "success", "message": "Garmin connected successfully"}

    except Exception as e:
        print(f"Login Failure: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/garmin/today")
async def garmin_today(authorization: str = Header(...)):
    try:
        id_token = authorization.replace("Bearer ", "")
        decoded = auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]

        doc = db.collection("users").document(firebase_uid).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="No Garmin session found.")

        garmin_dump = doc.to_dict().get("garmin_dump")
        client = garth.Client()
        client.resume_from_dump(garmin_dump)   # Correct way to restore session

        today = datetime.now().strftime("%Y-%m-%d")

        sess = client.sess
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 GarminConnect/5.12.0.0",
            "Accept": "application/json",
            "NK": "NT",
            "X-App-Ver": "5.12.0.0",
            "X-lang": "en-US",
        })

        endpoints = {
            "summary": f"https://connect.garmin.com/modern/proxy/usersummary-service/usersummary/daily/{today}",
            "sleep":   f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailySleepData/{today}",
            "hrv":     f"https://connect.garmin.com/modern/proxy/hrv-service/hrv/{today}",
            "hr":      f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailyHeartRate/{today}",
        }

        data = {}
        for key, url in endpoints.items():
            try:
                resp = sess.get(url)
                if resp.status_code == 200:
                    data[key] = resp.json()
                else:
                    print(f"Error {key}: {resp.status_code} - {resp.text[:200]}")
                    data[key] = None
            except Exception as e:
                print(f"Fetch failed for {key}: {e}")
                data[key] = None

        return data

    except Exception as e:
        print(f"Critical Backend Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
