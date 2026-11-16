#!/usr/bin/env python3
"""
send_whatsapp.py — one-way evening update via Twilio WhatsApp, straight from
GitHub Actions (no server needed). Reads output/bot.json.

Environment (GitHub repo secrets):
  TWILIO_ACCOUNT_SID     AC…
  TWILIO_AUTH_TOKEN      …
  TWILIO_WHATSAPP_FROM   your Twilio WhatsApp number, e.g. +14155238886 (sandbox) or your own
  WHATSAPP_RECIPIENTS    numbers with optional watchlists, separated by ';' or newlines:
                           +919812345678=TCS,INFY,RELIANCE; +919811111111
  TWILIO_CONTENT_SID     optional HX… approved template (see whatsapp-bot/README.md).
                         Set it for production: WhatsApp only delivers free-form text to
                         people who messaged the number in the last 24 hours.
                         Without it, the full update is sent as free-form text (fine for
                         the sandbox and for testing).

Usage:  python bot/send_whatsapp.py [--dry-run]     (stdlib only)
"""

import base64
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

FEED = Path(__file__).resolve().parent.parent / "output" / "bot.json"
TWILIO_MAX = 1600       # Twilio's per-message body limit for WhatsApp
VAR_MAX = 900           # keep template variables well under WhatsApp's 1024-char body limit
FOOTER = "_Model output for family use — not investment advice._"


# ── formatting (mirrors whatsapp-bot/src/feed.js) ────────────────────────────
def inr(x):
    if x is None:
        return "–"
    neg, x = x < 0, abs(x)
    whole, frac = f"{x:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    s = ",".join(groups + [tail]) if groups else tail
    frac = frac.rstrip("0")
    return ("-" if neg else "") + "₹" + s + (f".{frac}" if frac else "")


def pct(x):
    return "" if x is None else f"{'+' if x > 0 else ''}{x:.2f}%"


def date_label(d):
    dt = date.fromisoformat(d)
    return f"{dt.day} {dt.strftime('%b %Y')}"


def lvl(x):
    """Compact price for levels: whole rupees above ₹100, else 2 decimals."""
    return inr(math.floor(x + 0.5)) if x >= 100 else inr(x)   # round half up, same as JS


def pick_lines(picks):
    out = []
    for i, k in enumerate(picks, 1):
        out.append(f"{i}. *{k['s']}* {inr(k['close'])} · score {k['score']}")
        out.append(f"   Entry {lvl(k['entry'][0])}–{lvl(k['entry'][1])} · SL {lvl(k['sl'])} · Target {lvl(k['tgt'])}")
    return out


def stock_line(r):
    s, p, c, rsi = r[0], r[1], r[2], r[3]
    return f"• {s} {inr(p)}" + (f" ({pct(c)})" if c is not None else "") + (
        f" · RSI {round(rsi)}" if rsi is not None else "")


def digest_text(feed, idx, watch):
    g = feed.get("digest") or {}
    L = [f"*NSE Cockpit · {date_label(feed['d'])} close*", ""]
    b = g.get("breadth")
    if b:
        L.append(f"Breadth: {b['adv']} up · {b['dec']} down · {b['unch']} flat")
    L.append(f"{g.get('above_sma10', '–')} of {feed['n']} stocks above their 10-day average")
    L.append(f"RSI ≥ 70: {g.get('overbought', '–')} stocks · RSI ≤ 30: {g.get('oversold', '–')}")

    picks = feed.get("picks") or []
    if picks:
        L.extend(["", f"*Cockpit picks* (score ≥ 65, levels from ATR)", *pick_lines(picks)])

    def section(title, syms, fmt=stock_line):
        rows = [idx[s] for s in syms or [] if s in idx]
        if rows:
            L.extend(["", f"*{title}*", *map(fmt, rows)])

    section("Top gainers", g.get("gainers"))
    section("Top losers", g.get("losers"))
    section("Unusual volume", g.get("volume"),
            lambda r: f"• {r[0]} {r[4]:.1f}× avg volume" + (f" ({pct(r[2])})" if r[2] is not None else ""))
    if watch:
        L.extend(["", "*Your watchlist*"])
        L.extend(stock_line(idx[s]) if s in idx else f"• {s} — no data today" for s in watch)
    L.extend(["", FOOTER])
    return "\n".join(L)


