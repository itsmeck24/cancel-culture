#!/usr/bin/env python3
"""Cancel Culture subscription ledger: deadlines, student discounts and renewal decisions.

Cubbie reads the emails, screenshots or pasted text itself and passes the extracted facts in as
JSON. This script never reads email and never contacts a website. It only stores evidence,
merges later messages over earlier ones, does the date math and remembers which reminders exist.

State lives in ~/.config/cancel-culture/trials.json (or $SKILL_DATA_DIR/trials.json).

Exit codes: 0 ok, 2 bad arguments or input, 3 unknown trial id, 4 saved state unreadable (left as is).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path

KINDS = {"trial_started", "dates_changed", "price_changed", "renewal_notice", "cancelled", "charged", "discount_expiring", "discount_renewed", "other"}
ACTIVE_KINDS = {"trial_started", "dates_changed", "price_changed", "renewal_notice", "discount_expiring"}
DATE_KINDS = {"trial_started", "dates_changed", "renewal_notice", "discount_expiring"}
DATE_FIELDS = ("trial_end", "charge_at", "cancel_by")
PRICE_FIELDS = ("amount", "currency", "interval", "price_note")
OTHER_FIELDS = ("biller", "biller_name", "immediate_access_loss", "access_ends_at")
BILLERS = {"direct", "apple", "google_play", "paypal", "other", "unknown"}
USER_MARKS = {"cancel_requested", "cancellation_reported", "reverification_reported", "kept", "active"}


class Problem(Exception):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


# ---------- time ----------

def get_zone(name: str, fallback_offset: timedelta | None, warnings: list) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # missing tz database or unknown name
        if fallback_offset is None:
            raise Problem(f"Unknown time zone '{name}'. Pass an IANA name such as America/Phoenix.")
        warnings.append(f"Time zone data for {name} unavailable; using the fixed offset from --now. "
                        "Times after a daylight-saving change may be off by an hour.")
        return timezone(fallback_offset)


def parse_now(text: str) -> datetime:
    try:
        now = datetime.fromisoformat(text)
    except ValueError:
        raise Problem("--now must be the student's local date and time with its UTC offset, "
                      "for example 2026-10-24T15:30-07:00.")
    if now.tzinfo is None:
        raise Problem("--now needs a UTC offset, for example 2026-10-24T15:30-07:00.")
    return now


def parse_sent(value, zone: tzinfo) -> tuple[datetime, bool]:
    """Read a message's sent date. Returns (aware datetime, exact). A date with no time, or a time
    with no zone, is read in the student's zone so screenshots and forwards still work."""
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.combine(date.fromisoformat(text), time(0, 0), tzinfo=zone), False
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise Problem(f"Can't read sent_at '{text}'. Use 2026-10-20T09:00-07:00, or 2026-10-20 if only the date is shown.")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone), False
    return dt, True


def sent_key(e: dict) -> datetime:
    return parse_sent(e["sent_at"], timezone.utc)[0]


def parse_when(value, zone: tzinfo, notes: list, label: str):
    """Return (aware datetime, precision) or (None, None). Date-only values become the start of that
    day in the student's zone (the conservative reading) with precision 'date'."""
    if value in (None, "", "unknown"):
        return None, None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            d = date.fromisoformat(text)
        except ValueError:
            raise Problem(f"Invalid date for {label}: {text}")
        return datetime.combine(d, time(0, 0), tzinfo=zone), "date"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise Problem(f"Can't read {label} '{text}'. Use 2026-11-05T12:00-07:00, or 2026-11-05 when no time is given.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zone)
        notes.append(f"{label} had no time zone; read it in the student's zone.")
    return dt, "datetime"


def fmt(dt: datetime | None, zone: tzinfo, precision: str | None = "datetime") -> str | None:
    if dt is None:
        return None
    local = dt.astimezone(zone)
    day = f"{local:%a %b} {local.day}"
    if precision == "date":
        return f"{day} (time not stated)"
    hour = local.hour % 12 or 12
    minute = f":{local.minute:02d}"
    return f"{day}, {hour}{minute} {'AM' if local.hour < 12 else 'PM'} {local.tzname() or ''}".strip()


# ---------- state ----------

def state_path() -> Path:
    base = os.environ.get("SKILL_DATA_DIR") or (Path.home() / ".config" / "cancel-culture")
    return Path(base) / "trials.json"


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "trials": {}}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("trials"), dict):
            raise ValueError("unexpected shape")
        return data
    except (OSError, ValueError) as exc:
        raise Problem(f"Saved trials at {path} can't be read ({exc}). Left untouched.", 4)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    json.loads(tmp.read_text())  # validate before replacing
    os.replace(tmp, path)


def norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def fingerprint(item: dict) -> str:
    raw = "|".join(str(item.get(k) or "") for k in ("sender", "subject", "sent_at", "service", "kind"))
    return "fp-" + hashlib.sha1(raw.encode()).hexdigest()[:10]


def new_id(state: dict, service: str, account: str | None) -> str:
    base = norm(service)[:20] or "trial"
    if account:
        base += "-" + norm(account)[:8]
    tid, n = base, 2
    while tid in state["trials"]:
        tid, n = f"{base}-{n}", n + 1
    return tid


# ---------- evidence ----------

def clean_item(item: dict, now: datetime, zone: tzinfo) -> tuple[dict, list]:
    warnings = []
    if not isinstance(item, dict) or not str(item.get("service") or "").strip():
        raise Problem("Every evidence item needs a 'service' name.")
    out = {k: item.get(k) for k in (
        "source", "sender", "subject", "sent_at", "service", "account", "kind", "biller", "biller_name",
        "trial_end", "charge_at", "cancel_by", "access_ends_at", "amount", "currency", "interval",
        "price_note", "immediate_access_loss", "quote", "tz", "discount_ends", "reverify_by",
        "current_amount", "current_currency", "current_interval")}
    out["service"] = str(out["service"]).strip()
    kind = str(out.get("kind") or "other").strip().lower()
    if kind not in KINDS:
        warnings.append(f"Unknown kind '{kind}' for {out['service']}; stored as 'other'.")
        kind = "other"
    out["kind"] = kind
    biller = str(out.get("biller") or "unknown").strip().lower().replace(" ", "_")
    out["biller"] = biller if biller in BILLERS else "other"
    if out["biller"] == "other" and not out.get("biller_name") and biller not in BILLERS:
        out["biller_name"] = item.get("biller")
    if out.get("current_amount") is not None:
        try:
            out["current_amount"] = float(out["current_amount"])
        except (TypeError, ValueError):
            raise Problem("current_amount must be a number or null.")
    if out.get("amount") not in (None, ""):
        try:
            out["amount"] = float(out["amount"])
        except (TypeError, ValueError):
            raise Problem(f"amount for {out['service']} must be a number or null (put text like 'from $9' in price_note).")
    else:
        out["amount"] = None
    for field in ("amount", "current_amount"):
        if out.get(field) is not None and (not math.isfinite(out[field]) or out[field] < 0):
            raise Problem(f"{field} must be finite and non-negative.")
    if not out.get("sent_at"):
        out["sent_at"] = now.isoformat()
        warnings.append(f"No sent_at for a {out['service']} message; used the current time for ordering.")
    sent, exact = parse_sent(out["sent_at"], zone)
    if not exact:
        warnings.append(f"The {out['service']} message shows no exact send time; read it in the student's zone.")
    out["sent_at"] = sent.isoformat()
    if out.get("quote"):
        out["quote"] = str(out["quote"])[:240]
    out["source"] = str(out.get("source") or fingerprint(out))
    return out, warnings


def find_trial(state: dict, item: dict) -> tuple[str | None, str | None]:
    """Return (trial id, note). Match on service plus account; never merge two different accounts."""
    svc, acct = norm(item["service"]), norm(item.get("account"))
    same_service = [tid for tid, t in state["trials"].items() if norm(t["service"]) == svc]
    if acct:
        exact = [tid for tid in same_service if norm(state["trials"][tid].get("account")) == acct]
        if exact:
            return exact[0], None
        unnamed = [tid for tid in same_service if not state["trials"][tid].get("account")]
        if len(unnamed) == 1 and len(same_service) == 1:
            return unnamed[0], "matched to the only saved record for this service, which had no account yet"
        return None, None
    if len(same_service) == 1:
        note = None
        if state["trials"][same_service[0]].get("account"):
            note = "message names no account; matched by service name only"
        return same_service[0], note
    if len(same_service) > 1:
        return None, "ambiguous"
    return None, None


