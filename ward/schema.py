"""The settings schema: every app-wide option with its type, default, choices and where it appears in Preferences.

The Preferences dialog is generated from this table (one row per option), Settings validates against it, and the
test-suite checks that every option has a title.  Per-account options are in ACCOUNT_SPECS and live in the
database (accounts.options); per-chat overrides live in the chats table.  Strings are marked with N_() and
translated when shown."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .i18n import N_

MB = 1000 * 1000


@dataclass(frozen=True)
class Spec:
    type: str                         # bool | choice | int | str | list | font | time_range | json
    default: Any
    title: str = ""
    subtitle: str = ""
    page: str | None = None           # None: internal, never shown
    group: str = ""
    choices: tuple[tuple[Any, str], ...] = ()
    min: int | None = None
    max: int | None = None
    step: int = 1
    unit: str = ""
    advanced: bool = False            # shown inside an "Advanced" expander of its group
    keywords: tuple[str, ...] = field(default_factory=tuple)


def S(type_, default, title="", subtitle="", **kw) -> Spec:
    return Spec(type_, default, title, subtitle, **kw)


ACCENTS = (("system", N_("Follow system")), ("blue", N_("Blue")), ("teal", N_("Teal")), ("green", N_("Green")),
           ("yellow", N_("Yellow")), ("orange", N_("Orange")), ("red", N_("Red")), ("pink", N_("Pink")),
           ("purple", N_("Purple")), ("slate", N_("Slate")))

ACCENT_COLORS = {"blue": "#3584e4", "teal": "#2190a4", "green": "#3a944a", "yellow": "#c88800",
                 "orange": "#ed5b00", "red": "#e62d42", "pink": "#d56199", "purple": "#9141ac", "slate": "#6f8396"}

DEFAULT_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🎉"]

PAGES = (
    ("general", N_("General"), "preferences-system-symbolic"),
    ("appearance", N_("Appearance"), "applications-graphics-symbolic"),
    ("chats", N_("Chats"), "ward-chat-symbolic"),
    ("calls", N_("Calls"), "call-start-symbolic"),
    ("notifications", N_("Notifications"), "preferences-system-notifications-symbolic"),
    ("accounts", N_("Accounts"), "system-users-symbolic"),
    ("encryption", N_("Encryption"), "channel-secure-symbolic"),
    ("storage", N_("Storage"), "drive-harddisk-symbolic"),
    ("advanced", N_("Advanced"), "applications-engineering-symbolic"),
)

SPECS: dict[str, Spec] = {
    # ------------------------------------------------------------------ General
    "general.background": S("bool", True, N_("Run in the background"),
                            N_("Stay connected when the window is closed, so messages and notifications still arrive"),
                            page="general", group=N_("Running")),
    "general.autostart": S("bool", False, N_("Start at login"), N_("Start hidden, in the background"),
                           page="general", group=N_("Running")),
    "general.tray": S("choice", "auto", N_("Tray icon"),
                      N_("Automatic shows it when the desktop has a tray: KDE, Cinnamon, XFCE, or GNOME with the "
                         "AppIndicator extension"),
                      page="general", group=N_("Running"),
                      choices=(("auto", N_("Automatic")), ("always", N_("Always")), ("never", N_("Never")))),
    "general.confirm_quit": S("bool", False, N_("Confirm before quitting"), page="general", group=N_("Running")),
    "general.uri_handler": S("bool", True, N_("Open xmpp: links with Ward"),
                             N_("Links to contacts and groups in your browser open the right chat"),
                             page="general", group=N_("Links")),
    "general.search_provider": S("bool", False, N_("Show contacts in desktop search"),
                                 N_("Lets GNOME Shell search find your contacts and chats by name"),
                                 page="general", group=N_("Links")),
    # ------------------------------------------------------------------ Appearance
    "appearance.style": S("choice", "system", N_("Style"), page="appearance", group=N_("Colours"),
                          choices=(("system", N_("Follow system")), ("light", N_("Light")), ("dark", N_("Dark")))),
    "appearance.accent": S("choice", "system", N_("Accent colour"), page="appearance", group=N_("Colours"),
                           choices=ACCENTS),
    "appearance.nick_colors": S("bool", True, N_("Coloured nicknames"),
                                N_("The same colour for a person as in Conversations and Dino"),
                                page="appearance", group=N_("Colours")),
    "appearance.code_scheme": S("str", "auto", N_("Code block colour scheme"),
                                N_("“auto” uses a matching light and dark pair"),
                                page="appearance", group=N_("Colours"), advanced=True),
    "appearance.text_scale": S("int", 100, N_("Text size"), N_("Also Ctrl+Plus and Ctrl+Minus"),
                               page="appearance", group=N_("Text"), min=80, max=200, step=10, unit="%"),
    "appearance.message_font": S("font", "", N_("Message font"), page="appearance", group=N_("Text")),
    "appearance.code_font": S("font", "", N_("Code font"), page="appearance", group=N_("Text")),
    "appearance.line_spacing": S("choice", "normal", N_("Line spacing"), page="appearance", group=N_("Text"),
                                 choices=(("compact", N_("Compact")), ("normal", N_("Normal")),
                                          ("relaxed", N_("Relaxed")))),
    "appearance.large_emoji": S("bool", True, N_("Large emoji in emoji-only messages"),
                                page="appearance", group=N_("Text")),
    "appearance.layout": S("choice", "cozy", N_("Message layout"), page="appearance", group=N_("Messages"),
                           choices=(("bubbles", N_("Bubbles")), ("cozy", N_("Cozy: names and avatars")),
                                    ("compact", N_("Compact: like IRC")))),
    "appearance.chat_animations": S("bool", True, N_("Animate chats"),
                                    N_("Slide a chat up into the message area when you open it"),
                                    page="appearance", group=N_("Messages")),
    "appearance.message_animations": S("bool", True, N_("Animate messages"),
                                       N_("New messages slide up into the conversation"),
                                       page="appearance", group=N_("Messages")),
    "appearance.avatars": S("choice", "always", N_("Avatars in chats"), page="appearance", group=N_("Messages"),
                            choices=(("always", N_("Always")), ("groups", N_("Groups only")),
                                     ("never", N_("Never")))),
    "appearance.avatar_shape": S("choice", "circle", N_("Avatar shape"), page="appearance",
                                 group=N_("Messages"),
                                 choices=(("circle", N_("Circle")), ("rounded", N_("Rounded square")))),
    "appearance.timestamps": S("choice", "hover", N_("Timestamps"), page="appearance", group=N_("Messages"),
                               choices=(("always", N_("Always")), ("hover", N_("On hover")), ("off", N_("Off")))),
    "appearance.time_format": S("choice", "system", N_("Time format"), page="appearance", group=N_("Messages"),
                                choices=(("system", N_("Follow system")), ("12", N_("12-hour")),
                                         ("24", N_("24-hour")))),
    "appearance.time_seconds": S("bool", False, N_("Show seconds"), page="appearance", group=N_("Messages"),
                                 advanced=True),
    "appearance.list_density": S("choice", "comfortable", N_("Chat list density"), page="appearance",
                                 group=N_("Chat list"),
                                 choices=(("comfortable", N_("Comfortable")), ("compact", N_("Compact")))),
    "appearance.list_previews": S("bool", True, N_("Message previews in the chat list"), page="appearance",
                                  group=N_("Chat list")),
    # ------------------------------------------------------------------ Chats and messages
    "chats.send_with": S("choice", "enter", N_("Send with"),
                         N_("The other key combination adds a new line"), page="chats", group=N_("Writing"),
                         choices=(("enter", N_("Enter")), ("ctrl_enter", N_("Ctrl+Enter")))),
    "chats.toolbar": S("bool", False, N_("Formatting toolbar"), page="chats", group=N_("Writing")),
    "chats.shortcodes": S("bool", True, N_("Convert :shortcodes: to emoji"), page="chats", group=N_("Writing")),
    "chats.ascii_smileys": S("bool", False, N_("Convert smileys like :) to emoji"), page="chats",
                             group=N_("Writing")),
    "chats.spellcheck": S("bool", True, N_("Spell check"), page="chats", group=N_("Writing")),
    "chats.spell_languages": S("list", [], N_("Spell check languages"),
                               N_("Empty means the system language"), page="chats", group=N_("Writing"),
                               advanced=True),
    "chats.quick_reactions": S("list", DEFAULT_REACTIONS, N_("Quick reactions"),
                               N_("The six emoji in the reaction bar"), page="chats", group=N_("Messages")),
    "chats.recent_emoji": S("list", []),                  # the emoji picker's recently used, newest first
    "chats.emoji_skin_tone": S("int", 0, min=0, max=5),   # the emoji picker's skin tone, 0 the default
    "chats.markdown_others": S("bool", False, N_("Render Markdown from other apps"),
                               N_("Other apps use simpler styling, where *text* is bold instead of italic"),
                               page="chats", group=N_("Messages")),
    "chats.collapse_after": S("int", 25, N_("Collapse long messages after"),
                              N_("Lines; 0 never collapses"), page="chats", group=N_("Messages"), min=0, max=100,
                              step=5, unit=N_("lines")),
    "chats.animations": S("choice", "always", N_("Play animated images"), page="chats", group=N_("Messages"),
                          choices=(("always", N_("Always")), ("hover", N_("On hover")), ("never", N_("Never")))),
    "chats.link_previews": S("choice", "contacts", N_("Link previews"),
                             N_("Fetching a preview shows your IP address to the website"), page="chats",
                             group=N_("Messages"),
                             choices=(("off", N_("Off")), ("contacts", N_("Only from contacts")),
                                      ("always", N_("Always")))),
    "chats.confirm_delete": S("bool", True, N_("Confirm “Delete for everyone”"), page="chats",
                              group=N_("Messages")),
    "chats.send_typing": S("bool", True, N_("Send typing indicators"), page="chats", group=N_("Sharing")),
    "chats.show_typing": S("bool", True, N_("Show typing indicators"), page="chats", group=N_("Sharing")),
    "chats.send_markers": S("bool", True, N_("Send read markers"), page="chats", group=N_("Sharing")),
    "chats.send_receipts": S("bool", True, N_("Send delivery receipts"), page="chats", group=N_("Sharing")),
    "chats.join_leave": S("choice", "summary", N_("Join and leave lines in groups"), page="chats",
                          group=N_("Groups"),
                          choices=(("summary", N_("Summary")), ("all", N_("Every line")), ("hidden", N_("Hidden")))),
    "chats.voice_quality": S("choice", "standard", N_("Voice message quality"), page="chats",
                             group=N_("Media"), choices=(("standard", N_("Standard")), ("high", N_("High")))),
    # ------------------------------------------------------------------ Calls
    "calls.discover": S("bool", True, N_("Discover STUN/TURN from the server"),
                        N_("Use XEP-0215 external service discovery for call relay servers"),
                        page="calls", group=N_("Network")),
    "calls.stun": S("str", "", N_("STUN servers"),
                    N_("Space-separated stun:host:port addresses; the server can provide these too"),
                    page="calls", group=N_("Network")),
    "calls.turn": S("str", "", N_("TURN servers"),
                    N_("Space-separated turn:host:port addresses; add ?transport=tcp for TCP"),
                    page="calls", group=N_("Network")),
    "calls.turn_user": S("str", "", N_("TURN username"), page="calls", group=N_("Network")),
    "calls.turn_password": S("str", "", N_("TURN password"), page="calls", group=N_("Network")),
    "calls.turn_secret": S("str", "", N_("TURN shared secret"),
                           N_("For time-limited credentials, as used by coturn and eturnal"),
                           page="calls", group=N_("Network"), advanced=True),
    "calls.turn_ttl": S("int", 86400, N_("TURN credential lifetime"),
                        N_("Seconds; used with the shared secret"), page="calls", group=N_("Network"),
                        min=300, max=604800, step=3600, unit=N_("seconds"), advanced=True),
    "calls.relay_only": S("bool", False, N_("Always relay calls"),
                          N_("Use TURN even when a direct connection is possible"),
                          page="calls", group=N_("Network"), advanced=True),
    # ------------------------------------------------------------------ Notifications
    "notify.enabled": S("bool", True, N_("Notifications"), page="notifications", group=N_("Notifications")),
    "notify.content": S("choice", "full", N_("Show in notification"), page="notifications",
                        group=N_("Notifications"),
                        choices=(("full", N_("Sender and text")), ("sender", N_("Sender only")),
                                 ("nothing", N_("Nothing")))),
    "notify.hide_encrypted": S("bool", True, N_("Hide text for encrypted chats"), page="notifications",
                               group=N_("Notifications")),
    "notify.groups": S("choice", "mentions", N_("Group chats"), page="notifications", group=N_("Notifications"),
                       choices=(("all", N_("All messages")), ("mentions", N_("Mentions and replies")),
                                ("none", N_("None")))),
    "notify.highlight_words": S("list", [], N_("Highlight words"),
                                N_("Your nickname always counts; one word per line"), page="notifications",
                                group=N_("Notifications")),
    "notify.quiet_hours": S("time_range", "", N_("Quiet hours"), N_("No sounds or pop-ups in this daily range"),
                            page="notifications", group=N_("Notifications")),
    "notify.badge": S("bool", True, N_("Unread count on the dock icon"), page="notifications",
                      group=N_("Notifications")),
    "notify.sounds": S("bool", True, N_("Sounds"), page="notifications", group=N_("Sounds")),
    "notify.sound_message": S("sound", "message-new-instant", N_("New message"), page="notifications",
                              group=N_("Sounds"), advanced=True),
    "notify.sound_mention": S("sound", "message-new-instant", N_("Mention"), page="notifications",
                              group=N_("Sounds"), advanced=True),
    "notify.sound_request": S("sound", "dialog-information", N_("Contact request"), page="notifications",
                              group=N_("Sounds"), advanced=True),
    # ------------------------------------------------------------------ Encryption and privacy
    "encryption.default": S("choice", "always", N_("Default encryption"),
                            N_("Each account and chat can override this"), page="encryption",
                            group=N_("Encryption"),
                            choices=(("always", N_("Always (recommended)")), ("available", N_("When available")),
                                     ("off", N_("Off")))),
    "encryption.trust": S("choice", "btbv", N_("Trust new devices"), page="encryption", group=N_("Encryption"),
                          choices=(("btbv", N_("Blind trust before verification")),
                                   ("manual", N_("Ask every time")))),
    "encryption.new_device_notice": S("choice", "banner", N_("Tell me about new devices"), page="encryption",
                                      group=N_("Encryption"),
                                      choices=(("banner", N_("Chat banner")),
                                               ("notify", N_("Banner and notification")))),
    "encryption.inactive_days": S("choice", 90, N_("Mark devices inactive after"), page="encryption",
                                  group=N_("Encryption"),
                                  choices=((30, N_("30 days")), (90, N_("90 days")), (180, N_("180 days")),
                                           (0, N_("Never")))),
    "encryption.legacy_omemo": S("bool", True, N_("Allow legacy OMEMO (0.3)"),
                                 N_("Most other apps still use it"), page="encryption", group=N_("Encryption")),
    "encryption.limit_legacy_extras": S("bool", False, N_("Limit unencrypted extras with legacy OMEMO"),
                                        N_("Legacy OMEMO only encrypts the text: don’t send reactions or typing"),
                                        page="encryption", group=N_("Encryption"), advanced=True),
    "pgp.enabled": S("bool", False, N_("OpenPGP"),
                     N_("Optional end-to-end encryption with your GnuPG keys (XEP-0373), for chats with Gajim "
                        "OpenPGP users"), page="encryption", group=N_("OpenPGP")),
    "pgp.use_gpgman": S("bool", True, N_("Share GPGMan’s keyring"),
                        N_("Use the gpg program and key folder chosen in GPGMan, so both apps see the same keys"),
                        page="encryption", group=N_("OpenPGP")),
    "pgp.gpg_binary": S("str", "", N_("gpg program"), N_("Empty finds gpg automatically"), page="encryption",
                        group=N_("OpenPGP"), advanced=True),
    "pgp.gnupg_home": S("str", "", N_("GnuPG folder"), N_("Empty uses ~/.gnupg"), page="encryption",
                        group=N_("OpenPGP"), advanced=True),
    "privacy.strip_metadata": S("bool", True, N_("Strip image location and camera data"),
                                N_("Before sending a photo"), page="encryption", group=N_("Privacy")),
    "privacy.strangers": S("choice", "requests", N_("Messages from strangers"), page="encryption",
                           group=N_("Privacy"),
                           choices=(("requests", N_("Requests folder")), ("block", N_("Block")),
                                    ("allow", N_("Allow")))),
    "privacy.stranger_media": S("bool", False, N_("Load images and previews from strangers"),
                                page="encryption", group=N_("Privacy")),
    # ------------------------------------------------------------------ Storage
    "storage.cache_limit": S("choice", 1000 * MB, N_("Cache size limit"),
                             N_("Downloaded files, thumbnails, avatars and previews"), page="storage",
                             group=N_("Cache"),
                             choices=((250 * MB, "250 MB"), (1000 * MB, "1 GB"), (5000 * MB, "5 GB"),
                                      (0, N_("Unlimited")))),
    "storage.delete_files_after": S("choice", 0, N_("Delete downloaded files after"), page="storage",
                                    group=N_("Cache"),
                                    choices=((30, N_("30 days")), (90, N_("90 days")), (365, N_("365 days")),
                                             (0, N_("Never")))),
    # ------------------------------------------------------------------ Advanced
    "advanced.renderer": S("choice", "opengl", N_("Graphics renderer"),
                           N_("OpenGL uses much less memory than Vulkan on many systems. Applies after a restart"),
                           page="advanced", group=N_("Performance"),
                           choices=(("opengl", N_("OpenGL")), ("auto", N_("GTK's choice (often Vulkan)")),
                                    ("software", N_("Software: no graphics driver, half the memory")))),
    "advanced.free_window": S("bool", True, N_("Free the window's memory in the background"),
                              N_("Closing the window releases it instead of hiding it; reopening takes a moment"),
                              page="advanced", group=N_("Performance")),
    "advanced.devtools": S("bool", False, N_("Developer tools"),
                           N_("XML console, service discovery and PEP browser"), page="advanced",
                           group=N_("Developer")),
    "advanced.debug_log": S("choice", "off", N_("Debug log"), page="advanced", group=N_("Developer"),
                            choices=(("off", N_("Off")), ("normal", N_("Normal")), ("verbose", N_("Verbose")))),
    "advanced.custom_css": S("bool", False, N_("Load custom.css"),
                             N_("A style sheet in the config folder, loaded last"), page="advanced",
                             group=N_("Developer")),
    "advanced.keepalive": S("int", 60, N_("Keepalive ping interval"), page="advanced", group=N_("Connection"),
                            min=30, max=600, step=30, unit=N_("seconds")),
    "advanced.reconnect_cap": S("int", 5, N_("Reconnect delay cap"), page="advanced", group=N_("Connection"),
                                min=1, max=30, unit=N_("minutes")),
    "advanced.ip_preference": S("choice", "auto", N_("Internet protocol"), page="advanced",
                                group=N_("Connection"),
                                choices=(("auto", N_("Automatic")), ("ipv4", N_("Prefer IPv4")),
                                         ("ipv6", N_("Prefer IPv6")))),
    "advanced.spk_rotation_days": S("int", 7, N_("Signed prekey rotation"), page="advanced", group=N_("OMEMO"),
                                    min=7, max=30, unit=N_("days")),
    "advanced.prekey_threshold": S("int", 99, N_("Refill prekeys below"), page="advanced", group=N_("OMEMO"),
                                   min=25, max=99),
    # ------------------------------------------------------------------ internal state (never shown)
    "window.width": S("int", 1000),
    "window.height": S("int", 720),
    "window.maximized": S("bool", False),
    "ui.chat_filter": S("str", "all"),
    "ui.account_filter": S("str", ""),
    "ui.last_chat": S("str", ""),
    "ui.members_panel": S("bool", True),
}

# --------------------------------------------------------------------------------------- per-account options
PROXY_CHOICES = (("system", N_("System settings")), ("none", N_("None")), ("socks5", "SOCKS5"),
                 ("http", "HTTP CONNECT"), ("tor", N_("Tor (127.0.0.1:9050)")))

ACCOUNT_SPECS: dict[str, Spec] = {
    "enabled": S("bool", True, N_("Enabled"), group=N_("Connection")),
    "autoconnect": S("bool", True, N_("Connect on startup"), group=N_("Connection")),
    "host": S("str", "", N_("Server address"), N_("Empty looks the server up automatically"),
              group=N_("Connection"), advanced=True),
    "port": S("int", 0, N_("Port"), N_("0 for the default"), group=N_("Connection"), min=0, max=65535,
              advanced=True),
    "tls": S("choice", "auto", N_("Encryption of the connection"), group=N_("Connection"), advanced=True,
             choices=(("auto", N_("Automatic")), ("starttls", "STARTTLS"), ("direct", N_("Direct TLS")))),
    "resource": S("str", "", N_("Resource"), N_("The name of this device on the server"),
                  group=N_("Connection"), advanced=True),
    "priority": S("int", 0, N_("Priority"), group=N_("Connection"), min=-128, max=127, advanced=True),
    "proxy": S("choice", "system", N_("Proxy"), group=N_("Connection"), choices=PROXY_CHOICES),
    "proxy_host": S("str", "", N_("Proxy host"), group=N_("Connection")),
    "proxy_port": S("int", 0, N_("Proxy port"), group=N_("Connection"), min=0, max=65535),
    "proxy_user": S("str", "", N_("Proxy user name"), group=N_("Connection"), advanced=True),
    "allow_plain": S("bool", False, N_("Allow PLAIN sign-in"),
                     N_("Only over an encrypted connection; needed by some old servers"), group=N_("Security")),
    "pinned_certs": S("json", [], group=N_("Security")),
    "client_cert": S("str", "", N_("Client certificate"), N_("Sign in with a certificate (SASL EXTERNAL)"),
                     group=N_("Security"), advanced=True),
    "store_password": S("choice", "keyring", N_("Remember password"), group=N_("Security"),
                        choices=(("keyring", N_("In the system keyring")), ("file", N_("In a file (less safe)")),
                                 ("never", N_("Ask every time")))),
    "encryption": S("choice", "default", N_("Default encryption"), group=N_("Security"),
                    choices=(("default", N_("Like Preferences")), ("always", N_("Always")),
                             ("available", N_("When available")), ("off", N_("Off")))),
    "pgp_key": S("str", "", N_("OpenPGP key"), group=N_("Security")),
    "answer_version": S("bool", False, N_("Tell others which app you use"), group=N_("Privacy")),
    "answer_time": S("bool", False, N_("Tell others your local time"), group=N_("Privacy")),
    "answer_last": S("bool", False, N_("Tell others when you were last active"), group=N_("Privacy")),
    "send_idle": S("bool", True, N_("Send idle time"), N_("Contacts see how long you have been away"),
                   group=N_("Privacy")),
    "first_sync_days": S("int", 7, N_("Days of history to load on first sync"), group=N_("History"), min=1,
                         max=365),
    "retention_days": S("choice", 0, N_("Keep local history"), group=N_("History"),
                        choices=((0, N_("Forever")), (30, N_("30 days")), (90, N_("90 days")),
                                 (365, N_("365 days")))),
    "image_quality": S("choice", "balanced", N_("Image upload quality"), group=N_("Files"),
                       choices=(("original", N_("Original")), ("balanced", N_("Balanced")),
                                ("small", N_("Small")))),
    "autodownload_mb": S("int", 10, N_("Download automatically up to"),
                         N_("Images and voice messages from contacts"), group=N_("Files"), min=0, max=1000,
                         unit="MB"),
    "allow_large_uploads": S("bool", False, N_("Allow uploads larger than 100 MB"), group=N_("Files")),
    "nickname": S("str", "", N_("Nickname"), group=N_("Profile")),
}


def default(key: str) -> Any:
    v = SPECS[key].default
    return list(v) if isinstance(v, list) else v


def account_default(key: str) -> Any:
    v = ACCOUNT_SPECS[key].default
    return list(v) if isinstance(v, list) else v


def coerce(spec: Spec, value: Any) -> Any:
    """Return the value converted to the spec's type, or raise ValueError with a readable message."""
    t = spec.type
    if t == "bool":
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected on/off, got %r" % value)
        return bool(value)
    if t == "int":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("expected a whole number, got %r" % (value,)) from None
        if spec.min is not None and n < spec.min:
            raise ValueError("must be at least %s" % spec.min)
        if spec.max is not None and n > spec.max:
            raise ValueError("must be at most %s" % spec.max)
        return n
    if t == "choice":
        for c, _label in spec.choices:
            if c == value or (isinstance(c, int) and not isinstance(value, bool) and str(c) == str(value)):
                return c
        raise ValueError("must be one of: %s" % ", ".join(str(c) for c, _ in spec.choices))
    if t == "list":
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, (list, tuple)):
            raise ValueError("expected a list")
        return [str(x).strip() for x in value if str(x).strip()]
    if t == "time_range":
        v = "" if value is None else str(value).strip()
        if v and parse_time_range(v) is None:
            raise ValueError("expected HH:MM-HH:MM, got %r" % v)
        return v
    if t == "json":
        return value
    return "" if value is None else str(value)


def parse_time_range(v: str) -> tuple[int, int] | None:
    """'22:00-07:30' -> (1320, 450) minutes after midnight."""
    try:
        a, b = v.split("-")

        def mins(x):
            h, m = x.strip().split(":")
            h, m = int(h), int(m)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
            return h * 60 + m

        return mins(a), mins(b)
    except (ValueError, AttributeError):
        return None


def in_time_range(v: str, minute_of_day: int) -> bool:
    r = parse_time_range(v) if v else None
    if r is None:
        return False
    a, b = r
    if a == b:
        return False
    return a <= minute_of_day < b if a < b else (minute_of_day >= a or minute_of_day < b)