def summary_line(feed, idx, watch):
    """Single line (template variables can't contain line breaks)."""
    g = feed.get("digest") or {}
    parts = []
    picks = feed.get("picks") or []
    if picks:
        parts.append("Picks: " + "; ".join(
            f"{k['s']} ({k['score']}) entry {lvl(k['entry'][0])}-{lvl(k['entry'][1])} SL {lvl(k['sl'])} TGT {lvl(k['tgt'])}"
            for k in picks))
    b = g.get("breadth")
    if b:
        parts.append(f"Breadth {b['adv']} up / {b['dec']} down")
    def short(syms, k=3):
        return ", ".join(f"{s} {pct(idx[s][2])}" for s in (syms or [])[:k] if s in idx and idx[s][2] is not None)
    if short(g.get("gainers")):
        parts.append("Gainers: " + short(g.get("gainers")))
    if short(g.get("losers")):
        parts.append("Losers: " + short(g.get("losers")))
    vol = [f"{s} {idx[s][4]:.0f}×" for s in (g.get("volume") or [])[:3] if s in idx]
    if vol:
        parts.append("Heavy volume: " + ", ".join(vol))
    if watch:
        w = []
        for s in watch:
            r = idx.get(s)
            w.append(f"{s} {inr(r[1])}" + (f" ({pct(r[2])})" if r and r[2] is not None else "") if r else f"{s} n/a")
        parts.append("Watchlist: " + ", ".join(w))
    line = " · ".join(parts) or f"{feed['n']} NSE stocks updated"
    return line if len(line) <= VAR_MAX else line[: VAR_MAX - 1] + "…"


def chunks(text, limit=TWILIO_MAX):
    """Split on line boundaries so each message fits Twilio's limit."""
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur.rstrip("\n"))
    return out


# ── Twilio ───────────────────────────────────────────────────────────────────
def twilio_send(fields):
    sid, token = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return True, json.load(res).get("sid")
    except urllib.error.HTTPError as e:
        return False, e.read().decode(errors="replace")[:300]


def wa(num):
    return "whatsapp:+" + re.sub(r"\D", "", num)


def parse_recipients(raw):
    out = []
    for item in re.split(r"[;\n]+", raw or ""):
        item = item.strip()
        if not item:
            continue
        num, _, syms = item.partition("=")
        watch = [s.strip().upper() for s in syms.split(",") if s.strip()]
        if re.sub(r"\D", "", num):
            out.append((num.strip(), watch))
    return out


def main():
    dry = "--dry-run" in sys.argv
    feed = json.loads(FEED.read_text())
    idx = {r[0]: r for r in feed["rows"]}
    recipients = parse_recipients(os.environ.get("WHATSAPP_RECIPIENTS"))
    content_sid = os.environ.get("TWILIO_CONTENT_SID", "").strip()
    if not recipients:
        sys.exit("WHATSAPP_RECIPIENTS is empty — nothing to send")

    sent = failed = 0
    for num, watch in recipients:
        if content_sid:
            payloads = [{"ContentSid": content_sid, "ContentVariables": json.dumps(
                {"1": date_label(feed["d"]), "2": summary_line(feed, idx, watch)}, ensure_ascii=False)}]
        else:
            payloads = [{"Body": part} for part in chunks(digest_text(feed, idx, watch))]
        for p in payloads:
            fields = {"From": wa(os.environ.get("TWILIO_WHATSAPP_FROM", "")), "To": wa(num), **p}
            if dry:
                print(f"--- to {fields['To']} ---\n{p.get('Body') or p}\n")
                continue
            ok, info = twilio_send(fields)
            sent, failed = sent + ok, failed + (not ok)
            print(f"{'✓' if ok else '✗'} {fields['To'][:-4]}****  {info}")
    if not dry:
        print(f"Done: {sent} sent, {failed} failed")
        if failed and not sent:
            sys.exit(1)


if __name__ == "__main__":
    main()
