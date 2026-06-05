"""Verify every external credential is working before we run a full RCA.

Posts a test message to #slack-testing on success.
Run:  python scripts/credentials_check.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rca_agent.core.settings import settings  # noqa: E402


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    s = settings()
    failures: list[str] = []

    # ---------- Anthropic ----------
    section("Anthropic")
    if not s.ANTHROPIC_API_KEY:
        failures.append("ANTHROPIC_API_KEY is empty")
        print("FAIL: ANTHROPIC_API_KEY not set")
    else:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=s.ANTHROPIC_API_KEY)
            r = client.messages.create(
                model=s.LLM_MODEL,
                max_tokens=20,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            )
            text = "".join(b.text for b in r.content if getattr(b, "type", None) == "text").strip()
            print(f"ok — model={s.LLM_MODEL}  reply={text!r}")
        except Exception as e:
            failures.append(f"Anthropic: {e}")
            print(f"FAIL: {e}")

    # ---------- Slack ----------
    section("Slack")
    test_channel_id: str | None = None
    test_channel_name = "slack-testing"
    if not s.SLACK_BOT_TOKEN:
        failures.append("SLACK_BOT_TOKEN empty")
        print("FAIL: SLACK_BOT_TOKEN not set")
    else:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
        slack = WebClient(token=s.SLACK_BOT_TOKEN)
        try:
            auth = slack.auth_test()
            print(f"ok — auth.test  team={auth['team']}  bot={auth['user']}  user_id={auth['user_id']}")
        except SlackApiError as e:
            failures.append(f"Slack auth.test: {e.response.get('error')}")
            print(f"FAIL auth.test: {e.response.get('error')}")
            slack = None  # type: ignore

        if slack is not None:
            try:
                cursor = None
                while True:
                    resp = slack.conversations_list(
                        types="public_channel,private_channel",
                        limit=200,
                        cursor=cursor,
                        exclude_archived=True,
                    )
                    for c in resp.get("channels", []):
                        if c.get("name") == test_channel_name:
                            test_channel_id = c["id"]
                            break
                    if test_channel_id:
                        break
                    cursor = resp.get("response_metadata", {}).get("next_cursor")
                    if not cursor:
                        break
                if test_channel_id:
                    print(f"ok — found #{test_channel_name}  id={test_channel_id}")
                else:
                    print(f"WARN: #{test_channel_name} not visible to bot. "
                          "Either it doesn't exist, or invite the bot: /invite @<bot>")
            except SlackApiError as e:
                failures.append(f"conversations.list: {e.response.get('error')}")
                print(f"FAIL conversations.list: {e.response.get('error')}")

            if test_channel_id:
                try:
                    msg = slack.chat_postMessage(
                        channel=test_channel_id,
                        text="🧪 sla-crushers credential check — all tokens validated.",
                        blocks=[
                            {"type": "header", "text": {"type": "plain_text", "text": "sla-crushers — credential check"}},
                            {"type": "section", "text": {"type": "mrkdwn",
                                "text": ":white_check_mark: Anthropic\n"
                                       ":white_check_mark: Slack auth + posting\n"
                                       ":hourglass_flowing_sand: ClickUp + GitHub checking next"}},
                            {"type": "context", "elements": [{"type": "mrkdwn",
                                "text": "_Posted by the RCA agent. Reply with a Slack message link to test the full pipeline._"}]},
                        ],
                    )
                    permalink = slack.chat_getPermalink(
                        channel=test_channel_id, message_ts=msg["ts"]
                    ).get("permalink")
                    print(f"ok — posted message  ts={msg['ts']}  permalink={permalink}")
                except SlackApiError as e:
                    failures.append(f"chat.postMessage: {e.response.get('error')}")
                    print(f"FAIL chat.postMessage: {e.response.get('error')}")

    # ---------- ClickUp ----------
    section("ClickUp")
    if not s.CLICKUP_API_TOKEN:
        failures.append("CLICKUP_API_TOKEN empty")
        print("FAIL: CLICKUP_API_TOKEN not set")
    else:
        import httpx
        try:
            r = httpx.get(
                "https://api.clickup.com/api/v2/team",
                headers={"Authorization": s.CLICKUP_API_TOKEN},
                timeout=10,
            )
            if r.status_code == 200:
                teams = r.json().get("teams", [])
                names = [t.get("name") for t in teams]
                print(f"ok — auth ok  teams={names}")
            else:
                failures.append(f"ClickUp HTTP {r.status_code}: {r.text[:120]}")
                print(f"FAIL HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            failures.append(f"ClickUp: {e}")
            print(f"FAIL: {e}")

    # ---------- GitHub ----------
    section("GitHub")
    if not s.GITHUB_TOKEN:
        print("SKIP: GITHUB_TOKEN not set (PR feature disabled)")
    else:
        try:
            from github import Github
            gh = Github(s.GITHUB_TOKEN)
            user = gh.get_user()
            print(f"ok — authenticated as {user.login}")
            try:
                repo = gh.get_repo(s.GITHUB_REPO)
                print(f"ok — can access {s.GITHUB_REPO}  default_branch={repo.default_branch}")
            except Exception as e:
                failures.append(f"GitHub repo access: {e}")
                print(f"FAIL repo access {s.GITHUB_REPO}: {e}")
        except Exception as e:
            failures.append(f"GitHub: {e}")
            print(f"FAIL: {e}")

    # ---------- Snowflake ----------
    section("Snowflake")
    if not os.getenv("SF_USER") or not os.getenv("SF_RSA_KEY"):
        print("SKIP: SF_USER / SF_RSA_KEY not set (auto-loaded from scraping/.env)")
    else:
        try:
            from rca_agent.tools.snowflake_tool import _execute as sf_exec
            res = sf_exec("SELECT CURRENT_VERSION() AS v, CURRENT_ROLE() AS role", default_limit=1)
            print(f"ok — {res['rows'][0] if res['rows'] else '(no rows)'}")
        except Exception as e:
            failures.append(f"Snowflake: {e}")
            print(f"FAIL: {e}")

    # ---------- Postgres ----------
    section("Postgres")
    try:
        from rca_agent.tools.postgres_tool import _execute as pg_exec
        res = pg_exec("SELECT current_database() AS db, current_user AS u", limit=1)
        print(f"ok — {res['rows'][0] if res['rows'] else '(no rows)'}")
    except Exception as e:
        failures.append(f"Postgres: {e}")
        print(f"FAIL: {e}")

    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CREDENTIALS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