def ingest(state: dict, items: list, now: datetime, zone: tzinfo) -> tuple[list, list]:
    changes, warnings = [], []
    for raw in items:
        item, w = clean_item(raw, now, zone)
        if any(e["source"] == item["source"] for t in state["trials"].values() for e in t["evidence"]):
            continue
        warnings += w
        tid, note = find_trial(state, item)
        if note == "ambiguous":
            tid = new_id(state, item["service"], item.get("account") or "unmatched")
            state["trials"][tid] = {"service": item["service"], "account": item.get("account"),
                                    "evidence": [], "needs_review": "Could belong to more than one saved "
                                    f"{item['service']} account; ask the student which."}
            changes.append(f"{tid}: held for review (more than one {item['service']} account saved)")
        elif tid is None:
            tid = new_id(state, item["service"], item.get("account"))
            state["trials"][tid] = {"service": item["service"], "account": item.get("account"), "evidence": []}
            changes.append(f"{tid}: new")
        trial = state["trials"][tid]
        if item.get("account") and not trial.get("account"):
            trial["account"] = item["account"]
        if any(e["source"] == item["source"] for e in trial["evidence"]):
            continue  # already have this message: rescans add nothing
        if note and note != "ambiguous":
            item["match_note"] = note
        trial["evidence"].append(item)
        trial["evidence"].sort(key=sent_key)
        if not changes or not changes[-1].startswith(f"{tid}: new"):
            changes.append(f"{tid}: added {item['kind']} message ({item['source']})")
    return changes, warnings


# ---------- derived view ----------

