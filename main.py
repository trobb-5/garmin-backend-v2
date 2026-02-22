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


def _get_client(firebase_uid: str) -> garth.Client:
    doc = db.collection("users").document(firebase_uid).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="No Garmin session found.")
    garmin_dump = doc.to_dict().get("garmin_dump")
    client = garth.Client()
    client.loads(garmin_dump)
    return client, doc.to_dict()


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

        display_name = None
        try:
            profile = client.connectapi("/userprofile-service/socialProfile")
            display_name = profile.get("displayName")
            print(f"Got displayName: {display_name}")
        except Exception as e:
            print(f"Could not fetch displayName: {e}")

        db.collection("users").document(firebase_uid).set({
            "garmin_dump":  dump,
            "display_name": display_name,
            "last_sync":    firestore.SERVER_TIMESTAMP
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

        client, doc_data = _get_client(firebase_uid)
        display_name = doc_data.get("display_name")

        if not display_name:
            try:
                profile      = client.connectapi("/userprofile-service/socialProfile")
                display_name = profile.get("displayName")
                db.collection("users").document(firebase_uid).set(
                    {"display_name": display_name}, merge=True)
                print(f"Fetched displayName on-demand: {display_name}")
            except Exception as e:
                print(f"Could not fetch displayName: {e}")

        today = datetime.now().strftime("%Y-%m-%d")

        endpoints = {
            "summary": f"/usersummary-service/usersummary/daily/{display_name}?calendarDate={today}",
            "sleep":   f"/wellness-service/wellness/dailySleepData/{display_name}?date={today}",
            "hr":      f"/wellness-service/wellness/dailyHeartRate/{display_name}?date={today}",
            "hrv":     f"/hrv-service/hrv/{today}",
        } if display_name else {
            "summary": f"/usersummary-service/usersummary/daily/{today}",
            "sleep":   f"/wellness-service/wellness/dailySleepData/{today}",
            "hr":      f"/wellness-service/wellness/dailyHeartRate/{today}",
            "hrv":     f"/hrv-service/hrv/{today}",
        }

        data = {}
        for key, path in endpoints.items():
            try:
                result = client.connectapi(path)
                data[key] = result
                # Log top-level keys so we can see the actual structure
                if isinstance(result, dict):
                    print(f"OK {key}: keys={list(result.keys())}")
                else:
                    print(f"OK {key}: type={type(result)}")
            except Exception as e:
                print(f"Error {key}: {e}")
                data[key] = None

        db.collection("users").document(firebase_uid).set(
            {"last_sync": firestore.SERVER_TIMESTAMP}, merge=True)

        return data

    except Exception as e:
        print(f"Critical Backend Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ── Debug endpoint — call once to see exact field names ─────────────────────
@app.get("/garmin/debug")
async def garmin_debug(authorization: str = Header(...)):
    """Returns full raw response from each endpoint so we can verify field names."""
    try:
        id_token = authorization.replace("Bearer ", "")
        decoded = auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]

        client, doc_data = _get_client(firebase_uid)
        display_name = doc_data.get("display_name")
        today = datetime.now().strftime("%Y-%m-%d")

        out = {"display_name": display_name, "date": today, "data": {}}

        endpoints = {
            "summary": f"/usersummary-service/usersummary/daily/{display_name}?calendarDate={today}",
            "sleep":   f"/wellness-service/wellness/dailySleepData/{display_name}?date={today}",
            "hr":      f"/wellness-service/wellness/dailyHeartRate/{display_name}?date={today}",
            "hrv":     f"/hrv-service/hrv/{today}",
        }

        for key, path in endpoints.items():
            try:
                result = client.connectapi(path)
                out["data"][key] = result  # Full raw response
                print(f"DEBUG {key}: {result}")
            except Exception as e:
                out["data"][key] = {"error": str(e)}

        return out

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
