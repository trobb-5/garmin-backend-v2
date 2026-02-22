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

        # Fetch and store displayName at login time so we have it for data endpoints
        display_name = None
        try:
            profile = client.connectapi("/userprofile-service/socialProfile")
            display_name = profile.get("displayName")
            print(f"Got displayName: {display_name}")
        except Exception as e:
            print(f"Could not fetch displayName: {e}")

        db.collection("users").document(firebase_uid).set({
            "garmin_dump":    dump,
            "display_name":   display_name,
            "last_sync":      firestore.SERVER_TIMESTAMP
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

        doc_data     = doc.to_dict()
        garmin_dump  = doc_data.get("garmin_dump")
        display_name = doc_data.get("display_name")

        client = garth.Client()
        client.loads(garmin_dump)

        # If displayName wasn't stored at login, fetch it now
        if not display_name:
            try:
                profile      = client.connectapi("/userprofile-service/socialProfile")
                display_name = profile.get("displayName")
                print(f"Fetched displayName on-demand: {display_name}")
                # Cache it for future requests
                db.collection("users").document(firebase_uid).set(
                    {"display_name": display_name}, merge=True
                )
            except Exception as e:
                print(f"Could not fetch displayName: {e}")

        today = datetime.now().strftime("%Y-%m-%d")

        # Summary and HR/sleep endpoints require displayName in the path.
        # HRV does not. This is why HRV succeeded before while the others 403'd.
        endpoints = {}

        if display_name:
            endpoints["summary"] = f"/usersummary-service/usersummary/daily/{display_name}?calendarDate={today}"
            endpoints["sleep"]   = f"/wellness-service/wellness/dailySleepData/{display_name}?date={today}"
            endpoints["hr"]      = f"/wellness-service/wellness/dailyHeartRate/{display_name}?date={today}"
        else:
            # Fallback without displayName — may still 403 but won't crash
            print("WARNING: No displayName available, summary/sleep/hr may fail")
            endpoints["summary"] = f"/usersummary-service/usersummary/daily/{today}"
            endpoints["sleep"]   = f"/wellness-service/wellness/dailySleepData/{today}"
            endpoints["hr"]      = f"/wellness-service/wellness/dailyHeartRate/{today}"

        # HRV endpoint does not use displayName
        endpoints["hrv"] = f"/hrv-service/hrv/{today}"

        data = {}
        for key, path in endpoints.items():
            try:
                data[key] = client.connectapi(path)
                print(f"OK {key}: fetched successfully")
            except Exception as e:
                print(f"Error {key}: {e}")
                data[key] = None

        db.collection("users").document(firebase_uid).set(
            {"last_sync": firestore.SERVER_TIMESTAMP}, merge=True
        )

        return data

    except Exception as e:
        print(f"Critical Backend Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
