"""Known DM channels, resolved by recipient without sending a message.

Bots cannot enumerate DMs through GET /users/@me/channels. POST resolves an
existing channel or creates an empty one; recipients come from the admission
policy, and all other DMs can arrive only through the Gateway.
"""

from dataclasses import dataclass, field


@dataclass
class DirectChannels:
    names: dict = field(default_factory=dict)
    users: dict = field(default_factory=dict)

    def resolve(self, user_ids, post):
        """Return [(channel_id, recipient_username)] for known users' DM channels."""
        out = []
        for uid in user_ids:
            try:
                c = post("/users/@me/channels", {"recipient_id": uid})
            except Exception as e:
                print(f"DM channel resolve failed for {uid}: {e}", flush=True)
                continue
            if c and c.get("id"):
                recips = c.get("recipients") or []
                name = recips[0].get("username", uid) if recips else uid
                out.append((c["id"], name))
                self.names[c["id"]] = name
                self.users[c["id"]] = str(uid)
        return out
