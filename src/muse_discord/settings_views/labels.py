"""Settings categories, titles and diagnostic wording."""

from muse_discord import muse_settings as ms

CATEGORIES = {
    "reply": (
        "返信・話し方",
        (
            "who.replies",
            "style.length",
            "style.notes",
            "limits.per_day",
            "limits.per_person_per_day",
        ),
    ),
    "people": (
        "利用者・権限",
        (
            "who.dms_from",
            "who.allow",
            "who.ignore",
            "who.bots",
            "who.bot_exchange_limit",
            "abilities.reminders",
            "abilities.owner_commands",
            "abilities.external_apps",
        ),
    ),
    "proactive": (
        "自発的な発言",
        tuple(k for k in ms.CORE if k.startswith("proactive.")),
    ),
    "media": ("画像・動画", tuple(k for k in ms.CORE if k.startswith("media."))),
    "privacy": (
        "プライバシー",
        ("privacy.memory", "privacy.suspicious", "memory.save_dms"),
    ),
    "other": (
        "その他",
        ("presence.status", "presence.kind", "updates.notify", "telemetry.usage_stats"),
    ),
}


TITLES = {
    "who.replies": "返信するタイミング",
    "style.length": "返信の長さ",
    "style.notes": "話し方の指示",
    "limits.per_day": "1日の返信上限",
    "limits.per_person_per_day": "1人あたりの1日の返信上限",
    "who.dms_from": "DMを許可する相手",
    "who.allow": "返信を許可する相手",
    "who.ignore": "無視する相手",
    "who.bots": "他のBotへの返信",
    "who.bot_exchange_limit": "Botとの連続返信上限",
    "abilities.reminders": "リマインダー・監視を使える相手",
    "abilities.owner_commands": "所有者からの操作依頼",
    "abilities.external_apps": "外部アプリへのアクセス",
    "privacy.memory": "会話で使える個人情報",
    "privacy.suspicious": "攻撃が疑われるメッセージ",
    "memory.save_dms": "DMの内容を記憶に保存",
    "media.enabled": "画像・動画の生成",
    "media.who": "生成を依頼できる相手",
    "media.per_day": "1日の生成上限",
    "proactive.enabled": "自発的な発言を許可",
    "proactive.frequency": "自発的に話しかける頻度",
    "proactive.per_person_per_day": "1人あたりの1日上限",
    "proactive.per_person_per_week": "1人あたりの1週間上限",
    "proactive.per_channel_per_day": "1チャンネルあたりの1日上限",
    "proactive.quiet_hours": "自発的な発言を控える時間帯",
    "proactive.timezone": "タイムゾーン",
    "presence.status": "ステータスの文章",
    "presence.kind": "ステータスの種類",
    "updates.notify": "更新のお知らせ",
    "telemetry.usage_stats": "匿名の利用統計",
}


WORDS = {
    "off": "無効",
    "when-addressed": "呼びかけられたとき",
    "conversational": "呼びかけと会話の続き",
    "freely": "呼びかけなしで会話に参加・自発的に発言",
    "brief": "短め",
    "normal": "標準",
    "detailed": "詳しく",
    "owners": "所有者のみ",
    "everyone": "全員",
    "anyone": "誰でも",
    "shared-servers": "共通サーバーの参加者",
    "ignore": "無視する",
    "hold-for-review": "所有者の確認を待つ",
    "everything": "記憶している情報すべて",
    "public-only": "公開されている情報のみ",
    "nothing": "個人情報を使わない",
    "rarely": "少なめ（1日最大2件）",
    "sometimes": "ときどき（1日最大5件）",
    "often": "多め（1日最大10件）",
    "read-only": "読み取りのみ",
    "confirm": "書き込みは確認する",
    "write": "指定範囲の書き込みを許可",
    "unconditional": "登録済み操作を確認なしで許可",
}


REASONS = {
    "replies-off": "この場所では返信が無効です。",
    "dm-off": "DMへの返信が無効です。",
    "bots-ignore": "他のBotには返信しない設定です。",
    "not-allowed": "許可リスト・無視リストの設定で返信しませんでした。",
    "dm-not-allowed": "この相手からのDMは許可されていません。",
    "cap": "返信の日次上限に達しました。",
    "quarantined": "所有者の確認待ちです。DMを確認してください。",
    "suspicious-ignored": "攻撃の可能性があるため無視しました。",
    "no-send": "Discordのチャンネル権限でメッセージを送信できません。",
    "internal": "内部エラーです。monitor.logを確認してください。",
    "worker-skipped": "メッセージを読み、返信しないと判断しました。",
    "send-failed": "送信に失敗しました。monitor.logを確認してください。",
    "never-share": "共有禁止の文章を含むため返信を保留しました。",
    "predates-join": "Botが参加する前のメッセージです。",
    "ownerless": "サーバー内に所有者がおらず、返信が有効化されていません。",
    "bot-exchange": "Botとの連続返信上限に達しました。",
    "watch-cap": "リマインダー・監視の上限に達しました。",
    "watch-held": "リマインダーの配信を保留しています。",
    "watch-not-allowed": "リマインダー・監視の利用が許可されていません。",
}
