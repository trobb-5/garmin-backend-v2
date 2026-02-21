from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import garth
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth, firestore
from datetime import datetime

load_dotenv()

app = FastAPI(title="Garmin Backend for Nutrient Sync")

# Initialize Firebase & Firestore
# Using Firestore instead of SQLite because Render deletes local files on every restart
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
            client.login(request.username, request.password, prompt_mfa=lambda: request.mfa_code)
        else:
            client.login(request.username, request.password)

        # Store session in Firestore so it's permanent
        dump = client.dumps()
        db.collection("users").document(firebase_uid).set({
            "garmin_dump": dump,
            "last_sync": firestore.SERVER_TIMESTAMP
        }, merge=True)

        return {"status": "success", "message": "Garmin connected successfully"}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/garmin/today")
async def garmin_today(authorization: str = Header(...)):
    try:
        id_token = authorization.replace("Bearer ", "")
        decoded = auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]

        # Pull permanent session from Firestore
        doc = db.collection("users").document(firebase_uid).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="No Garmin session found in database.")
        
        garmin_dump = doc.to_dict().get("garmin_dump")

        client = garth.Client()
        client.loads(garmin_dump)
        
        # SPOOF HEADERS: Essential to prevent 403 Forbidden errors on Render
        client.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": "https://connect.garmin.com/modern/dashboards/daily-summary",
            "Accept": "application/json, text/plain, */*"
        })
        
        today = datetime.now().date().isoformat()
        data = {}

        # UPDATED ENDPOINTS: Using dailySummaryChart for real-time steps/calories
        endpoints = {
            "summary": f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailySummaryChart/{today}",
            "sleep": f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailySleepData/{today}",
            "hrv": f"https://connect.garmin.com/modern/proxy/hrv-service/hrv/{today}",
            "hr": f"https://connect.garmin.com/modern/proxy/wellness-service/wellness/dailyHeartRate/{today}"
        }

        for key, url in endpoints.items():
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    data[key] = resp.json()
                else:
                    print(f"Garmin API Info: {key} returned status {resp.status_code}")
                    data[key] = None
            except Exception as e:
                print(f"Error fetching {key}: {e}")
                data[key] = None

        return data

    except Exception as e:
        print(f"Fetch Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # Local development uses port 8000; Render uses 10000
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
