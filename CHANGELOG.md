# Changelog

## v1.0.1 (2026-10-19)

Sent messages now sync between devices on the same account, Wayland screen sharing works through the ScreenCast portal, calls leave a history line in their chat, and incoming calls ring.

## v1.0.0 (2026-10-18)

The first release.

- **Encrypted by default:** OMEMO 2 with Stanza Content Encryption and legacy OMEMO side by side, blind trust before
  verification or manual trust, fingerprints in the format other apps use, QR codes and verification links, new
  device banners, per-message lock and trust indicators, trust export and import. Encrypted groups for members-only
  rooms.
- **OpenPGP (optional):** XEP-0373/0374 with the GnuPG keyring that GPGMan uses, key choice per account, key
  generation, and importing keys that GPGMan exported.
- **Messaging:** Markdown with code highlighting, tables, quotes, task lists and spoilers (XEP-0393 styling for
  other apps), replies, reactions, edits with history, delete for everyone, receipts, read markers synced across
  devices, typing notifications, drafts, mentions, message requests and blocking.
- **Groups and channels:** private and public presets, invites, members panel, roles and moderation, room settings,
  bookmarks with autojoin, public channel search, history from the room archive.
- **Files:** HTTP upload with encryption (aesgcm links), thumbnails, metadata stripping, automatic downloads.
- **Accounts:** sign in, create an account (with the server's form and CAPTCHA), invite links, several accounts,
  certificate pinning, proxies and Tor, password change and account deletion.
- **Power tools:** server administration with any ad-hoc command as a native form, account invites, XML console,
  service discovery.
- **Desktop:** adaptive libadwaita interface from phones to wide screens, notifications, background mode, tray icon,
  unread badge, xmpp: links, generated preferences with search.
- **Packages:** install.sh for every Linux distribution and the BSDs, .deb, .rpm, Arch and Void packages, recipes for
  Alpine, Gentoo, FreeBSD, OpenBSD, NetBSD and a Flatpak manifest.
