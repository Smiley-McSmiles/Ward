#!/usr/bin/env bash
# ==============================================================================
# Ward packaging - one script for every Linux distribution family and the BSDs.
#
# Builds: a portable source tarball (.tar.gz with install.sh), Debian/Ubuntu (.deb), Fedora/RHEL/openSUSE
# (.rpm), Arch (.pkg.tar.xz), Void (.xbps), FreeBSD (.pkg, on FreeBSD) and a Flatpak bundle, plus recipes for
# Alpine (APKBUILD), Gentoo (ebuild), the FreeBSD ports tree, OpenBSD ports and NetBSD pkgsrc, and SHA256SUMS.
# The .deb and Arch packages are written by small Python scripts, so they build on any distribution.
#
#   ./package.sh                     everything this machine can build (= --all); a missing tool only skips that
#                                    package, the summary says why, and recipes are written regardless
#   ./package.sh --deb --arch        selected packages (plain names work too: ./package.sh deb arch)
#   ./package.sh --container --rpm   build inside a Fedora container (podman or docker), from any distribution
#   ./package.sh --version 1.1.0 --all   set the version everywhere first (tools/set_version.py), then build;
#                                    pre-releases like 1.1.0-rc1 work and sort before 1.1.0 in every package format
#   ./package.sh --help
# ==============================================================================
# shellcheck disable=SC2016,SC2012,SC2001   # scriptlets are single-quoted on purpose: they expand at install time
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"
# --version X.Y.Z[-rc1] (or --version=...) sets the version everywhere before anything is built
args=(); new_version=""
while [ $# -gt 0 ]; do
    case "$1" in
        --version) new_version=${2:-}; [ -n "$new_version" ] || { echo "--version needs a version, e.g. --version 1.1.0" >&2; exit 2; }; shift ;;
        --version=*) new_version=${1#*=} ;;
        *) args+=("$1") ;;
    esac
    shift
done
set -- ${args[@]+"${args[@]}"}
if [ -n "$new_version" ]; then python3 "$ROOT/tools/set_version.py" "$new_version" || exit 2; fi
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ward/__init__.py)
# The version as each format spells it: deb and rpm sort "~" before the release (1.1.0~rc1 < 1.1.0), Alpine and
# Gentoo use "_rc1", Arch, Void and the BSD package tools take no "-".  The same for a plain release (1.1.0).
case "$VERSION" in
    *-*) DEBVER=${VERSION/-/\~}; ALPVER=${VERSION/-/_}; PLAINVER=${VERSION/-/} ;;
    *) DEBVER=$VERSION; ALPVER=$VERSION; PLAINVER=$VERSION ;;
esac
NAME=ward
APPID=io.github.smiley_mcsmiles.Ward
DIST=$ROOT/dist
BUILD=$ROOT/build
SRCNAME=$NAME-$VERSION
URL=https://github.com/smiley-mcsmiles/ward
DESC="XMPP client for GTK 4 and libadwaita, encrypted by default"
MAINT="Ward contributors <ward@users.noreply.github.com>"
LONGDESC="Ward is a modern XMPP (Jabber) messenger. Chats are end-to-end encrypted with OMEMO by default; OpenPGP
shares its keys with GPGMan. Markdown messages, groups and channels with moderation, replies, reactions,
edits, files, several accounts, message requests, full-text search, server administration and an adaptive
interface for desktops and phones."

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m[done]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
# A package that cannot be built on this machine (tool missing) exits 3: "skipped", not "failed".
skip() { warn "$*"; exit 3; }

SRC_ITEMS=(ward data packaging po tools tests install.sh package.sh README.md ROADMAP.md LICENSE pyproject.toml)

stage() {  # stage <destdir> <prefix> [install.sh options...]
    local dest=$1 prefix=$2
    shift 2
    rm -rf "$dest"
    mkdir -p "$dest"
    DESTDIR=$dest "$ROOT/install.sh" --prefix "$prefix" --no-post "$@" >/dev/null
    find "$dest" -name '*.pyc' -delete
    find "$dest" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
}

# Scriptlets shared by deb/rpm/arch/void: refresh the icon cache and the desktop database (which registers the
# xmpp: link handler), drop stale bytecode, and remove an older install.sh copy that would shadow the package.
POST_COMMON='find /usr/lib/ward -name __pycache__ -type d -exec rm -rf {} + >/dev/null 2>&1 || true
if [ -e /usr/local/lib/ward/ward/__init__.py ]; then
    rm -rf /usr/local/lib/ward /usr/local/share/ward
    rm -f /usr/local/bin/ward
fi
for d in /usr/share/icons/hicolor; do
    [ -d "$d" ] || continue
    for t in gtk4-update-icon-cache gtk-update-icon-cache; do
        if command -v $t >/dev/null 2>&1; then $t -q -f -t "$d" >/dev/null 2>&1 && break; fi
    done
done
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || :; fi'

