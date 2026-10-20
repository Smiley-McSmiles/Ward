# Third-party notices

Ward is licensed under the MIT License; see [LICENSE](LICENSE). This file gives the copyright and license notices
for the third-party components that Ward uses or redistributes. The full texts of the copyleft and Creative Commons
licenses are in [LICENSES/](LICENSES/).

This notice does not change any component's license. Where a component has its own copyright notice, that notice and
its license apply to that component. Ward's own source remains available under the MIT License.

## Source code and license availability

Ward's source is at <https://github.com/Smiley-McSmiles/Ward>. Source code for the components below is available from
the upstream projects and from the source packages used to build a binary release:

| Component | Version used by the latest AppImage/Flatpak | Source |
| --- | --- | --- |
| oldmemo | 2.1.0 (the build installs the current `>=2.0,<3`) | <https://github.com/Syndace/python-oldmemo> · <https://pypi.org/project/oldmemo/> |
| sound-theme-freedesktop | 0.8 | <https://people.freedesktop.org/~mccann/dist/> |
| AppImage type 2 runtime | continuous build | <https://github.com/AppImage/type2-runtime> |
| Ubuntu 24.04 libraries | the Ubuntu 24.04 archive versions | <https://packages.ubuntu.com/noble/> |

The other Python components are unmodified releases from PyPI. Their source distributions and license files are on
their PyPI project pages and in the links in the tables below.

Ward does not modify oldmemo, the freedesktop sound theme, or the Ubuntu libraries it bundles. For the LGPL
components, the AppImage can be unpacked with `--appimage-extract` and its shared libraries can be replaced; the
Flatpak can be rebuilt from [packaging/flatpak/io.github.smiley_mcsmiles.Ward.json](packaging/flatpak/io.github.smiley_mcsmiles.Ward.json).
Those are the practical ways to exercise the LGPL relinking rights.

## Python components

These are bundled in the AppImage and Flatpak. On other installations they may come from the distribution, or Ward
may download them on request with pip.

| Component | License | Copyright / source |
| --- | --- | --- |
| slixmpp | MIT | <https://github.com/poezio/slixmpp> |
| markdown-it-py | MIT | <https://github.com/executablebooks/markdown-it-py> |
| cryptography | Apache-2.0 OR BSD-3-Clause | <https://github.com/pyca/cryptography> |
| omemo | MIT | <https://github.com/Syndace/python-omemo> |
| twomemo | MIT | <https://github.com/Syndace/python-twomemo> |
| x3dh | MIT | <https://github.com/Syndace/python-x3dh> |
| doubleratchet | MIT | <https://github.com/Syndace/python-doubleratchet> |
| xeddsa | MIT | <https://github.com/Syndace/python-xeddsa> |
| mdit-py-plugins | MIT | <https://github.com/executablebooks/mdit-py-plugins> |
| linkify-it-py | MIT | <https://github.com/tsutsu3/linkify-it-py> |
| segno | BSD-3-Clause | <https://github.com/heuer/segno> |
| Pillow | HPND | <https://github.com/python-pillow/Pillow> |
| pycairo | LGPL-2.1-only OR MPL-1.1 | <https://github.com/pygobject/pycairo> |
| pydantic, pydantic-core, annotated-types, typing-inspection | MIT | <https://github.com/pydantic/pydantic> |
| protobuf | BSD-3-Clause | <https://github.com/protocolbuffers/protobuf> |
| cffi | MIT-0 | <https://github.com/python-cffi/cffi> |
| pycparser | BSD-3-Clause | <https://github.com/eliben/pycparser> |
| xmlschema, elementpath | MIT | <https://github.com/sissaschool/xmlschema> · <https://github.com/sissaschool/elementpath> |

**oldmemo** is the one strong-copyleft Python component. It is licensed under the **GNU Affero General Public
License, version 3 only (AGPL-3.0-only)**. It is included in the AppImage and Flatpak because those builds install
the `legacy-omemo` optional dependency. The corresponding source for the version in your binary is available at the
links above; the complete license text is in [LICENSES/AGPL-3.0.txt](LICENSES/AGPL-3.0.txt). Ward itself remains
MIT-licensed.

## GTK, GStreamer and desktop libraries

These are normally provided by the system. The AppImage also bundles the Ubuntu 24.04 versions, and the Flatpak
uses the GNOME runtime versions.