def derive(trial: dict, zone: tzinfo, now: datetime) -> dict:
    notes: list = []
    ev = sorted(trial["evidence"], key=sent_key)
    latest = {}
    # Dates come as a group from the newest message that states any of them, so an older cutoff
    # never survives an extension that moved the dates.
    date_src = next((e for e in reversed(ev) if e["kind"] in DATE_KINDS and any(e.get(f) for f in DATE_FIELDS)), None)
    for f in DATE_FIELDS:
        latest[f] = (date_src or {}).get(f), (date_src or {}).get("source")
    for f in PRICE_FIELDS + OTHER_FIELDS:
        src = next((e for e in reversed(ev) if e.get(f) not in (None, "", "unknown")), None)
        latest[f] = (src.get(f), src.get("source")) if src else (None, None)
    older_dates = [e for e in ev if e is not date_src and e["kind"] in DATE_KINDS and any(e.get(f) for f in DATE_FIELDS)]
    if date_src and older_dates:
        old = older_dates[-1]
        notes.append(f"Dates from {date_src['source']} replace the earlier ones in {old['source']}.")

    zone_for = lambda src_id: zone
    parsed = {}
    for f in DATE_FIELDS + ("access_ends_at",):
        parsed[f] = parse_when(latest[f][0], zone_for(latest[f][1]), notes, f)

    event_source = next((e for e in reversed(ev) if e["kind"] in
                         {"trial_started", "renewal_notice", "discount_expiring", "discount_renewed"}), {})
    event_type = {"discount_expiring": "student_discount", "discount_renewed": "student_discount",
                  "renewal_notice": "paid_renewal"}.get(event_source.get("kind"), "trial")
    discount_source = next((e for e in reversed(ev) if e["kind"] == "discount_expiring"), {})
    biller = latest["biller"][0] or "unknown"
    deadline, precision, basis = None, None, None
    if event_type == "student_discount":
        discount_date = discount_source.get("reverify_by") or discount_source.get("discount_ends")
        deadline, precision = parse_when(discount_date, zone, notes, "student discount deadline")
        basis = ("reverification deadline stated in the message" if discount_source.get("reverify_by")
                 else "discount expiry stated; reverification cutoff not stated, check now")
    elif parsed["cancel_by"][0]:
        deadline, precision = parsed["cancel_by"]
        basis = "cancel-by date stated in the message"
    else:
        candidates = [parsed[f] for f in ("trial_end", "charge_at") if parsed[f][0]]
        if candidates:
            deadline, precision = min(candidates, key=lambda p: p[0])
            if biller == "apple" and event_type == "trial" and parsed["trial_end"][0]:
                deadline = deadline - timedelta(hours=24)
                basis = "Apple says to cancel a trial at least 24 hours before it ends"
            else:
                basis = "no cancel-by stated; using the trial end or charge date, so aim earlier"
    if precision == "date":
        notes.append("Only a date was given, so the deadline is treated as the start of that day.")

    status_ev = next((e for e in reversed(ev) if e["kind"] in ACTIVE_KINDS | {"cancelled", "charged", "discount_renewed"}), None)
    status = "needs_info"
    if status_ev:
        status = {"cancelled": "cancellation_evidenced", "charged": "charged", "discount_renewed": "discount_renewal_evidenced"}.get(status_ev["kind"], "active")
    mark = trial.get("user_mark")
    if mark and status_ev and parse_now(mark["at"]) >= sent_key(status_ev) and status == "active":
        status = {"active": "active"}.get(mark["as"], mark["as"])
    elif mark and not status_ev:
        status = mark["as"]
    if trial.get("needs_review"):
        status = "needs_review"
    if status == "active" and deadline is None:
        status = "needs_info"
    if status == "active" and deadline is not None and deadline <= now:
        status = "deadline_passed"

    # The student's own reason ("only until my presentation") never replaces the billing cutoff. It adds a
    # check-in after that need ends, as long as that is still before the cutoff.
    purpose = trial.get("purpose") or {}
    purpose_end, purpose_precision = parse_when(purpose.get("until"), zone, notes, "purpose end")
    purpose_due = bool(purpose_end and purpose_end <= now)
    review_after = None
    action_deadline, action_precision = deadline, precision
    action_reason = "student discount expiring" if event_type == "student_discount" else "upcoming subscription charge"
    if purpose.get("text"):
        action_reason = f"Student's stated purpose: {purpose['text']}"
        if purpose_due:
            action_reason += "; that need-until date has passed, so ask whether it is still needed"
        elif purpose_end and (deadline is None or purpose_end < deadline):
            review_after = purpose_end
            action_reason += "; check in after that need ends" + (", before the charge" if deadline else "")
    if deadline is None and purpose_end:
        action_deadline, action_precision = purpose_end, purpose_precision
    if status == "needs_info" and purpose_end:
        status = "review_now" if purpose_due else "active"
    hours_left = round((action_deadline - now).total_seconds() / 3600, 1) if action_deadline else None
    needs_action = status in ("active", "cancel_requested", "review_now")
    anchor = review_after or deadline or purpose_end

    rem = trial.get("reminders", [])
    reminder = "none"
    if rem:
        current = anchor.isoformat() if anchor else None
        if not needs_action:
            reminder = "remove (no longer needed)"
        elif any(r.get("for_deadline") != current for r in rem):
            reminder = "move (deadline changed since it was set)"
        else:
            reminder = "set"

    if event_type == "student_discount" and event_source.get("kind") == "discount_expiring":
        for f in PRICE_FIELDS:
            latest[f] = discount_source.get(f), discount_source.get("source")
    amount, currency = latest["amount"][0], latest["currency"][0]
    price = None
    if amount is not None:
        price = f"{currency or ''} {amount:.2f}".strip() + (f" per {latest['interval'][0]}" if latest["interval"][0] else "")
    elif latest["price_note"][0]:
        price = f"not confirmed ({latest['price_note'][0]})"
    elif status in ("active", "needs_info", "cancel_requested"):
        price = "not stated"

    match_notes = sorted({e["match_note"] for e in ev if e.get("match_note")})
    view = {
        "id": None,
        "service": trial["service"],
        "account": trial.get("account"),
        "status": status,
        "event_type": event_type,
        "action_reason": action_reason,
        "actions": (["reverify", "cancel", "keep", "remind"] if event_type == "student_discount"
                    else ["cancel", "keep", "remind"]) if needs_action else [],
        "action_by": fmt(action_deadline, zone, action_precision),
        "action_by_iso": action_deadline.isoformat() if action_deadline else None,
        "deadline_iso": deadline.isoformat() if deadline else None,
        "review_after": fmt(review_after, zone, purpose_precision) if review_after else None,
        "review_after_iso": review_after.isoformat() if review_after else None,
        "reminder_anchor_iso": anchor.isoformat() if anchor else None,
        "purpose": purpose.get("text"),
        "needed_until": fmt(purpose_end, zone, purpose_precision),
        "purpose_due": purpose_due,
        "current_price": ({"amount": discount_source.get("current_amount"),
                           "currency": discount_source.get("current_currency"),
                           "interval": discount_source.get("current_interval")}
                          if event_type == "student_discount" and discount_source.get("current_amount") is not None else None),
        "needs_action": needs_action,
        "cancel_by": fmt(deadline, zone, precision) if event_type != "student_discount" else None,
        "cancel_by_iso": deadline.isoformat() if deadline and event_type != "student_discount" else None,
        "basis": basis,
        "hours_left": hours_left,
        "charge": fmt(*parsed["charge_at"][:1], zone, parsed["charge_at"][1]) if parsed["charge_at"][0] else None,
        "trial_end": fmt(parsed["trial_end"][0], zone, parsed["trial_end"][1]) if parsed["trial_end"][0] else None,
        "price": price,
        "biller": biller if biller != "other" else (latest["biller_name"][0] or "other"),
        "access_ends": fmt(parsed["access_ends_at"][0], zone, parsed["access_ends_at"][1]) if parsed["access_ends_at"][0] else None,
        "immediate_access_loss": latest["immediate_access_loss"][0],
        "reminder": reminder,
        "reminders": [{"at": fmt(datetime.fromisoformat(r["at"]), zone), "ref": r.get("ref")} for r in rem],
        "sources": [e["source"] for e in ev],
        "quotes": [f"{e['source']}: {e['quote']}" for e in ev if e.get("quote")][-2:],
        "notes": notes + match_notes + ([trial["needs_review"]] if trial.get("needs_review") else []),
    }
    return {k: v for k, v in view.items() if v not in (None, [], "")}