POSTRM_COMMON='for d in /usr/share/icons/hicolor; do
    [ -d "$d" ] || continue
    for t in gtk4-update-icon-cache gtk-update-icon-cache; do
        if command -v $t >/dev/null 2>&1; then $t -q -f -t "$d" >/dev/null 2>&1 && break; fi
    done
done
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || :; fi'

# ---------------------------------------------------------------------------------------------- tarball
build_tar() {
    [ -n "${PKG_TAR_DONE:-}" ] && [ -f "$DIST/$SRCNAME.tar.gz" ] && return 0
    log "tarball"
    mkdir -p "$DIST" "$BUILD"
    local t=$BUILD/$SRCNAME
    rm -rf "$t"
    mkdir -p "$t"
    for i in "${SRC_ITEMS[@]}"; do [ -e "$i" ] && cp -R "$i" "$t/"; done
    rm -rf "$t/tests/screenshots"
    find "$t" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    chmod -R u+rwX,go+rX,go-w "$t"
    chmod 755 "$t/install.sh" "$t/package.sh"
    tar -C "$BUILD" -czf "$DIST/$SRCNAME.tar.gz" "$SRCNAME"
    log "  -> dist/$SRCNAME.tar.gz"
}

# ---------------------------------------------------------------------------------------------- Debian
DEB_DEPENDS="python3 (>= 3.11), python3-gi, gir1.2-gtk-4.0 (>= 4.12), gir1.2-adw-1 (>= 1.4), python3-slixmpp (>= 1.8), python3-markdown-it, python3-cryptography"
DEB_RECOMMENDS="python3-pip, gir1.2-gtksource-5, gir1.2-secret-1, gnupg, python3-pil, python3-linkify-it, python3-mdit-py-plugins, libgtk-4-media-gstreamer, desktop-file-utils, gstreamer1.0-plugins-bad, gir1.2-gst-plugins-bad-1.0, gir1.2-gst-plugins-base-1.0, gstreamer1.0-nice, gstreamer1.0-plugins-good"
DEB_SUGGESTS="gir1.2-spelling-1, zbar-tools, gstreamer1.0-gtk4"

build_deb() {
    have python3 || skip "python3 not found, cannot build the deb"
    log "deb"
    local root=$BUILD/deb/${NAME}_$VERSION
    stage "$root" /usr
    mkdir -p "$root/DEBIAN" "$root/usr/share/doc/$NAME"
    cp LICENSE "$root/usr/share/doc/$NAME/copyright"
    gzip -9nf "$root/usr/share/man/man1/ward.1"
    local size
    size=$(du -sk "$root" | cut -f1)
    {
        echo "Package: $NAME"
        echo "Version: $DEBVER"
        echo "Section: net"
        echo "Priority: optional"
        echo "Architecture: all"
        echo "Installed-Size: $size"
        echo "Depends: $DEB_DEPENDS"
        echo "Recommends: $DEB_RECOMMENDS"
        echo "Suggests: $DEB_SUGGESTS"
        echo "Maintainer: $MAINT"
        echo "Homepage: $URL"
        echo "Description: $DESC"
        echo "$LONGDESC" | tr '\n' ' ' | fold -s -w 76 | awk '{ sub(/ +$/, ""); print " " $0 }'
        echo " ."
        echo " The OMEMO libraries are not in Debian yet: Ward offers to download them"
        echo " into its own folder the first time you write an encrypted message."
    } > "$root/DEBIAN/control"
    printf '#!/bin/sh\nset -e\nif [ "$1" = configure ]; then\n%s\nfi\nexit 0\n' "$POST_COMMON" > "$root/DEBIAN/postinst"
    printf '#!/bin/sh\nset -e\nif [ "$1" = remove ] || [ "$1" = purge ]; then\n%s\nfi\nexit 0\n' "$POSTRM_COMMON" > "$root/DEBIAN/postrm"
    chmod 755 "$root/DEBIAN/postinst" "$root/DEBIAN/postrm"
    mkdir -p "$DIST"
    python3 "$ROOT/packaging/mkdeb.py" "$root" "$DIST/${NAME}_${DEBVER}_all.deb"
    log "  -> dist/${NAME}_${DEBVER}_all.deb"
}

