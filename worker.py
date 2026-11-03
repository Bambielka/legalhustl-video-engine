"""GitHub Actions worker: claim jobs from Apps Script, render locally on runner,
then upload MP4 bytes back to the bound Google Drive folder through Apps Script.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from renderer import build_video

BRIDGE_URL = os.environ.get("APP_SCRIPT_BRIDGE_URL", "").strip()
TOKEN = os.environ.get("VIDEO_BRIDGE_TOKEN", "").strip()
MAX_JOBS = int(os.environ.get("MAX_JOBS_PER_RUN", "2"))
TIMEOUT = (20, 180)


def api(action: str, **payload):
    if not BRIDGE_URL or not TOKEN:
        raise RuntimeError("Missing APP_SCRIPT_BRIDGE_URL or VIDEO_BRIDGE_TOKEN secret")
    body = {"action": action, "token": TOKEN, **payload}
    response = requests.post(BRIDGE_URL, json=body, timeout=TIMEOUT)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Apps Script bridge did not return JSON; verify the deployed /exec URL and access setting") from exc
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error") or "Apps Script bridge returned ok=false"))
    return data


def process_one() -> bool:
    claimed = api("claim")
    job = claimed.get("job")
    if not job:
        print("No queued video jobs.")
        return False
    job_id = str(job.get("job_id") or "")
    if not job_id:
        raise RuntimeError("Claimed job has no job_id")
    print(f"Rendering {job_id} / {job.get('content_id')} / {job.get('format')}")
    try:
        with tempfile.TemporaryDirectory(prefix="legalhustl-job-") as tmp:
            job_file = Path(tmp) / "job.json"
            video_file = Path(tmp) / "legalhustl.mp4"
            job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            build_video(job, video_file)
            raw = video_file.read_bytes()
            if len(raw) > 9_000_000:
                raise RuntimeError(f"Video is too large for safe Apps Script upload: {len(raw)} bytes")
            encoded = base64.b64encode(raw).decode("ascii")
            uploaded = api(
                "complete",
                job_id=job_id,
                file_name=f"{job.get('content_id','LEGALHUSTL')}_{job.get('format','Reels')}.mp4",
                mime_type="video/mp4",
                video_base64=encoded,
                video_bytes=len(raw),
            )
            print(f"Uploaded {len(raw)} bytes; Drive URL stored for job {job_id}.")
            print("Video URL: " + str(uploaded.get("video_url", "")))
        return True
    except Exception as exc:
        message = str(exc)[:1800]
        try:
            api("fail", job_id=job_id, error=message)
        except Exception as fail_exc:
            print("Could not report failure back to Apps Script: " + str(fail_exc))
        raise


def main():
    if not BRIDGE_URL or not TOKEN:
        raise SystemExit("Required GitHub Actions secrets are not configured.")
    processed = 0
    for _ in range(max(1, min(MAX_JOBS, 3))):
        try:
            did_work = process_one()
            if not did_work:
                break
            processed += 1
        except Exception as exc:
            print("Worker stopped: " + str(exc))
            raise
    print(f"Worker finished. Jobs rendered: {processed}")


if __name__ == "__main__":
    main()