ORDER = {"active": 0, "cancel_requested": 0, "review_now": 0, "needs_info": 1, "needs_review": 1, "deadline_passed": 2,
         "cancellation_reported": 3, "reverification_reported": 3, "kept": 4, "cancellation_evidenced": 5, "discount_renewal_evidenced": 5, "charged": 6}


def summary(state: dict, zone: tzinfo, now: datetime, only_open: bool) -> list:
    views = []
    for tid, trial in state["trials"].items():
        v = derive(trial, zone, now)
        v["id"] = tid
        views.append(v)
    views.sort(key=lambda v: (ORDER.get(v["status"], 9), datetime.fromisoformat(v["action_by_iso"]).timestamp() if v.get("action_by_iso") else float("inf")))
    if only_open:
        views = [v for v in views if v["status"] not in ("charged", "kept") or v.get("reminder", "none") != "none"]
    return views


# ---------- reminders ----------

def plan(trial_view: dict, zone: tzinfo, now: datetime, lead_hours: float, hour: int) -> dict:
    if not trial_view.get("needs_action"):
        return {"id": trial_view["id"], "plan": "no_reminder", "why": f"status is {trial_view['status']}"}
    if trial_view["status"] == "review_now":
        return {"id": trial_view["id"], "plan": "ask_now",
                "why": "the date the student said they needed it until has passed; ask whether they still need it"}
    deadline = datetime.fromisoformat(trial_view["deadline_iso"]) if trial_view.get("deadline_iso") else None
    note = None
    if trial_view.get("review_after_iso"):
        review = datetime.fromisoformat(trial_view["review_after_iso"])
        at = review.astimezone(zone).replace(hour=hour, minute=0, second=0, microsecond=0)
        at = max(at, review, now + timedelta(minutes=30))
        if deadline is None or at <= deadline - timedelta(hours=12):
            out = {"id": trial_view["id"], "plan": "remind", "kind": "after_need_ends", "at": fmt(at, zone),
                   "at_iso": at.isoformat(), "needed_until": trial_view.get("needed_until"),
                   "why": "after the need the student stated ends" + (", before the charge" if deadline else "")}
            if deadline:
                out["action_by"] = trial_view.get("action_by")
            return out
        note = "The stated need ends too close to the cutoff, so this reminder comes before the cutoff instead."
    if deadline is None:
        return {"id": trial_view["id"], "plan": "no_reminder",
                "why": "no charge or cutoff date known yet; ask for the email or the date"}
    if deadline <= now:
        return {"id": trial_view["id"], "plan": "passed", "why": "the action deadline has already passed"}
    target = deadline - timedelta(hours=lead_hours)
    local_target = target.astimezone(zone)
    at = local_target.replace(hour=hour, minute=0, second=0, microsecond=0)
    if at > local_target:
        at -= timedelta(days=1)
    if at > now + timedelta(minutes=10):
        out = {"id": trial_view["id"], "plan": "remind", "kind": "before_cutoff", "at": fmt(at, zone),
               "at_iso": at.isoformat(), "lead_hours": round((deadline - at).total_seconds() / 3600, 1),
               "action_by": trial_view.get("action_by")}
        if note:
            out["note"] = note
        return out
    out = {"id": trial_view["id"], "plan": "act_now",
           "hours_left": round((deadline - now).total_seconds() / 3600, 1),
           "why": f"less than {lead_hours:g} hours of notice is left", "action_by": trial_view.get("action_by")}
    fallback = max(now + timedelta(minutes=30), deadline - timedelta(hours=24))
    if fallback < deadline - timedelta(hours=1):
        out["fallback_at"] = fmt(fallback, zone)
        out["fallback_iso"] = fallback.astimezone(zone).isoformat()
    return out


