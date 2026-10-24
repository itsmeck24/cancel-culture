#!/usr/bin/env python3
"""Cancel Culture trial ledger: merge trial evidence, work out deadlines, track reminders.

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
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path

KINDS = {"trial_started", "dates_changed", "price_changed", "renewal_notice", "cancelled", "charged", "other"}
ACTIVE_KINDS = {"trial_started", "dates_changed", "price_changed", "renewal_notice"}
DATE_KINDS = {"trial_started", "dates_changed", "renewal_notice"}
DATE_FIELDS = ("trial_end", "charge_at", "cancel_by")
PRICE_FIELDS = ("amount", "currency", "interval", "price_note")
OTHER_FIELDS = ("biller", "biller_name", "immediate_access_loss", "access_ends_at")
BILLERS = {"direct", "apple", "google_play", "paypal", "other", "unknown"}
USER_MARKS = {"cancel_requested", "cancellation_reported", "kept", "active"}


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


def parse_when(value, zone: tzinfo, notes: list, label: str):
    """Return (aware datetime, precision) or (None, None). Date-only values become the start of that
    day in the student's zone (the conservative reading) with precision 'date'."""
    if value in (None, "", "unknown"):
        return None, None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        d = date.fromisoformat(text)
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

def clean_item(item: dict, now: datetime) -> tuple[dict, list]:
    warnings = []
    if not isinstance(item, dict) or not str(item.get("service") or "").strip():
        raise Problem("Every evidence item needs a 'service' name.")
    out = {k: item.get(k) for k in (
        "source", "sender", "subject", "sent_at", "service", "account", "kind", "biller", "biller_name",
        "trial_end", "charge_at", "cancel_by", "access_ends_at", "amount", "currency", "interval",
        "price_note", "immediate_access_loss", "quote", "tz")}
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
    if out.get("amount") not in (None, ""):
        try:
            out["amount"] = float(out["amount"])
        except (TypeError, ValueError):
            raise Problem(f"amount for {out['service']} must be a number or null (put text like 'from $9' in price_note).")
    else:
        out["amount"] = None
    if not out.get("sent_at"):
        out["sent_at"] = now.isoformat()
        warnings.append(f"No sent_at for a {out['service']} message; used the current time for ordering.")
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


def ingest(state: dict, items: list, now: datetime) -> tuple[list, list]:
    changes, warnings = [], []
    for raw in items:
        item, w = clean_item(raw, now)
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
        trial["evidence"].sort(key=lambda e: e["sent_at"])
        if not changes or not changes[-1].startswith(f"{tid}: new"):
            changes.append(f"{tid}: added {item['kind']} message ({item['source']})")
    return changes, warnings


# ---------- derived view ----------

def derive(trial: dict, zone: tzinfo, now: datetime) -> dict:
    notes: list = []
    ev = sorted(trial["evidence"], key=lambda e: e["sent_at"])
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

    biller = latest["biller"][0] or "unknown"
    deadline, precision, basis = None, None, None
    if parsed["cancel_by"][0]:
        deadline, precision = parsed["cancel_by"]
        basis = "cancel-by date stated in the message"
    else:
        candidates = [parsed[f] for f in ("trial_end", "charge_at") if parsed[f][0]]
        if candidates:
            deadline, precision = min(candidates, key=lambda p: p[0])
            if biller == "apple":
                deadline = deadline - timedelta(hours=24)
                basis = "Apple says to cancel a trial at least 24 hours before it ends"
            else:
                basis = "no cancel-by stated; using the trial end or charge date, so aim earlier"
    if precision == "date":
        notes.append("Only a date was given, so the deadline is treated as the start of that day.")

    status_ev = next((e for e in reversed(ev) if e["kind"] in ACTIVE_KINDS | {"cancelled", "charged"}), None)
    status = "needs_info"
    if status_ev:
        status = {"cancelled": "cancellation_evidenced", "charged": "charged"}.get(status_ev["kind"], "active")
    mark = trial.get("user_mark")
    if mark and status_ev and mark["at"] >= status_ev["sent_at"] and status == "active":
        status = {"active": "active"}.get(mark["as"], mark["as"])
    elif mark and not status_ev:
        status = mark["as"]
    if trial.get("needs_review"):
        status = "needs_review"
    if status == "active" and deadline is None:
        status = "needs_info"
    if status == "active" and deadline is not None and deadline <= now:
        status = "deadline_passed"

    hours_left = round((deadline - now).total_seconds() / 3600, 1) if deadline else None
    needs_action = status in ("active", "cancel_requested")

    rem = trial.get("reminders", [])
    reminder = "none"
    if rem:
        current = deadline.isoformat() if deadline else None
        if not needs_action:
            reminder = "remove (no longer needed)"
        elif any(r.get("for_deadline") != current for r in rem):
            reminder = "move (deadline changed since it was set)"
        else:
            reminder = "set"

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
        "needs_action": needs_action,
        "cancel_by": fmt(deadline, zone, precision),
        "cancel_by_iso": deadline.isoformat() if deadline else None,
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


ORDER = {"active": 0, "cancel_requested": 0, "needs_info": 1, "needs_review": 1, "deadline_passed": 2,
         "cancellation_reported": 3, "kept": 4, "cancellation_evidenced": 5, "charged": 6}


def summary(state: dict, zone: tzinfo, now: datetime, only_open: bool) -> list:
    views = []
    for tid, trial in state["trials"].items():
        v = derive(trial, zone, now)
        v["id"] = tid
        views.append(v)
    views.sort(key=lambda v: (ORDER.get(v["status"], 9), v.get("cancel_by_iso") or "9999"))
    if only_open:
        views = [v for v in views if v["status"] not in ("charged", "kept") or v.get("reminder", "none") != "none"]
    return views


# ---------- reminders ----------

def plan(trial_view: dict, zone: tzinfo, now: datetime, lead_hours: float, hour: int) -> dict:
    if not trial_view.get("needs_action"):
        return {"id": trial_view["id"], "plan": "no_reminder", "why": f"status is {trial_view['status']}"}
    deadline = datetime.fromisoformat(trial_view["cancel_by_iso"])
    if deadline <= now:
        return {"id": trial_view["id"], "plan": "passed", "why": "the cancel-by time has already passed"}
    target = deadline - timedelta(hours=lead_hours)
    local_target = target.astimezone(zone)
    at = local_target.replace(hour=hour, minute=0, second=0, microsecond=0)
    if at > local_target:
        at -= timedelta(days=1)
    if at > now + timedelta(minutes=10):
        return {"id": trial_view["id"], "plan": "remind", "at": fmt(at, zone), "at_iso": at.isoformat(),
                "lead_hours": round((deadline - at).total_seconds() / 3600, 1), "cancel_by": trial_view["cancel_by"]}
    out = {"id": trial_view["id"], "plan": "act_now", "hours_left": trial_view["hours_left"],
           "why": f"less than {lead_hours:g} hours of notice is left", "cancel_by": trial_view["cancel_by"]}
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
            changes, w = ingest(state, items, now)
            warnings += w
            save_state(path, state)
            out = {"changes": changes or ["nothing new"], "trials": summary(state, zone, now, True)}
        elif args.cmd == "list":
            out = {"trials": summary(state, zone, now, not args.all)}
        elif args.cmd == "plan":
            views = {v["id"]: v for v in summary(state, zone, now, False)}
            if args.id not in views:
                raise Problem(f"No saved trial '{args.id}'.", 3)
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
                trial.setdefault("reminders", []).append({
                    "at": at.isoformat(), "ref": args.ref, "created_at": now.isoformat(),
                    "for_deadline": view.get("cancel_by_iso")})
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
