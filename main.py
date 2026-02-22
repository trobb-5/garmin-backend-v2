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

        dump = client.dumps()
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
        client.loads(garmin_dump)

        today = datetime.now().strftime("%Y-%m-%d")

        # FIX: Use client.connectapi() — this uses garth's session which injects
        # the correct OAuth/SSO headers that Garmin's Cloudflare expects.
        # sess.get() bypasses those headers, causing 403 "Just a moment" blocks.
        endpoints = {
            "summary": f"/usersummary-service/usersummary/daily/{today}",
            "sleep":   f"/wellness-service/wellness/dailySleepData/{today}",
            "hrv":     f"/hrv-service/hrv/{today}",
            "hr":      f"/wellness-service/wellness/dailyHeartRate/{today}",
        }

        data = {}
        for key, path in endpoints.items():
            try:
                data[key] = client.connectapi(path)
                print(f"OK {key}: fetched successfully")
            except Exception as e:
                print(f"Error {key}: {e}")
                data[key] = None

        # Update last_sync timestamp after successful fetch
        db.collection("users").document(firebase_uid).set({
            "last_sync": firestore.SERVER_TIMESTAMP
        }, merge=True)

        return data

    except Exception as e:
        print(f"Critical Backend Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
