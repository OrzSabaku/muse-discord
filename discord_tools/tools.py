"""On-demand operations shared by the CLI and future MCP transport."""
from urllib.parse import urlencode

from .messages import format_message, split_message
from .state import read_state


class PartialSendError(Exception):
    def __init__(self, message_ids):
        super().__init__('Some parts were sent. Do not resend those parts.')
        self.message_ids = message_ids


class MessageTools:
    def __init__(self, api, guard, scrub, bot_id, state_path, mentions):
        self.api, self.guard, self.scrub = api, guard, scrub
        self.bot_id, self.state_path, self.mentions = bot_id, state_path, mentions

    @staticmethod
    def limit(value):
        value = int(value)
        if not 1 <= value <= 100:
            raise ValueError('Limit must be between 1 and 100.')
        return value

    def messages(self, channel, limit=50, before=None):
        limit = self.limit(limit)
        self.guard(channel)
        query = {'limit': limit}
        if before:
            query['before'] = before
        batch = self.api('GET', f'/channels/{channel}/messages?{urlencode(query)}')
        return {'channel_id': channel, 'untrusted': True,
                'messages': [format_message(m) for m in sorted(batch, key=lambda m: int(m['id']))]}

    def unread(self, channel, limit=50):
        limit = self.limit(limit)
        self.guard(channel)
        own_id = self.bot_id()
        with read_state(self.state_path) as state:
            watermark = state.get(channel)
            if watermark is None:
                latest = self.api('GET', f'/channels/{channel}/messages?limit=1')
                # Zero means the channel was empty at initialization.
                watermark = state.advance(channel, latest[0]['id'] if latest else '0')
                return {'channel_id': channel, 'initialized': True,
                        'last_read_message_id': watermark, 'messages': [],
                        'truncated': False, 'untrusted': True}
        collected, cursor, capped = [], watermark, False
        for _ in range(10):
            query = urlencode({'after': cursor, 'limit': 100})
            batch = self.api('GET', f'/channels/{channel}/messages?{query}')
            batch = sorted((m for m in batch if int(m['id']) > int(cursor)),
                           key=lambda m: int(m['id']))
            if not batch:
                break
            collected.extend(m for m in batch if (m.get('author') or {}).get('id') != own_id)
            cursor = batch[-1]['id']
            if len(collected) > limit or len(batch) < 100:
                break
        else:
            capped = True
        return {'channel_id': channel, 'initialized': False, 'untrusted': True,
                'last_read_message_id': watermark,
                'messages': [format_message(m) for m in collected[:limit]],
                'truncated': capped or len(collected) > limit}

    def mark_read(self, channel, message=None):
        self.guard(channel)
        if message:
            # A typo or a future id must not silently hide unread messages.
            self.api('GET', f'/channels/{channel}/messages/{message}')
        else:
            latest = self.api('GET', f'/channels/{channel}/messages?limit=1')
            message = latest[0]['id'] if latest else '0'
        with read_state(self.state_path) as state:
            watermark = state.advance(channel, message)
        return {'channel_id': channel, 'last_read_message_id': watermark}

    def send(self, channel, text, reply_to=None):
        self.scrub(text)
        chunks = split_message(text)
        self.guard(channel)
        if reply_to:
            self.api('GET', f'/channels/{channel}/messages/{reply_to}')
        sent = []
        for i, chunk in enumerate(chunks):
            payload = {'content': chunk,
                       'allowed_mentions': {**self.mentions, 'replied_user': False}}
            if i == 0 and reply_to:
                payload['message_reference'] = {'message_id': reply_to,
                                                'fail_if_not_exists': True}
            try:
                result = self.api('POST', f'/channels/{channel}/messages', payload)
            except (SystemExit, OSError) as error:
                if sent:
                    raise PartialSendError(sent) from error
                raise
            sent.append(result['id'])
        return {'channel_id': channel, 'message_ids': sent}
