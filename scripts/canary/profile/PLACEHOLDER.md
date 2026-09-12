Drop your canary test voice here (kept separate from real channel profiles
on purpose, so a canary failure never touches production data):

- `reference.wav` -- any short (8-12s), clean, single-take clip
- `reference.json` -- `{"transcript": "...exact words spoken..."}`

Until both files exist, canary.yml's "Report result" step sends a
"skipped, no fixture yet" notice instead of failing -- so the empty
scheduled workflow won't spam you with false alarms before setup is done.