# ---------- commands ----------

def read_items(path: str) -> list:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise Problem(f"Can't read evidence file {path}: {exc}")
    if isinstance(data, dict):
        data = data.get("evidence", [data])
    if not isinstance(data, list) or not data:
        raise Problem("The evidence file must hold a list of messages (or {'evidence': [...]}).")
    return data


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def timed(sp):
        sp.add_argument("--now", required=True, help="student's local time with offset, e.g. 2026-10-24T15:30-07:00")
        sp.add_argument("--tz", required=True, help="student's IANA time zone, e.g. America/Phoenix")

    a = sub.add_parser("add", help="merge evidence from a JSON file and print the updated list")
    a.add_argument("--file", required=True, help="evidence JSON")
    timed(a)
    l = sub.add_parser("list", help="print saved trials, most urgent first")
    l.add_argument("--all", action="store_true", help="include charged and kept trials")
    timed(l)
    pl = sub.add_parser("plan", help="propose a reminder time for one trial")
    pl.add_argument("--id", required=True)
    pl.add_argument("--lead-hours", type=float, default=48)
    pl.add_argument("--hour", type=int, default=18, help="preferred local hour, 0-23")
    timed(pl)
    r = sub.add_parser("reminder", help="record a reminder Cubbie created, or clear one")
    r.add_argument("action", choices=["set", "clear"])
    r.add_argument("--id", required=True)
    r.add_argument("--at", help="reminder time with offset (set)")
    r.add_argument("--ref", help="what Cubbie returned for the reminder, if anything")
    timed(r)
    m = sub.add_parser("mark", help="record what the student said")
    m.add_argument("--id", required=True)
    m.add_argument("--as", dest="as_", required=True, choices=sorted(USER_MARKS))
    timed(m)
    purpose = sub.add_parser("purpose", help="save the student's stated reason and optional need-until date")
    who = purpose.add_mutually_exclusive_group(required=True)
    who.add_argument("--id", help="a saved record")
    who.add_argument("--service", help="service name; creates a record if none is saved yet")
    purpose.add_argument("--text", required=True)
    purpose.add_argument("--until", help="student-stated date or datetime; omit if not known")
    timed(purpose)
    f = sub.add_parser("forget", help="delete one saved trial")
    f.add_argument("--id", required=True)

    args = p.parse_args(argv)
    warnings: list = []
    path = state_path()
    try:
        now = parse_now(args.now) if hasattr(args, "now") else None
        zone = get_zone(args.tz, now.utcoffset(), warnings) if now else None
        state = load_state(path)
        out: dict = {}

        if args.cmd == "add":
            items = read_items(args.file)
            changes, w = ingest(state, items, now, zone)
            warnings += w
            views = summary(state, zone, now, True)
            save_state(path, state)
            out = {"changes": changes or ["nothing new"], "trials": views}
        elif args.cmd == "list":
            out = {"trials": summary(state, zone, now, not args.all)}
        elif args.cmd == "plan":
            views = {v["id"]: v for v in summary(state, zone, now, False)}
            if args.id not in views:
                raise Problem(f"No saved trial '{args.id}'.", 3)
            if not math.isfinite(args.lead_hours) or args.lead_hours <= 0:
                raise Problem("--lead-hours must be finite and positive.")
            if not 0 <= args.hour <= 23:
                raise Problem("--hour must be 0-23.")
            out = plan(views[args.id], zone, now, args.lead_hours, args.hour)
        elif args.cmd == "reminder":
            trial = state["trials"].get(args.id)
            if trial is None:
                raise Problem(f"No saved trial '{args.id}'.", 3)
            if args.action == "set":
                if not args.at:
                    raise Problem("reminder set needs --at.")
                at, _ = parse_when(args.at, zone, warnings, "--at")
                view = derive(trial, zone, now)
                if at is None or at <= now:
                    raise Problem("Reminder time must be in the future.")
                reminders = trial.setdefault("reminders", [])
                reminders[:] = [r for r in reminders if not
                               ((args.ref and r.get("ref") == args.ref) or
                                (not args.ref and r.get("at") == at.isoformat()))]
                reminders.append({
                    "at": at.isoformat(), "ref": args.ref, "created_at": now.isoformat(),
                    "for_deadline": view.get("reminder_anchor_iso")})
            else:
                before = trial.get("reminders", [])
                trial["reminders"] = [r for r in before if args.ref and r.get("ref") != args.ref]
            save_state(path, state)
            v = derive(trial, zone, now)
            v["id"] = args.id
            out = {"trial": v}
        elif args.cmd == "mark":
            trial = state["trials"].get(args.id)
            if trial is None:
                raise Problem(f"No saved trial '{args.id}'.", 3)
            trial["user_mark"] = {"as": args.as_, "at": now.isoformat()}
            save_state(path, state)
            v = derive(trial, zone, now)
            v["id"] = args.id
            out = {"trial": v}
        elif args.cmd == "purpose":
            if not args.text.strip():
                raise Problem("Purpose text cannot be empty.")
            parse_when(args.until, zone, warnings, "--until")
            tid = args.id
            if tid is None:
                name = args.service.strip()
                if not name:
                    raise Problem("--service cannot be empty.")
                tid, match = find_trial(state, {"service": name})
                if match == "ambiguous":
                    raise Problem(f"More than one saved {name} record; run list and use --id.")
                if tid is None:
                    tid = new_id(state, name, None)
                    state["trials"][tid] = {"service": name, "account": None, "evidence": []}
            trial = state["trials"].get(tid)
            if trial is None:
                raise Problem(f"No saved trial '{tid}'.", 3)
            trial["purpose"] = {"text": args.text.strip()[:240], "until": args.until, "at": now.isoformat()}
            save_state(path, state)
            v = derive(trial, zone, now)
            v["id"] = tid
            out = {"trial": v}
        elif args.cmd == "forget":
            if args.id not in state["trials"]:
                raise Problem(f"No saved trial '{args.id}'.", 3)
            state["trials"].pop(args.id)
            save_state(path, state)
            out = {"forgotten": [args.id]}

        if warnings:
            out["warnings"] = warnings
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0
    except Problem as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
