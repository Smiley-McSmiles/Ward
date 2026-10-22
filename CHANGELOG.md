# Changelog

## v1.0.2 (2026-10-22)

Encryption keys can now be backed up and restored. Preferences > Encryption > Your devices > Manage has **Export
this device…** and **Import a device…**, which keep this device's OMEMO identity and every session in a
passphrase-protected file. Restoring it on a new install keeps the same device ID and fingerprint, so contacts do not
have to approve a new device after an update, a reinstall or a move to another computer.

OpenPGP keys get **Export public key…** and **Export secret key (backup)…** in an account's settings. The secret-key
file can be protected with the key's own passphrase, and importing it on a fresh account selects it for that account.
Key backups are written readable only by the user (0600), and importing a damaged or incompatible device backup
leaves the keys already on the device untouched.

## v1.0.1 (2026-10-20)

Calls work with Conversations. Ward's calls used a `<jingle-message>` wrapper around the call invitations that no
other app sends or understands, so Conversations never saw Ward's calls and Ward never saw theirs. They are rebuilt
on the standard Jingle Message Initiation (XEP-0353), with the Jingle-to-SDP translation Conversations uses, and are
tested end to end: Opus and VP8 both ways, answering on one of two devices, declining, cancelling, busy. The STUN and
TURN settings (server discovery, manual servers, shared-secret credentials, relay only) and the Preferences test stay.

The session descriptions now carry the ICE candidates gathered so far as well as trickling the rest, Ward advertises
SDP offer/answer (urn:ietf:rfc:3264) so Conversations does the same, and answer content names follow the offer even
when a webrtcbin version renames its mids. GStreamer errors (no microphone, no camera) end a call with a reason
instead of leaving it "Connecting" until the timeout. Audio-only calls make no video sink, so they work on a system
without a display server.

Preferences > Calls > Audio lets you choose the microphone and speaker GStreamer should use, and set their volumes;
a call in progress follows volume changes. STUN and TURN stay empty by default and come from the server's discovery
or from what you enter in Preferences; calls between different networks need one of them. Every call writes a short
trace to ~/.local/state/ward/calls.log, independent of the Debug log setting, including candidate types and the
peer's termination reason.

Video calls can share the desktop: the call window has a Share screen button, using the XDG ScreenCast portal and
PipeWire on Wayland and ximagesrc on X11.  The camera and screen are two branches of the same video track, so
switching needs no renegotiation and the other side keeps seeing the same call.

The AppImage bundles GtkSourceView's language specs and style schemes, so code blocks are highlighted there too.
Scrolling with the pointer over a code block or table reaches the message list on GTK 4.14, the composer's attach,
emoji and send buttons are vertically centered, and Ctrl+W closes the window even where GTK's application
accelerators do not pass through.

An AppImage: one file with Python, GTK, libadwaita, GStreamer's WebRTC with libnice and the OMEMO libraries inside,
for distributions where Ward's dependencies are missing or too old. Calls work in it on Void, whose own GStreamer has
no WebRTC. `./package.sh appimage` builds it; CI builds and starts it. The image includes NSS's crypto modules
(libsoftokn3, libfreebl3, ...): libsrtp2 is built against NSS, which loads them by name, and without them SRTP could
not start, so calls connected but carried no voice. Its own encryption libraries come before an existing
distribution's private folder, whose modules are built for another Python and used to hide the image's.

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