# ---------------------------------------------------------------------------------------------- RPM
recipe_rpm() {
    mkdir -p "$DIST/recipes"
    cat > "$DIST/recipes/$NAME.spec" <<SPEC
# Keep real file mtimes: clamping them to the changelog date makes Python trust stale bytecode after upgrades
%global clamp_mtime_to_source_date_epoch 0
%global source_date_epoch_from_changelog 0

Name:           $NAME
Version:        $DEBVER
Release:        1%{?dist}
Summary:        $DESC
License:        MIT
URL:            $URL
Source0:        $SRCNAME.tar.gz
BuildArch:      noarch
Requires:       python3 >= 3.11
Requires:       python3-gobject
Requires:       (gtk4 >= 4.12 or typelib-1_0-Gtk-4_0)
Requires:       (libadwaita >= 1.4 or typelib-1_0-Adw-1)
Requires:       python3-slixmpp >= 1.8
Requires:       python3-markdown-it-py
Requires:       python3-cryptography
Recommends:     python3-pip
Recommends:     (gtksourceview5 or typelib-1_0-GtkSource-5)
Recommends:     (libsecret or typelib-1_0-Secret-1)
Recommends:     (gnupg2 or gpg2)
Recommends:     (python3-pillow or python3-Pillow)
Recommends:     python3-linkify-it-py
Recommends:     (gstreamer1-plugins-bad-free or gstreamer-plugins-bad)
Recommends:     (libnice-gstreamer1 or gstreamer-plugins-libnice)
Recommends:     (gstreamer1-plugins-good or gstreamer-plugins-good)
Suggests:       libspelling
Suggests:       gstreamer1-plugin-gtk4

%description
$LONGDESC

The OMEMO libraries are not packaged yet: Ward offers to download them into its own folder the first time an
encrypted message is written.

%prep
%autosetup -n $SRCNAME

%build

%install
DESTDIR=%{buildroot} ./install.sh --prefix /usr --mandir %{_mandir}/man1 --no-post
find %{buildroot} -name __pycache__ -type d -prune -exec rm -rf {} +

%post
$(printf '%s' "$POST_COMMON")

%postun
$(printf '%s' "$POSTRM_COMMON")

%files
%license LICENSE
/usr/bin/ward
/usr/lib/ward
/usr/share/ward
/usr/lib/systemd/user/ward.service
/usr/share/applications/$APPID.desktop
/usr/share/metainfo/$APPID.metainfo.xml
/usr/share/icons/hicolor/*/apps/$APPID*
/usr/share/pixmaps/$APPID.svg
%{_mandir}/man1/ward.1*
/usr/share/bash-completion/completions/ward
/usr/share/zsh/site-functions/_ward
/usr/share/fish/vendor_completions.d/ward.fish

%changelog
* Sun Oct 18 2026 Ward contributors <ward@users.noreply.github.com> - $DEBVER-1
- Ward $VERSION
SPEC
}

build_rpm() {
    recipe_rpm
    have rpmbuild || skip "rpmbuild not found (dnf install rpm-build, or: ./package.sh --container --rpm); spec written to dist/recipes/$NAME.spec"
    log "rpm"
    build_tar
    local top=$BUILD/rpm
    rm -rf "$top"
    mkdir -p "$top"/{SOURCES,SPECS,BUILD,RPMS,SRPMS}
    cp "$DIST/$SRCNAME.tar.gz" "$top/SOURCES/"
    cp "$DIST/recipes/$NAME.spec" "$top/SPECS/"
    if ! rpmbuild --define "_topdir $top" -bb "$top/SPECS/$NAME.spec" >"$top/rpmbuild.log" 2>&1; then
        tail -n 40 "$top/rpmbuild.log" >&2
        echo "rpmbuild failed (full log: $top/rpmbuild.log)" >&2
        exit 1
    fi
    cp "$top"/RPMS/*/*.rpm "$DIST/"
    log "  -> dist/$(cd "$DIST" && ls -- *"$DEBVER"*.rpm)"
}

# ---------------------------------------------------------------------------------------------- Arch
ARCH_DEPENDS=(python python-gobject gtk4 libadwaita python-slixmpp python-markdown-it-py python-cryptography)
ARCH_OPTDEPENDS=('python-pip: let Ward install the OMEMO libraries' 'gtksourceview5: code highlighting'
                 'libsecret: keep passwords in the keyring' 'gnupg: OpenPGP encryption'
                 'python-pillow: smaller images' 'libspelling: spell check' 'python-linkify-it-py: link detection'
                 'gst-plugins-bad: calls' 'libnice: calls' 'gst-plugins-good: calls' 'gst-plugin-gtk4: calls')

recipe_arch() {
    mkdir -p "$DIST/recipes/arch"
    {
        echo "# Maintainer: $MAINT"
        echo "pkgname=$NAME"
        echo "pkgver=$PLAINVER"
        echo "pkgrel=1"
        echo "pkgdesc=\"$DESC\""
        echo "arch=('any')"
        echo "url=\"$URL\""
        echo "license=('MIT')"
        printf "depends=("; printf "'%s' " "${ARCH_DEPENDS[@]}"; echo ")"
        printf "optdepends=("; printf "'%s'\n            " "${ARCH_OPTDEPENDS[@]}"; echo ")"
        echo "source=(\"$SRCNAME.tar.gz::$URL/releases/download/v$VERSION/$SRCNAME.tar.gz\")"
        echo "sha256sums=('SKIP')"
        echo
        echo "package() {"
        echo "  cd \"\$srcdir/$SRCNAME\""
        echo "  DESTDIR=\"\$pkgdir\" ./install.sh --prefix /usr --no-post"
        echo "  install -Dm644 LICENSE \"\$pkgdir/usr/share/licenses/\$pkgname/LICENSE\""
        echo "}"
    } > "$DIST/recipes/arch/PKGBUILD"
}