| Component | License | Source / notice |
| --- | --- | --- |
| CPython | PSF-2.0 | <https://www.python.org/> · [LICENSES/Python-2.0.txt](LICENSES/Python-2.0.txt) |
| PyGObject | LGPL-2.1-or-later | <https://gitlab.gnome.org/GNOME/pygobject> |
| GTK 4 | LGPL-2.1-or-later (some files under other licenses) | <https://gitlab.gnome.org/GNOME/gtk> |
| GLib | LGPL-2.1-or-later | <https://gitlab.gnome.org/GNOME/glib> |
| libadwaita | LGPL-2.1-or-later | <https://gitlab.gnome.org/GNOME/libadwaita> |
| GdkPixbuf | LGPL-2.1-or-later | <https://gitlab.gnome.org/GNOME/gdk-pixbuf> |
| GtkSourceView | LGPL-2.1-or-later | <https://gitlab.gnome.org/GNOME/gtksourceview> |
| libspelling | LGPL-2.1-or-later | <https://gitlab.gnome.org/GNOME/libspelling> |
| libsecret | LGPL-2.1-or-later | <https://gitlab.gnome.org/GNOME/libsecret> |
| GStreamer | LGPL-2.1-or-later | <https://gstreamer.freedesktop.org/> |
| GStreamer plugins (base, good, bad-free, libav, nice, pipewire, alsa) | Mostly LGPL; some files MIT, BSD, MPL-1.1, CC0 or GPL-2.0 | See each Ubuntu source package's `debian/copyright` |
| libnice | LGPL-2.1-or-later OR MPL-1.1 | <https://gitlab.freedesktop.org/libnice/libnice> |
| Enchant | LGPL-2.0-or-later | <https://github.com/AbiWord/enchant> |
| dconf | LGPL-2.1-or-later | <https://gitlab.gnome.org/GNOME/dconf> |
| NSS | MPL-2.0 | <https://firefox-source-docs.mozilla.org/security/nss/> |
| OpenSSL | Apache-2.0 | <https://www.openssl.org/> |
| libsrtp | BSD-3-Clause | <https://github.com/cisco/libsrtp> |
| FFmpeg libraries (via gstreamer1.0-libav) | LGPL-2.1-or-later | <https://ffmpeg.org/> |
| Adwaita icon theme | LGPL-3.0-only OR CC-BY-SA-3.0 | <https://gitlab.gnome.org/GNOME/adwaita-icon-theme> |
| shared-mime-info | GPL-2.0-or-later | <https://gitlab.freedesktop.org/xdg/shared-mime-info> |

The AppImage runtime is MIT-licensed and contains statically linked **musl** (MIT) and **libfuse**
(LGPL-2.1). Its complete notice is in
[LICENSES/AppImage-runtime-LICENSE.txt](LICENSES/AppImage-runtime-LICENSE.txt).

The GStreamer and FFmpeg copyright notices are a large collection of individual files and licenses. The exact
notices for the Ubuntu packages are in the corresponding source package's `debian/copyright`; those packages are
unmodified. This notice does not relicense any of them.

**Codec patents are separate from copyright.** The FFmpeg/GStreamer libraries can decode H.264 and other codecs.
Their licenses do not grant patent rights, and some jurisdictions may require a patent license for distribution or
use of those codecs. Opus and VP8 are royalty-free.

## Notification and call sounds

The files below are unmodified copies from the freedesktop.org sound theme 0.8. They are installed in
`share/ward/sounds` and bundled in the AppImage and Flatpak.

| File | Copyright and license |
| --- | --- |
| `phone-incoming-call.oga`, `phone-outgoing-calling.oga` | Copyright Damien Sandras; **GPL-2.0-or-later**. From the Ekiga sound set via sound-theme-freedesktop. |
| `message-new-instant.oga` | From sound-theme-freedesktop 0.8. Its separate credit is not listed in the theme's `CREDITS`; the theme's aggregate licensing includes CC-BY-3.0, CC-BY-SA-3.0, GPL-2.0 and GPL-2.0-or-later. The copyright belongs to the theme contributors credited there. |

The theme's complete credits are reproduced in
[LICENSES/sound-theme-freedesktop-CREDITS.txt](LICENSES/sound-theme-freedesktop-CREDITS.txt). The source archive is
at <https://people.freedesktop.org/~mccann/dist/sound-theme-freedesktop-0.8.tar.bz2>. Full GPL-2.0 and Creative
Commons texts are in [LICENSES/](LICENSES/).

## Build-only components

The packaging tools and CI also use Ubuntu packages, `patchelf`, `squashfs-tools`, Flatpak, and the AppImage
runtime. They are not part of Ward's source license and are not shipped as Ward code. The AppImage runtime notice is
included above; the Ubuntu package notices are in their source packages.

## A note on the licenses

Nothing in this notice is legal advice. The purpose is to give recipients the attribution, license texts, source
locations and modification status that the third-party licenses ask for. If you redistribute a Ward binary, keep
this file and the [LICENSES/](LICENSES/) directory with it.
