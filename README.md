# Cancel Culture

Catch the charge. Find the right exit. Confirm you're out.

A [Cubbie](https://cubbie.ai) Skill for the Cubbie Hackathon, Track 1.

Built at DevHacks S3 by Team Southside: Charith Kutikuppala and Richitha Rekula.

Your semester ends. Your subscriptions don't.

Cancel Culture helps you act before a free trial charges, a paid subscription renews or a student
discount expires. Keep what you use, renew student pricing when eligible, and exit what you no longer
need. If you tell Cubbie "I only need this until my presentation", it remembers that reason and asks
before the next relevant deadline. No usage tracking and no automatic cancellation.

## Install

Text this link to Cubbie and reply "yes" when it asks:

```text
https://github.com/itsmeck24/cancel-culture
```

No keys and no extra sign-in. Connecting Google is optional, and only needed for "check my email for trials".

## Use it

- Forward, paste or screenshot a trial email: "just started this trial"
- "check my email for upcoming renewals or expiring student discounts"
- "I only need this editor until my presentation on November 12"
- "help me renew my student discount"
- "am I about to get charged for anything?"
- "remind me before it charges" / "how do I cancel it?" / "done"

## What it does

- Distinguishes trial conversions, paid renewals and student-discount expiry. Reverification is offered
  when a discount is ending; it never assumes eligibility or promises a student price.
- Saves an optional reason and need-until date supplied by you. It asks whether the subscription still
  serves that purpose, without claiming to know your usage.
- Shows the next documented price and current student rate separately. Missing prices stay unknown;
  an old receipt does not establish a future renewal.

- Separates the trial end, the charge date and the actual cancel-by cutoff. Some services need you to cancel
  a day early. The Apple trial rule is applied only to trial events, not ordinary paid renewals.
- Lets a later email win: an extension moves the deadline, and a cancellation email stops the reminder.
- Reminds you at least 48 hours before the cutoff, at a time you pick. Scanning the same email twice never
  adds no duplicate evidence; existing reminders are checked before scheduling another.
- Sends you to whoever actually bills you (Apple, Google Play or the service itself), using official pages
  only.
- Never cancels, signs in or pays for you. When you say "done", it records that you cancelled. It only calls
  the cancellation confirmed once the confirmation email shows up.
- Keeps your subscription list on your own Cubbie drive. Nothing is sent anywhere else.

## What's inside

```
SKILL.md                      what Cubbie follows
scripts/trials.py             subscription evidence, purpose preferences and date math (Python 3, no packages)
references/billing-routes.md  official cancel and student-verification routes
```

Local script tests cover evidence and date handling. Gmail access, reminder delivery and account handoff
still require testing on Cubbie; local tests do not establish those integrations.
