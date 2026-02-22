from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import garth
import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, auth, firestore
from datetime import datetime, timedelta

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
            client.login(
                request.username,
                request.password,
                prompt_mfa=lambda: request.mfa_code,
            )
        else:
            try:
                client.login(request.username, request.password)
            except EOFError:
                raise HTTPException(status_code=401, detail="MFA_REQUIRED")
            except Exception as e:
                err = str(e)
                if "MFA" in err or "TOTP" in err or "two-factor" in err.lower() or "2fa" in err.lower():
                    raise HTTPException(status_code=401, detail="MFA_REQUIRED")
                raise

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

        today     = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        def build_endpoints(date):
            if display_name:
                return {
                    "summary": f"/usersummary-service/usersummary/daily/{display_name}?calendarDate={date}",
                    "sleep":   f"/wellness-service/wellness/dailySleepData/{display_name}?date={date}",
                    "hr":      f"/wellness-service/wellness/dailyHeartRate/{display_name}?date={date}",
                    "hrv":     f"/hrv-service/hrv/{date}",
                }
            return {
                "summary": f"/usersummary-service/usersummary/daily/{date}",
                "sleep":   f"/wellness-service/wellness/dailySleepData/{date}",
                "hr":      f"/wellness-service/wellness/dailyHeartRate/{date}",
                "hrv":     f"/hrv-service/hrv/{date}",
            }

        def fetch_all(endpoints):
            result = {}
            for key, path in endpoints.items():
                try:
                    r = client.connectapi(path)
                    result[key] = r
                    print(f"OK {key} keys={list(r.keys()) if isinstance(r, dict) else type(r)}")
                except Exception as e:
                    print(f"Error {key}: {e}")
                    result[key] = None
            return result

        def has_activity_data(summary: dict) -> bool:
            """
            Check for real step data rather than relying on includesActivityData.
            Garmin keeps includesActivityData=false for most of the day even
            when steps/calories are actively being synced. totalSteps > 0
            is the reliable signal that the watch has synced real data.
            """
            if not summary:
                return False
            steps = summary.get("totalSteps")
            return steps is not None and steps > 0

        # ── Fetch today ──────────────────────────────────────────────────
        data          = fetch_all(build_endpoints(today))
        today_summary = data.get("summary") or {}

        if has_activity_data(today_summary):
            # Today has real step data — use it for activity, HR, HRV.
            # Sleep is always last night's data, so always pull from yesterday.
            print(f"Using today ({today}) for activity: {today_summary.get('totalSteps')} steps")
            yesterday_data = fetch_all({"sleep": build_endpoints(yesterday)["sleep"]})
            sleep_yest     = yesterday_data.get("sleep") or {}
            sleep_today    = data.get("sleep") or {}
            # Prefer yesterday's sleep if it has the actual DTO
            if sleep_yest.get("dailySleepDTO"):
                data["sleep"] = sleep_yest
                print(f"Using yesterday ({yesterday}) for sleep")
            elif sleep_today.get("dailySleepDTO"):
                print(f"Using today's sleep data")
            else:
                print("No sleep DTO found in either date")
        else:
            # Today has no step data yet — fall back to yesterday for everything
            print(f"No step data for {today}, falling back to {yesterday}")
            yesterday_data = fetch_all(build_endpoints(yesterday))
            data["summary"] = yesterday_data.get("summary") or today_summary
            data["hr"]      = yesterday_data.get("hr")      or data.get("hr")
            data["hrv"]     = yesterday_data.get("hrv")     or data.get("hrv")
            # Sleep: prefer whichever has dailySleepDTO
            sleep_yest  = yesterday_data.get("sleep") or {}
            sleep_today = data.get("sleep") or {}
            if sleep_yest.get("dailySleepDTO"):
                data["sleep"] = sleep_yest
            elif not sleep_today.get("dailySleepDTO"):
                data["sleep"] = sleep_yest  # best we have

        db.collection("users").document(firebase_uid).set(
            {"last_sync": firestore.SERVER_TIMESTAMP}, merge=True)

        return data

    except Exception as e:
        print(f"Critical Backend Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ── Debug endpoint ───────────────────────────────────────────────────────────
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
                out["data"][key] = result
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
