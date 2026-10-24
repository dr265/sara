import os
import time
import requests
import logging

log = logging.getLogger(__name__)

class HiggsfieldClient:
    """
    Client for Higgsfield AI Video Generation.
    Uses the All Models REST API.
    """
    def __init__(self):
        self.api_key_id = os.getenv("HIGGSFIELD_API_KEY_ID", "")
        self.api_secret = os.getenv("HIGGSFIELD_API_KEY_SECRET", "")
        self.base_url = "https://api.higgsfield.ai"
        # The user requested "the best free models". The 'soul/v2/standard' is a common default,
        # but there might be other endpoints. Let's use it as the default model.
        self.default_model = "higgsfield-ai/soul/v2/standard"

    def _get_headers(self):
        if not self.api_key_id or not self.api_secret:
            raise ValueError("HIGGSFIELD_API_KEY_ID or HIGGSFIELD_API_KEY_SECRET is not set in environment.")
        return {
            "Authorization": f"Key {self.api_key_id}:{self.api_secret}",
            "Content-Type": "application/json"
        }

    def generate_video_sync(self, prompt: str, model: str = None) -> str:
        """
        Synchronously block and poll the API until the video is generated.
        Returns the output URL of the generated video.
        """
        model = model or self.default_model
        url = f"{self.base_url}/{model.lstrip('/')}"
        
        headers = self._get_headers()
        payload = {"prompt": prompt}

        log.info(f"Submitting video generation to Higgsfield model: {model}...")
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
        except Exception as exc:
            log.error(f"Failed to submit Higgsfield request: {exc}")
            raise RuntimeError(f"Higgsfield request failed: {exc}")

        data = response.json()
        status_url = data.get("status_url")
        request_id = data.get("request_id")

        if not status_url:
            raise RuntimeError(f"Higgsfield API did not return a status_url. Response: {data}")

        log.info(f"Higgsfield generation started (ID: {request_id}). Polling {status_url}...")

        # Poll the status URL until completed or failed
        max_attempts = 60 # 5 mins max
        for attempt in range(max_attempts):
            time.sleep(5)
            try:
                status_resp = requests.get(status_url, headers=headers, timeout=10)
                status_resp.raise_for_status()
                status_data = status_resp.json()
                status = status_data.get("status", "").lower()

                if status == "completed":
                    output_url = status_data.get("output_url")
                    log.info(f"Higgsfield video generation completed! URL: {output_url}")
                    return output_url
                elif status in ("failed", "error"):
                    err = status_data.get("error", "Unknown error")
                    raise RuntimeError(f"Higgsfield generation failed: {err}")
                
                # If "processing" or "queued", continue polling
            except requests.exceptions.RequestException as exc:
                log.warning(f"Error polling Higgsfield status (attempt {attempt+1}/{max_attempts}): {exc}")

        raise RuntimeError("Higgsfield video generation timed out.")
