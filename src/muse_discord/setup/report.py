"""Present setup results as sentences or one JSON object."""

import json

OWNER_STEPS = {
    "intent": (
        "Turn on Message Content: in the Developer Portal, open "
        "your app and click **Bot** in the left menu. Scroll down "
        "to **Privileged Gateway Intents**, switch on **Message "
        "Content Intent**, then click **Save Changes** in the bar "
        "that appears at the bottom."
    ),
    "code_grant": (
        "On the **Bot** page, switch off **Requires OAuth2 "
        "Code Grant**, then click **Save Changes** at the "
        "bottom. (With it on, the invite link fails.)"
    ),
    "interactions_url": (
        "On the **General Information** page, empty the "
        "**Interactions Endpoint URL** box, then click "
        "**Save Changes** at the bottom. (With it set, "
        "the bot's buttons and /settings go elsewhere.)"
    ),
}


class Report:
    def __init__(self, as_json):
        self.as_json = as_json
        self.data = {"ok": True, "lines": [], "owner_steps": []}

    def line(self, text):
        self.data["lines"].append(text)
        if not self.as_json:
            print(text, flush=True)

    def step(self, kind):
        self.data["owner_steps"].append({"kind": kind, "text": OWNER_STEPS[kind]})
        if not self.as_json:
            print(f"OWNER STEP: {OWNER_STEPS[kind]}", flush=True)

    def fail(self, kind, text, code):
        self.data.update(ok=False, problem=kind)
        self.line(f"PROBLEM: {text}")
        self.finish()
        return code

    def finish(self):
        if self.as_json:
            print(json.dumps(self.data, indent=1))
