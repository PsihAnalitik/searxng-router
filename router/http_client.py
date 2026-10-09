"""One paid request per child process, with a wall-clock deadline and no retries."""
import base64
import http.client
import json
import math
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request


def request_json(request, timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("HTTP deadline must be finite and positive")
    message = {"url": request.full_url, "method": request.get_method(),
               "headers": dict(request.header_items()), "timeout": timeout,
               "data": base64.b64encode(request.data).decode() if request.data is not None else None}
    try:
        # Credentials travel over stdin, not command-line arguments or logs.
        response = subprocess.run(
            [sys.executable, "-B", str(pathlib.Path(__file__).resolve()), "--worker"],
            input=json.dumps(message).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills and reaps the worker before raising.
        raise TimeoutError("Paid HTTP deadline exceeded") from exc
    if response.returncode:
        raise ValueError("Paid HTTP worker failed")
    result = json.loads(response.stdout)
    if result.get("error") == "http":
        raise urllib.error.HTTPError("", result["status"], "Provider HTTP error", {}, None)
    if result.get("error") == "timeout":
        raise TimeoutError("Provider socket timeout")
    if result.get("error"):
        raise ValueError("Invalid provider response")
    return result["payload"]


def main():
    message = json.load(sys.stdin)
    data = base64.b64decode(message["data"]) if message["data"] is not None else None
    request = urllib.request.Request(message["url"], data=data, headers=message["headers"],
                                     method=message["method"])
    try:
        with urllib.request.urlopen(request, timeout=message["timeout"]) as response:
            result = {"payload": json.load(response)}
    except urllib.error.HTTPError as exc:
        result = {"error": "http", "status": exc.code}
        exc.close()
    except TimeoutError:
        result = {"error": "timeout"}
    except (OSError, ValueError, http.client.HTTPException):
        result = {"error": "response"}
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
