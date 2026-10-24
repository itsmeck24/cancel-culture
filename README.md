# Cancel Culture

Catch the charge. Find the right exit. Confirm you're out.

A [Cubbie](https://cubbie.ai) Skill for the Cubbie Hackathon, Track 1.

Built at DevHacks S3 by Team Southside: Charith Kutikuppala and Richitha Rekula.

You sign up for a free trial to get one thing done: a video editor for a class project, a study app
for exam week, a streaming service for one show. Two weeks later the charge shows up and you don't
remember signing up. Cancel Culture turns the trial email into a real cancel-by deadline,
reminds you early enough to act, and tells you exactly where to cancel.

## Install

Text this link to Cubbie and reply "yes" when it asks:

```text
https://github.com/itsmeck24/cancel-culture
```

No keys and no extra sign-in. Connecting Google is optional, and only needed for "check my email for trials".

## Use it

- Forward, paste or screenshot a trial email: "just started this trial"
- "check my email for free trials"
- "am I about to get charged for anything?"
- "remind me before it charges" / "how do I cancel it?" / "done"

## What it does

- Separates the trial end, the charge date and the actual cancel-by cutoff. Some services need you to cancel
  a day early. Apple says to cancel at least 24 hours before a trial ends.
- Lets a later email win: an extension moves the deadline, and a cancellation email stops the reminder.
- Reminds you at least 48 hours before the cutoff, at a time you pick. Scanning the same email twice never
  creates a second reminder.
- Sends you to whoever actually bills you (Apple, Google Play or the service itself), using official pages
  only.
- Never cancels, signs in or pays for you. When you say "done", it records that you cancelled. It only calls
  the cancellation confirmed once the confirmation email shows up.
- Keeps your trial list on your own Cubbie drive. Nothing is sent anywhere else.

## What's inside

```
SKILL.md                      what Cubbie follows
scripts/trials.py             saved trial list, merging and date math (Python 3, no packages)
references/billing-routes.md  official cancel routes
```
