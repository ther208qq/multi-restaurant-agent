"""Manual live smoke: first investigation + follow-up on one thread.

Run the isolated eval server first (the public dataset covers 2023 only):

    PORT=8010 .venv/Scripts/python.exe backend/run_eval.py
    .venv/Scripts/python.exe scripts/smoke_diagnosis_investigation.py

The script logs in as ``eval_admin``, investigates an explicit historical window
with real data, then continues the same thread with a different window. It
prints only safe response fields -- never the access token or API key -- and
relies on ``trust_env=False`` so a system proxy cannot intercept localhost.
"""

import httpx

BASE = "http://127.0.0.1:8010"


def summarize(tag: str, body: dict) -> None:
    window = body["resolved_window"]
    print(f"\n=== {tag} ===")
    print("status:", body["status"])
    print("thread:", body["thread_id"])
    print("window:", window["start_date"], "->", window["end_date"],
          "| baseline:", window["baseline_start_date"], "->", window["baseline_end_date"])
    print("tools:", [step["tool"] for step in body["steps"]])
    print("budget:", body["budget"])
    print("answer:", body["answer"])
    print("facts:", len(body["report"]["facts"]),
          "inferences:", len(body["report"]["inferences"]),
          "checks:", len(body["report"]["checks"]),
          "hypotheses:", len(body["hypotheses"]),
          "dropped:", body["report"]["dropped"],
          "source:", body["report"]["source"])
    print("notes:", body["notes"])


def main() -> None:
    with httpx.Client(timeout=240, trust_env=False) as client:
        login = client.post(
            f"{BASE}/api/staff/login",
            json={"username": "eval_admin", "password": "admin123"},
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        branches = client.get(f"{BASE}/api/staff/branches", headers=headers)
        branches.raise_for_status()
        branch = branches.json()[0]
        print("branch:", branch["id"], branch["name"])

        first = client.post(
            f"{BASE}/api/staff/diagnosis/investigate",
            json={
                "question": "这段时间生意为什么下降？",
                "branch_id": branch["id"],
                # The public dataset covers 2023-01-01..2023-06-30, so use an
                # explicit historical window for a smoke with real findings.
                "start_date": "2023-05-01",
                "end_date": "2023-05-31",
            },
            headers=headers,
        )
        first.raise_for_status()
        body = first.json()
        summarize("first turn", body)

        if body["status"] == "llm_unavailable":
            print("\nprovider unavailable; follow-up smoke skipped")
            return

        follow = client.post(
            f"{BASE}/api/staff/diagnosis/investigate",
            json={
                "question": "那这个月呢？",
                "thread_id": body["thread_id"],
                "branch_id": branch["id"],
                "start_date": "2023-06-01",
                "end_date": "2023-06-30",
            },
            headers=headers,
        )
        follow.raise_for_status()
        second = follow.json()
        summarize("follow-up turn", second)

        changed = (
            second["resolved_window"]["start_date"],
            second["resolved_window"]["end_date"],
        ) != (
            body["resolved_window"]["start_date"],
            body["resolved_window"]["end_date"],
        )
        print("\nwindow changed on follow-up:", changed)
        print("same thread:", second["thread_id"] == body["thread_id"])


if __name__ == "__main__":
    main()