build_arch() {
    recipe_arch
    have python3 || skip "python3 not found, cannot build the Arch package"
    log "arch"
    local root=$BUILD/arch/${NAME}-$VERSION
    stage "$root" /usr
    mkdir -p "$BUILD/arch" "$DIST" "$root/usr/share/licenses/$NAME"
    cp LICENSE "$root/usr/share/licenses/$NAME/LICENSE"
    printf 'post_install() {\n%s\n}\npost_upgrade() { post_install; }\npost_remove() {\n%s\n}\n' \
        "$POST_COMMON" "$POSTRM_COMMON" > "$BUILD/arch/ward.install"
    local args=() d
    for d in "${ARCH_DEPENDS[@]}"; do args+=(--depend "$d"); done
    for d in "${ARCH_OPTDEPENDS[@]}"; do args+=(--optdepend "$d"); done
    python3 "$ROOT/packaging/mkarch.py" "$root" "$DIST/$NAME-$PLAINVER-1-any.pkg.tar.xz" \
        --name "$NAME" --version "$PLAINVER" --desc "$DESC" --url "$URL" --packager "$MAINT" \
        "${args[@]}" --install "$BUILD/arch/ward.install" >/dev/null
    log "  -> dist/$NAME-$PLAINVER-1-any.pkg.tar.xz   (install: sudo pacman -U ...)"
}

# ---------------------------------------------------------------------------------------------- Void
VOID_DEPENDS="python3 python3-gobject gtk4 libadwaita python3-slixmpp python3-markdown-it-py python3-cryptography"

recipe_void() {
    mkdir -p "$DIST/recipes/void"
    cat > "$DIST/recipes/void/template" <<TPL
# Template file for 'ward' (copy to srcpkgs/ward/template in void-packages)
pkgname=$NAME
version=$PLAINVER
revision=1
wrksrc="$SRCNAME"
build_style=meta
depends="$VOID_DEPENDS"
short_desc="$DESC"
maintainer="$MAINT"
license="MIT"
homepage="$URL"
distfiles="\${homepage}/releases/download/v$VERSION/$SRCNAME.tar.gz"
checksum=SKIP

do_install() {
	DESTDIR=\${DESTDIR} ./install.sh --prefix /usr --no-post
	vlicense LICENSE
	# no systemd on Void: the runit user service is in /usr/share/ward/runit
	rm -rf \${DESTDIR}/usr/lib/systemd
}
TPL
}

build_void() {
    recipe_void
    have xbps-create || skip "xbps-create not found (it comes with Void's xbps); template written to dist/recipes/void/template"
    log "void"
    local root=$BUILD/void/${NAME}-$VERSION
    stage "$root" /usr
    mkdir -p "$DIST"
    (cd "$DIST" && xbps-create -A noarch -n "${NAME}-${PLAINVER}_1" -s "$DESC" -D "$VOID_DEPENDS" -H "$URL" -l MIT \
        -m "$MAINT" -t "net" "$root" >/dev/null)
    log "  -> dist/${NAME}-${PLAINVER}_1.noarch.xbps   (install: sudo xbps-install -R dist $NAME)"
}

# ---------------------------------------------------------------------------------------------- Alpine
recipe_alpine() {
    mkdir -p "$DIST/recipes/alpine"
    cat > "$DIST/recipes/alpine/APKBUILD" <<APK
# Maintainer: $MAINT
pkgname=$NAME
pkgver=$ALPVER
pkgrel=0
pkgdesc="$DESC"
url="$URL"
arch="noarch"
license="MIT"
depends="python3 py3-gobject3 gtk4.0 libadwaita py3-slixmpp py3-markdown-it-py py3-cryptography"
subpackages="\$pkgname-doc \$pkgname-bash-completion \$pkgname-zsh-completion \$pkgname-fish-completion"
source="$SRCNAME.tar.gz::$URL/releases/download/v$VERSION/$SRCNAME.tar.gz"
builddir="\$srcdir/$SRCNAME"
options="!check"   # the test-suite needs a display and a Prosody server

package() {
	DESTDIR="\$pkgdir" ./install.sh --prefix /usr --no-post
	install -Dm644 LICENSE "\$pkgdir"/usr/share/licenses/\$pkgname/LICENSE
}
APK
}

build_alpine() {
    recipe_alpine
    skip "an Alpine .apk is built with abuild on Alpine; APKBUILD written to dist/recipes/alpine/ (run abuild -r there)"
}

