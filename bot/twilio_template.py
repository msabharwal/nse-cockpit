#!/usr/bin/env python3
"""
twilio_template.py — create the NSE Cockpit WhatsApp template in Twilio and
submit it to WhatsApp for approval (Utility category). Run it once, on your
own machine — your credentials stay with you.

  export TWILIO_ACCOUNT_SID=AC...
  export TWILIO_AUTH_TOKEN=...
  python bot/twilio_template.py create        # creates + submits, prints the HX… Content SID
  python bot/twilio_template.py status HX...  # pending / approved / rejected (+ reason)

When it shows "approved", put the HX… value in:
  - GitHub secret TWILIO_CONTENT_SID              (one-way sender)
  - npx wrangler secret put TWILIO_CONTENT_SID    (two-way bot, if deployed)
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

NAME = "nse_daily_update"
# Meta rejects templates that start/end with a variable or are mostly variables,
# so the fixed text around {{1}} and {{2}} is deliberate. No emojis (needed for
# Twilio's Direct Send, and safer for approval).
BODY = (
    "NSE Cockpit end-of-day update for {{1}}.\n\n"
    "{{2}}\n\n"
    "This is the daily NSE market data summary you signed up for. "
    "Information only, not investment advice."
)
SAMPLES = {
    "1": "14 Nov 2026",
    "2": "Breadth 1310 up / 1246 down · Gainers: KMEW +3.00%, RITES +2.99% · "
         "Watchlist: TCS ₹2,436.06 (+1.19%), INFY ₹1,161.7 (+1.77%) · Reply TODAY for details",
}


def call(method, url, payload=None):
    sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        sys.exit(f"Twilio error {e.code}: {e.read().decode(errors='replace')}")


def create():
    content = call("POST", "https://content.twilio.com/v1/Content", {
        "friendly_name": NAME,
        "language": "en",
        "variables": SAMPLES,
        "types": {"twilio/text": {"body": BODY}},
    })
    hx = content["sid"]
    print(f"Created template {NAME}: {hx}")
    appr = call("POST", f"https://content.twilio.com/v1/Content/{hx}/ApprovalRequests/whatsapp",
                {"name": NAME, "category": "UTILITY"})
    print(f"Submitted to WhatsApp: status = {appr.get('status')}")
    print(f"\nCheck later with:  python bot/twilio_template.py status {hx}")


def status(hx):
    r = call("GET", f"https://content.twilio.com/v1/Content/{hx}/ApprovalRequests")
    wa = r.get("whatsapp") or {}
    print(f"{wa.get('name')}: {wa.get('status')}  (category {wa.get('category')})")
    if wa.get("rejection_reason"):
        print("Rejection reason:", wa["rejection_reason"])
    if wa.get("status") == "approved":
        print(f"\nSet TWILIO_CONTENT_SID = {hx}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "create":
        create()
    elif len(sys.argv) >= 3 and sys.argv[1] == "status":
        status(sys.argv[2])
    else:
        print(__doc__)
