"""Functional smoke test against the DEPLOYED stack (localhost:8080).

Exercises the REST surface (auth, conversations, status) and the live WS
pipeline (session.start -> PCM -> caption.partial/final -> session.end), then
checks the session persisted. Not part of CI — a manual end-to-end check of the
running stack.

Credentials come from TENIR_USERNAME / TENIR_PASSWORD (defaults match the
compose bootstrap-admin envs, so set those or these).
"""

import asyncio
import json
import os
import sys

import httpx
import websockets

# 127.0.0.1, not "localhost": on Windows the latter resolves to ::1 first and the
# published port is IPv4-only, which hangs the WS TCP connect.
BASE = "http://127.0.0.1:8080"
WS = "ws://127.0.0.1:8080/ws"

USERNAME = os.environ.get("TENIR_USERNAME", "admin")
PASSWORD = os.environ.get("TENIR_PASSWORD", "")

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def pcm_silence(seconds=1.0):
    # 16kHz s16le mono silence is enough for the stub transcriber; the real
    # Voxtral backend just produces empty/near-empty captions for it.
    return b"\x00\x00" * int(16000 * seconds)


def login(client: httpx.Client) -> str:
    r = client.post(f"{BASE}/auth/login", json={"username": USERNAME, "password": PASSWORD})
    check("auth: login", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    if r.status_code != 200:
        print("cannot continue without a token — set TENIR_USERNAME/TENIR_PASSWORD")
        sys.exit(1)
    return r.json()["token"]


def rest_tests(client: httpx.Client, headers: dict) -> None:
    r = client.get(f"{BASE}/health")
    check("health", r.status_code == 200 and r.json().get("status") == "ok", r.text[:120])

    r = client.get(f"{BASE}/status")
    check("status", r.status_code == 200 and "overall" in r.json(), r.text[:120])

    r = client.get(f"{BASE}/conversations", headers=headers)
    check("conversations: list", r.status_code == 200, r.text[:120])

    r = client.get(f"{BASE}/conversations", headers={})
    check("conversations: 401 without token", r.status_code == 401, str(r.status_code))


async def ws_test(token: str) -> str | None:
    session_id = None
    got_final = False
    async with websockets.connect(f"{WS}?token={token}") as ws:
        await ws.send(json.dumps({"type": "session.start", "micSource": "phone-microphone"}))
        ready = json.loads(await ws.recv())
        check("ws: session.ready", ready.get("type") == "session.ready", str(ready)[:120])
        session_id = ready.get("sessionId")

        # ~3s of audio in 100ms frames, then a beat for finals to flush.
        for _ in range(30):
            await ws.send(pcm_silence(0.1))
            await asyncio.sleep(0.02)
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                if msg.get("type") == "caption.final":
                    got_final = True
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            pass

        await ws.send(json.dumps({"type": "session.end"}))
    check("ws: caption.final received", got_final)
    return session_id


def persistence_tests(client: httpx.Client, headers: dict, session_id: str | None) -> None:
    if not session_id:
        check("persistence: session id", False, "no session id from ws test")
        return
    r = client.get(f"{BASE}/conversations/{session_id}", headers=headers)
    check("persistence: conversation stored", r.status_code == 200, r.text[:160])
    if r.status_code == 200:
        conv = r.json()
        check("persistence: status ready", conv.get("status") == "ready", conv.get("status"))
    r = client.delete(f"{BASE}/conversations/{session_id}", headers=headers)
    check("persistence: delete", r.status_code == 204, str(r.status_code))


def main() -> None:
    with httpx.Client(timeout=10) as client:
        token = login(client)
        headers = {"Authorization": f"Bearer {token}"}
        rest_tests(client, headers)
        session_id = asyncio.run(ws_test(token))
        persistence_tests(client, headers, session_id)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
