"""Record terminal reception refusals and suppress repeated channel warnings.

A deliberate refusal is ledgered before settings can change; gap repair must not
later answer it. Ordinary chatter never reaches these records. Warning state is
owned by one running reception service, while the ledger remains durable.
"""

from dataclasses import dataclass, field

from muse_discord.conversation import _who
from muse_discord.message_ledger import MessageLedger
from muse_discord.message_models import LedgerContext


@dataclass
class ReceptionAudit:
    ledger: MessageLedger
    no_send_logged: set = field(default_factory=set)

    def refused(self, msg, cname, reason):
        """A message that addressed the bot directly but that the settings
        decline: ledgered as skipped, so the reconciliation sweep (which
        judges by the settings in force when it runs) never answers it
        later, after a setting is relaxed. A deliberately ignored message
        stays ignored. Ordinary chatter is never ledgered."""
        author = _who(msg)
        self.ledger.record(
            msg["id"],
            "skipped",
            f"settings: {reason}",
            context=LedgerContext(
                channel_id=msg.get("channel_id"),
                author=author.get("username"),
                author_id=author.get("id"),
            ),
        )
        print(f"Not answering {author.get('username')} in {cname}: {reason}.", flush=True)

    def no_send(self, msg, cname):
        """Record a mention that cannot be answered because the bot has no
        send permission in the channel: ledgered as skipped (terminal, so the
        sweep won't resurrect it) and logged once per channel per run."""
        cid = msg.get("channel_id")
        if cid not in self.no_send_logged:
            self.no_send_logged.add(cid)
            print(f"Skipping replies in {cname}: the bot cannot send messages there.", flush=True)
        self.ledger.record(
            msg["id"],
            "skipped",
            "no send permission in channel",
            context=LedgerContext(
                channel_id=cid, author=_who(msg).get("username"), author_id=_who(msg).get("id")
            ),
        )
