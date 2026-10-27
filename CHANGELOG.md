# Changelog

## v1.0.5 (2026-10-26)

Ward 1.0.5 makes deleting mean deleting: a deleted message takes its photo or video with it, and destroying a group
removes the room from Ward instead of leaving an empty group in the sidebar for good.

## Deleting a message

Deleting your own message, or a group moderator deleting someone else's, now removes the image bubble along with the
message and deletes the cached media and thumbnail it had downloaded. Before, the tombstone stayed behind with the
photo still under it, the bytes still on disk.

## Deleting a group

Destroying a group as its owner now removes it locally as well: the sidebar entry, the conversation, the search index
rows and every cached photo and video the group held. The room is gone on the server, so nothing could ever reopen it,
and keeping the row only left an empty group behind.

Clearing a chat's history now also removes the media those messages left in the cache.

## v1.0.4 (2026-10-24)

Ward 1.0.4 makes creating and joining groups clearer, and fixes opening photos and videos in the AppImage on
desktops where GLib's default-app helper lives somewhere else.

## New groups

The New group dialog now suggests the room address from the group name and the server's group-chat service, for
example `blap@conference.example.org`. The advanced address can still be edited by hand.

Instead of typing comma-separated addresses, **Invite contacts** opens a searchable list of roster contacts with
checkboxes. The chosen contacts are invited after the room is created, and the dialog shows who is selected.

## Joining groups

A group-chat service domain such as `conference.example.org` is not a room. Ward now rejects it with a clear message
instead of sending a join presence and reporting “This group is full”. The Join group dialog also treats a bare name
or domain as search text and only joins a full room address such as `room@conference.example.org`.

## AppImage

The AppImage now bundles GLib's `gio-launch-desktop` helper and points `GIO_LAUNCH_DESKTOP` at it. Opening a photo or
video with the desktop's default app therefore works on Cinnamon, Arch and any other system where the bundled GLib
would otherwise look for the helper at a path that does not exist.

## v1.0.3 (2026-10-24)

The encryption menu in a chat is simpler and follows how OMEMO is actually used. **Default (OMEMO)** now means
legacy OMEMO 0.3, the version Conversations, Gajim and Dino use, and **OMEMO 2** is an explicit choice for chats
where every device supports it. **GPG/PGP** stays available when OpenPGP is enabled, and **Off** sends plain
messages. Chats saved with the old "OMEMO (automatic)" or "Legacy OMEMO only" values behave like the new default.

Removing one of your other devices now also deletes its published bundles, not just its entry in the device list.
That matters for clients which treat every bundle item as a device (Ward Android does): they would otherwise keep
encrypting for a removed device, even one that was lost or stolen. The OMEMO 2 bundle item and the legacy
per-device bundle node are both deleted. If deleting the device list fails, the bundle is left alone so other
clients can still reach the device the list says is active.

**Remove inactive devices** now also prunes orphaned OMEMO 2 bundles that older Ward versions left behind. It only
runs against a non-empty device list, so a failed or empty fetch cannot delete every bundle.

Reactions, edits and other body-less updates keep working when a chat uses legacy OMEMO. Since legacy OMEMO can
only carry a text body, they are sent without an OMEMO wrapper, the way Conversations sends them.

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