# ---------------------------------------------------------------------------------------------- Gentoo
recipe_gentoo() {
    mkdir -p "$DIST/recipes/gentoo/net-im/ward"
    cat > "$DIST/recipes/gentoo/net-im/ward/ward-$ALPVER.ebuild" <<'EBUILD'
# Copyright 2026 Ward contributors
# Distributed under the terms of the MIT license

EAPI=8

PYTHON_COMPAT=( python3_{11..13} )

inherit python-single-r1 xdg

DESCRIPTION="XMPP client for GTK 4 and libadwaita, encrypted by default"
HOMEPAGE="https://github.com/smiley-mcsmiles/ward"
# a pre-release is 1.1.0_rc1 here and 1.1.0-rc1 upstream
MY_PV="${PV/_/-}"
MY_P="${PN}-${MY_PV}"
SRC_URI="https://github.com/smiley-mcsmiles/ward/releases/download/v${MY_PV}/${MY_P}.tar.gz"
S="${WORKDIR}/${MY_P}"

LICENSE="MIT"
SLOT="0"
KEYWORDS="~amd64 ~arm64"
IUSE="gpg spell"
REQUIRED_USE="${PYTHON_REQUIRED_USE}"

RDEPEND="
	${PYTHON_DEPS}
	>=gui-libs/gtk-4.12:4[introspection]
	>=gui-libs/libadwaita-1.4[introspection]
	x11-libs/gtksourceview:5[introspection]
	app-crypt/libsecret[introspection]
	gpg? ( app-crypt/gnupg )
	spell? ( app-text/libspelling )
	$(python_gen_cond_dep '
		dev-python/pygobject:3[${PYTHON_USEDEP}]
		>=dev-python/slixmpp-1.8[${PYTHON_USEDEP}]
		dev-python/markdown-it-py[${PYTHON_USEDEP}]
		dev-python/cryptography[${PYTHON_USEDEP}]
		dev-python/pillow[${PYTHON_USEDEP}]
	')
"

src_compile() { :; }

src_install() {
	DESTDIR="${D}" ./install.sh --prefix "${EPREFIX}/usr" --no-post || die
	sed -i "s|^PY=\${WARD_PYTHON:-}|PY=\${WARD_PYTHON:-${PYTHON}}|" "${ED}/usr/bin/ward" || die
	dodoc README.md
}
EBUILD
}

build_gentoo() {
    recipe_gentoo
    skip "Gentoo builds from the ebuild; written to dist/recipes/gentoo/net-im/ward/ (copy it into a local overlay)"
}

# ---------------------------------------------------------------------------------------------- FreeBSD
recipe_freebsd() {
    local port=$DIST/recipes/freebsd/net-im/ward
    mkdir -p "$port"
    cat > "$port/Makefile" <<MK
PORTNAME=	ward
DISTVERSION=	$VERSION
CATEGORIES=	net-im python
MASTER_SITES=	$URL/releases/download/v\${DISTVERSION}/

MAINTAINER=	ward@users.noreply.github.com
COMMENT=	$DESC
WWW=		$URL

LICENSE=	MIT
LICENSE_FILE=	\${WRKSRC}/LICENSE

RUN_DEPENDS=	\${PYTHON_PKGNAMEPREFIX}slixmpp>=1.8:net-im/py-slixmpp@\${PY_FLAVOR} \\
		\${PYTHON_PKGNAMEPREFIX}markdown-it-py>0:textproc/py-markdown-it-py@\${PY_FLAVOR} \\
		\${PYTHON_PKGNAMEPREFIX}cryptography>0:security/py-cryptography@\${PY_FLAVOR} \\
		\${PYTHON_PKGNAMEPREFIX}pillow>0:graphics/py-pillow@\${PY_FLAVOR} \\
		\${PYTHON_PKGNAMEPREFIX}pip>0:devel/py-pip@\${PY_FLAVOR} \\
		gtksourceview5>0:x11-toolkits/gtksourceview5 \\
		libsecret>0:security/libsecret

USES=		desktop-file-utils gnome python:3.11+ shebangfix
USE_GNOME=	gtk40 libadwaita pygobject3
NO_ARCH=	yes
NO_BUILD=	yes

OPTIONS_DEFINE=	GPG
OPTIONS_DEFAULT=	GPG
GPG_DESC=	OpenPGP encryption with GnuPG
GPG_RUN_DEPENDS=	gpg:security/gnupg

do-install:
	cd \${WRKSRC} && \${SETENV} DESTDIR=\${STAGEDIR} ./install.sh --prefix \${PREFIX} \\
		--mandir \${PREFIX}/share/man/man1 --no-post --no-system-links
	\${REINPLACE_CMD} -e 's|^PY=\$\${WARD_PYTHON:-}|PY=\$\${WARD_PYTHON:-\${PYTHON_CMD}}|' \${STAGEDIR}\${PREFIX}/bin/ward

.include <bsd.port.mk>
MK
    printf '%s\n\nWWW: %s\n' "$LONGDESC" "$URL" > "$port/pkg-descr"
    local s=$BUILD/plist/freebsd
    WARD_NO_SYSTEMD=1 stage "$s" /usr/local --mandir /usr/local/share/man/man1
    python3 "$ROOT/packaging/plist.py" "$s" /usr/local freebsd > "$port/pkg-plist"
}

build_freebsd() {
    recipe_freebsd
    if [ "$(uname -s)" != FreeBSD ] || ! have pkg; then
        skip "a FreeBSD .pkg is built on FreeBSD; the port was written to dist/recipes/freebsd/net-im/ward/"
    fi
    log "freebsd"
    local root=$BUILD/freebsd/stage
    WARD_NO_SYSTEMD=1 stage "$root" /usr/local --mandir /usr/local/share/man/man1 --no-system-links
    gzip -9nf "$root/usr/local/share/man/man1/ward.1"
    local py
    py=$(python3 -c 'import sys; print("py%d%d" % sys.version_info[:2])')
    local manifest=$BUILD/freebsd/+MANIFEST
    {
        echo "name: $NAME"
        echo "version: \"$PLAINVER\""
        echo "origin: net-im/ward"
        echo "comment: \"$DESC\""
        echo "desc: \"$(echo "$LONGDESC" | tr '\n' ' ')\""
        echo "maintainer: ward@users.noreply.github.com"
        echo "www: $URL"
        echo "abi: \"FreeBSD:*:*\""
        echo "arch: \"freebsd:*:*\""
        echo "prefix: /usr/local"
        echo "licenselogic: single"
        echo "licenses: [MIT]"
        echo "deps: {"
        for d in python3 "$py-gobject3" gtk4 libadwaita "$py-slixmpp" "$py-markdown-it-py" "$py-cryptography"; do
            echo "  $d: { origin: \"$(pkg query '%o' "$d" 2>/dev/null || echo unknown)\", version: \"$(pkg query '%v' "$d" 2>/dev/null || echo 0)\" },"
        done
        echo "}"
        echo "scripts: { post-install: \"update-desktop-database -q /usr/local/share/applications >/dev/null 2>&1 || true\" }"
    } > "$manifest"
    python3 "$ROOT/packaging/plist.py" "$root" /usr/local freebsd | sed 's|^|/usr/local/|' > "$BUILD/freebsd/plist"
    pkg create -M "$manifest" -p "$BUILD/freebsd/plist" -r "$root" -o "$DIST"
    log "  -> dist/$NAME-$PLAINVER.pkg   (install: sudo pkg add dist/$NAME-$PLAINVER.pkg)"
}

# ---------------------------------------------------------------------------------------------- OpenBSD
recipe_openbsd() {
    local port=$DIST/recipes/openbsd/net/ward
    mkdir -p "$port/pkg"
    cat > "$port/Makefile" <<MK
COMMENT =		XMPP client for GTK 4, encrypted by default

DISTNAME =		ward-$VERSION
PKGNAME =		ward-$PLAINVER
CATEGORIES =		net x11
HOMEPAGE =		$URL
MAINTAINER =		Ward contributors <ward@users.noreply.github.com>
SITES =			$URL/releases/download/v$VERSION/

# MIT
PERMIT_PACKAGE =	Yes

MODULES =		lang/python
MODPY_BUILDDEP =	No
NO_BUILD =		Yes
NO_TEST =		Yes

RUN_DEPENDS =		devel/desktop-file-utils \\
			devel/py-gobject3 \\
			net/py-slixmpp \\
			security/py-cryptography \\
			textproc/py-markdown-it-py \\
			x11/gnome/libadwaita \\
			x11/gtk+4,-main \\
			x11/gtk+4,-cloudproviders \\
			x11/gtksourceview5 \\
			security/gnupg

do-install:
	cd \${WRKSRC} && DESTDIR=\${DESTDIR} ./install.sh --prefix \${PREFIX} \\
		--mandir \${PREFIX}/man/man1 --no-post --no-system-links
	sed -i 's|^PY=\$\${WARD_PYTHON:-}|PY=\$\${WARD_PYTHON:-\${MODPY_BIN}}|' \${PREFIX}/bin/ward

.include <bsd.port.mk>
MK
    echo "$LONGDESC" > "$port/pkg/DESCR"
    local s=$BUILD/plist/openbsd
    WARD_NO_SYSTEMD=1 stage "$s" /usr/local --mandir /usr/local/man/man1
    python3 "$ROOT/packaging/plist.py" "$s" /usr/local openbsd > "$port/pkg/PLIST"
}

build_openbsd() {
    recipe_openbsd
    skip "OpenBSD packages are built from the port: dist/recipes/openbsd/net/ward/ (copy to /usr/ports/mystuff/net/ward, then make install)"
}

# ---------------------------------------------------------------------------------------------- NetBSD
recipe_netbsd() {
    local pkg=$DIST/recipes/netbsd/chat/ward
    mkdir -p "$pkg"
    cat > "$pkg/Makefile" <<MK
# \$NetBSD\$

DISTNAME=	ward-$VERSION
PKGNAME=	ward-$PLAINVER
CATEGORIES=	chat python
MASTER_SITES=	$URL/releases/download/v$VERSION/

MAINTAINER=	ward@users.noreply.github.com
HOMEPAGE=	$URL
COMMENT=	XMPP client for GTK 4 and libadwaita, encrypted by default
LICENSE=	mit

USE_LANGUAGES=	# none
NO_BUILD=	yes
USE_TOOLS+=	bash:run

DEPENDS+=	\${PYPKGPREFIX}-gobject3-[0-9]*:../../devel/py-gobject3
DEPENDS+=	\${PYPKGPREFIX}-slixmpp>=1.8:../../chat/py-slixmpp
DEPENDS+=	\${PYPKGPREFIX}-markdown-it-py-[0-9]*:../../textproc/py-markdown-it-py
DEPENDS+=	\${PYPKGPREFIX}-cryptography-[0-9]*:../../security/py-cryptography
DEPENDS+=	libadwaita>=1.4:../../x11/libadwaita
DEPENDS+=	gtk4>=4.12:../../x11/gtk4

PYTHON_VERSIONS_INCOMPATIBLE=	27 38 39 310

do-install:
	cd \${WRKSRC} && \${SETENV} DESTDIR=\${DESTDIR} ./install.sh --prefix \${PREFIX} \\
		--mandir \${PREFIX}/\${PKGMANDIR}/man1 --no-post --no-system-links

.include "../../lang/python/application.mk"
.include "../../mk/bsd.pkg.mk"
MK
    echo "$LONGDESC" > "$pkg/DESCR"
    local s=$BUILD/plist/netbsd
    WARD_NO_SYSTEMD=1 stage "$s" /usr/pkg --mandir /usr/pkg/man/man1
    python3 "$ROOT/packaging/plist.py" "$s" /usr/pkg netbsd > "$pkg/PLIST"
}

build_netbsd() {
    recipe_netbsd
    skip "NetBSD packages are built from pkgsrc: dist/recipes/netbsd/chat/ward/ (copy to /usr/pkgsrc/chat/ward, then make install)"
}

# ---------------------------------------------------------------------------------------------- Flatpak
build_flatpak() {
    local manifest=$ROOT/packaging/flatpak/$APPID.json
    mkdir -p "$DIST/recipes/flatpak"
    cp "$ROOT"/packaging/flatpak/*.json "$DIST/recipes/flatpak/"
    have flatpak-builder || skip "flatpak-builder not found; manifest copied to dist/recipes/flatpak/ (flatpak-builder --user --install build $APPID.json)"
    log "flatpak"
    local repo=$BUILD/flatpak/repo
    flatpak-builder --force-clean --repo="$repo" --install-deps-from=flathub "$BUILD/flatpak/build" "$manifest" \
        >"$BUILD/flatpak/build.log" 2>&1 || { tail -n 40 "$BUILD/flatpak/build.log" >&2; exit 1; }
    flatpak build-bundle "$repo" "$DIST/$NAME-$VERSION.flatpak" "$APPID"
    log "  -> dist/$NAME-$VERSION.flatpak   (install: flatpak install --user dist/$NAME-$VERSION.flatpak)"
}

build_recipes() {
    recipe_rpm; recipe_arch; recipe_void; recipe_alpine; recipe_gentoo; recipe_freebsd; recipe_openbsd; recipe_netbsd
    mkdir -p "$DIST/recipes/flatpak"
    cp "$ROOT"/packaging/flatpak/*.json "$DIST/recipes/flatpak/"
    log "recipes -> dist/recipes/"
}

show_help() {
    cat <<HELP
Ward packaging (version $VERSION)
Usage: ./package.sh [options]

  --all         every package this machine can build, and all recipes (the default)
  --tar         portable source tarball - unpack and run: sudo ./install.sh (or ./install.sh --prefix ~/.local)
  --deb         Debian, Ubuntu, Mint, Pop!_OS (.deb) - needs only python3
  --rpm         Fedora, RHEL, openSUSE (.rpm) - needs rpmbuild (or use --container)
  --arch        Arch, Manjaro, EndeavourOS (.pkg.tar.xz) and PKGBUILD - needs only python3
  --void        Void Linux (.xbps) and template - needs xbps-create
  --alpine      Alpine / postmarketOS APKBUILD
  --gentoo      Gentoo ebuild
  --freebsd     FreeBSD port (and a .pkg when run on FreeBSD)
  --openbsd     OpenBSD port
  --netbsd      NetBSD pkgsrc package
  --flatpak     Flatpak bundle (needs flatpak-builder) and manifest
  --recipes     only the recipes in dist/recipes/
  --container   build inside a Fedora container (podman/docker), e.g. --container --rpm
  --version V   set the version everywhere first (code, README, man page, metainfo, CHANGELOG), e.g.
                --version 1.1.0 --all, or a pre-release: --version 1.1.0-rc1 --all
  --clean       remove dist/ and build/
  --help, -h    this message

Plain names work too (./package.sh deb arch). Every build also writes dist/SHA256SUMS.  Output: $DIST/
HELP
}

release_files() {  # this version's packages in dist/, by their exact names (other versions are left out)
    local f
    for f in "$DIST/$SRCNAME.tar.gz" "$DIST/${NAME}_${DEBVER}_all.deb" "$DIST/$NAME-$DEBVER"-1.*.rpm \
             "$DIST/$NAME-$PLAINVER"-1-any.pkg.tar.* "$DIST/$NAME-${PLAINVER}"_1.*.xbps "$DIST/$NAME-$PLAINVER.pkg" \
             "$DIST/$NAME-$VERSION.flatpak"; do
        [ -f "$f" ] && echo "$f"
    done
    return 0
}

checksums() {
    local files=() f
    for f in $(release_files); do
        case "$f" in
            *.tar.gz|*.deb|*.rpm|*.pkg.tar.*|*.xbps|*.pkg|*.flatpak) [ -f "$f" ] && files+=("$(basename "$f")") ;;
        esac
    done
    [ ${#files[@]} -gt 0 ] || return 0
    if have sha256sum; then
        (cd "$DIST" && sha256sum "${files[@]}") > "$DIST/SHA256SUMS.tmp"
    elif have shasum; then
        (cd "$DIST" && shasum -a 256 "${files[@]}") > "$DIST/SHA256SUMS.tmp"
    elif have sha256; then
        (cd "$DIST" && sha256 -r "${files[@]}" | awk '{print $1 "  " $2}') > "$DIST/SHA256SUMS.tmp"
    else
        warn "no sha256sum, shasum or sha256: dist/SHA256SUMS not written"
        return 0
    fi
    mv "$DIST/SHA256SUMS.tmp" "$DIST/SHA256SUMS"
    log "checksums -> dist/SHA256SUMS (${#files[@]} files)"
}

summary() {
    checksums
    local f
    echo
    ok "Packages in $DIST/:"
    for f in $(release_files) "$DIST/SHA256SUMS"; do
        [ -f "$f" ] && printf '    %s\n' "$(basename "$f")"
    done
    [ -d "$DIST/recipes" ] && printf '    recipes/  (%s)\n' "$(cd "$DIST/recipes" && ls | tr '\n' ' ' | sed 's/ $//')"
    return 0
}

targets=()
container=0
for a in "$@"; do
    case "$a" in
        -h|--help|help) show_help; exit 0 ;;
        --container) container=1 ;;
        --*) targets+=("${a#--}") ;;
        *) targets+=("$a") ;;
    esac
done
[ ${#targets[@]} -eq 0 ] && targets=(all)

if [ "$container" -eq 1 ]; then
    engine=$(command -v podman || command -v docker || true)
    [ -n "$engine" ] || { echo "--container needs podman or docker" >&2; exit 1; }
    image=${PKG_IMAGE:-registry.fedoraproject.org/fedora:latest}
    [ "${targets[*]}" = all ] && targets=(rpm)
    log "building [${targets[*]}] in $image with $(basename "$engine")"
    "$engine" run --rm -v "$ROOT:/src:Z" -w /src "$image" bash -c \
        "dnf -y -q install rpm-build python3 tar gzip findutils sed >/dev/null && \
         cp -r /src /tmp/build-src && cd /tmp/build-src && rm -rf dist build && ./package.sh ${targets[*]} && \
         mkdir -p /src/dist && cp dist/*.rpm dist/*.tar.gz dist/*.deb /src/dist/ 2>/dev/null; true"
    summary
    exit 0
fi

if [ "${targets[0]}" = all ]; then
    # each package in its own process: one that cannot be built here must not stop the others
    built=(); skipped=(); failed=()
    build_tar || { warn "tarball failed"; failed+=(tar); }
    export PKG_TAR_DONE=1 PKG_CHILD=1
    for t in deb rpm arch void alpine gentoo freebsd openbsd netbsd flatpak recipes; do
        set +e
        bash "$ROOT/package.sh" "$t"
        rc=$?
        set -e
        case $rc in
            0) built+=("$t") ;;
            3) skipped+=("$t") ;;
            *) failed+=("$t") ;;
        esac
    done
    log "built:   tar ${built[*]:-}"
    [ ${#skipped[@]} -eq 0 ] || log "skipped: ${skipped[*]}  (their recipes are in dist/recipes/)"
    if [ ${#failed[@]} -gt 0 ]; then
        echo "FAILED:  ${failed[*]}" >&2
        exit 1
    fi
    summary
    exit 0
fi

for t in "${targets[@]}"; do
    case "$t" in
        tar) build_tar ;;
        deb) build_deb ;;
        rpm) build_rpm ;;
        arch) build_arch ;;
        void) build_void ;;
        alpine) build_alpine ;;
        gentoo) build_gentoo ;;
        freebsd) build_freebsd ;;
        openbsd) build_openbsd ;;
        netbsd) build_netbsd ;;
        flatpak) build_flatpak ;;
        recipes) build_recipes ;;
        clean) rm -rf "$DIST" "$BUILD"; log "removed dist/ and build/" ;;
        *) echo "unknown target: $t (see ./package.sh --help)" >&2; exit 2 ;;
    esac
done
if [ -z "${PKG_CHILD:-}" ] && [ "${targets[*]}" != clean ]; then summary; fi
