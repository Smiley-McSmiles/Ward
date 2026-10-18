#!/bin/sh
# Ward installer - POSIX sh, for any Linux distribution and FreeBSD, OpenBSD, NetBSD and DragonFly BSD.
#
#   sudo ./install.sh                     install for every user (/usr/local); in a terminal it checks what is
#                                         missing and offers to install it (with legacy OMEMO)
#   ./install.sh --prefix ~/.local        install for yourself only (installing packages then uses sudo or doas)
#   sudo ./install.sh --install-deps      install Python, GTK 4, libadwaita and the libraries first
#   ./install.sh --vendor                 also fetch libraries your system has no packages for (OMEMO) with pip
#   ./install.sh --check                  only check this machine
#   sudo ./install.sh --uninstall
#   DESTDIR=/tmp/stage ./install.sh --prefix /usr --no-post      stage the files for a package
# shellcheck disable=SC2015   # "a && b || c" is used on purpose where c must run whenever the test fails
set -eu

umask 022
PATH="${PATH:-/usr/bin:/bin}:/usr/local/sbin:/usr/local/bin:/usr/pkg/bin:/usr/pkg/sbin:/usr/sbin:/sbin"
export PATH

PREFIX=/usr/local
DO_POST=1
UNINSTALL=0
DEPS=0
CHECK=0
VENDOR=0
LEGACY=1
ASK=ask         # ask | yes | no: whether to install missing dependencies
AS_ROOT=""      # sudo or doas, to run the package manager when we are not root
SYSLINKS=1
MANDIR_OPT=""
WARNINGS=""
SRC=$(cd "$(dirname "$0")" && pwd)
DESTDIR=${DESTDIR:-}
APPID=io.github.smiley_mcsmiles.Ward
MIN_PY=11      # Python 3.11

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    cat <<USAGE

Options:
  --prefix DIR          installation prefix (default /usr/local; ~/.local installs for the current user)
  --install-deps        install the dependencies with the system's package manager first
                        (apt, dnf, zypper, pacman, xbps, apk, emerge, FreeBSD pkg, OpenBSD pkg_add, NetBSD pkgin)
  --vendor              download what the system does not package (the OMEMO libraries...) with pip into
                        PREFIX/lib/ward/vendor.  Ward can also do this itself: Preferences > Encryption
  --no-legacy-omemo     leave out legacy OMEMO (oldmemo, AGPL-3.0) when fetching the OMEMO libraries
  -y, --yes             install missing dependencies without asking (packages, then OMEMO with pip)
  --no-deps             do not check for missing dependencies or offer to install them
  --mandir DIR          where the man page goes (default PREFIX/share/man/man1, PREFIX/man/man1 on OpenBSD
                        and NetBSD)
  --check               report what is missing on this machine, install nothing
  --no-post             do not refresh the icon and desktop caches
  --no-system-links     do not copy the launcher and icons into /usr/share when installing elsewhere
  --uninstall           remove Ward (your chats and settings in ~/.config and ~/.local are kept)
  -h, --help            show this help
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX=$2; shift ;;
        --prefix=*) PREFIX=${1#*=} ;;
        --no-post) DO_POST=0 ;;
        --uninstall) UNINSTALL=1 ;;
        --install-deps) DEPS=1 ;;
        --vendor) VENDOR=1 ;;
        --no-legacy-omemo) LEGACY=0 ;;
        -y|--yes) ASK=yes ;;
        --no-deps) ASK=no ;;
        --mandir) MANDIR_OPT=$2; shift ;;
        --mandir=*) MANDIR_OPT=${1#*=} ;;
        --check) CHECK=1 ;;
        --no-system-links) SYSLINKS=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
warn() {
    printf 'warning: %s\n' "$*" >&2
    WARNINGS="$WARNINGS
  - $*"
}

case "$PREFIX" in "~"*) PREFIX="$HOME${PREFIX#\~}" ;; esac
OS=$(uname -s)
LIBDIR=$PREFIX/lib/ward
SHAREDIR=$PREFIX/share
BINDIR=$PREFIX/bin
case "$OS" in
    OpenBSD|NetBSD) MANDIR=$PREFIX/man/man1 ;;
    *) MANDIR=$SHAREDIR/man/man1 ;;
esac
[ -n "$MANDIR_OPT" ] && MANDIR=$MANDIR_OPT
D=$DESTDIR
SYSTEM_SHARE=${WARD_SYSTEM_SHARE:-/usr/share}
USER_INSTALL=0
case "$PREFIX" in "$HOME"/*) USER_INSTALL=1 ;; esac

if [ -z "$DESTDIR" ] && [ "$CHECK" -eq 0 ] && [ "$UNINSTALL" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    mkdir -p "$PREFIX" 2>/dev/null || true
    [ -w "$PREFIX" ] || die "no write access to $PREFIX - run as root (sudo sh $0) or install for yourself: sh $0 --prefix ~/.local"
fi

# ------------------------------------------------------------------------------------------- package managers
pm_name() {
    for pm in apt-get dnf zypper pacman xbps-install apk emerge pkgin pkg_add pkg; do
        if have "$pm"; then
            # FreeBSD's pkg; an unrelated "pkg" elsewhere is not a package manager
            if [ "$pm" = pkg ] && [ "$OS" != FreeBSD ] && [ "$OS" != DragonFly ]; then continue; fi
            echo "$pm"; return
        fi
    done
    echo none
}

pyver() {   # py311 style prefix for FreeBSD and NetBSD package names
    v=$(python3 -c 'import sys; print("%d%d" % sys.version_info[:2])' 2>/dev/null || echo 311)
    echo "py$v"
}

# required, then recommended packages for each package manager
packages() {
    case "$(pm_name)" in
        apt-get) REQ="python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-slixmpp python3-markdown-it python3-cryptography"
                 OPT="gir1.2-gtksource-5 gir1.2-spelling-1 gir1.2-secret-1 python3-pil python3-linkify-it python3-mdit-py-plugins python3-pip gnupg libgtk-4-media-gstreamer desktop-file-utils" ;;
        dnf) REQ="python3 python3-gobject gobject-introspection cairo-gobject gtk4 libadwaita python3-slixmpp python3-markdown-it-py python3-cryptography"
             OPT="gtksourceview5 libspelling libsecret python3-pillow python3-linkify-it-py python3-mdit-py-plugins python3-pip gnupg2 gstreamer1-plugins-good desktop-file-utils" ;;
        zypper) REQ="python3 python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 python3-slixmpp python3-markdown-it-py python3-cryptography"
                OPT="typelib-1_0-GtkSource-5 typelib-1_0-Spelling-1 typelib-1_0-Secret-1 python3-Pillow python3-linkify-it-py python3-pip gpg2 desktop-file-utils" ;;
        pacman) REQ="python python-gobject gtk4 libadwaita python-slixmpp python-markdown-it-py python-cryptography"
                OPT="gtksourceview5 libspelling libsecret python-pillow python-linkify-it-py python-mdit_py_plugins python-pip gnupg gst-plugins-good desktop-file-utils" ;;
        xbps-install) REQ="python3 python3-gobject gtk4 libadwaita python3-slixmpp python3-markdown-it-py python3-cryptography"
                      OPT="gtksourceview5 libspelling libsecret python3-Pillow python3-linkify-it-py python3-pip gnupg gst-plugins-good1 desktop-file-utils gtk-update-icon-cache" ;;
        apk) REQ="python3 py3-gobject3 gtk4.0 libadwaita py3-slixmpp py3-markdown-it-py py3-cryptography"
             OPT="gtksourceview5 libspelling libsecret py3-pillow py3-linkify-it-py py3-pip gnupg gst-plugins-good desktop-file-utils" ;;
        emerge) REQ="dev-lang/python dev-python/pygobject gui-libs/gtk gui-libs/libadwaita dev-python/slixmpp dev-python/markdown-it-py dev-python/cryptography"
                OPT="x11-libs/gtksourceview:5 app-crypt/libsecret dev-python/pillow dev-python/linkify-it-py dev-python/pip app-crypt/gnupg" ;;
        pkg) P=$(pyver)
             REQ="python3 $P-gobject3 gtk4 libadwaita $P-slixmpp $P-markdown-it-py $P-cryptography"
             OPT="gtksourceview5 libspelling libsecret $P-pillow $P-linkify-it-py $P-pip gnupg desktop-file-utils" ;;
        pkg_add) REQ="python%3 py3-gobject3 gtk+4 libadwaita py3-slixmpp py3-markdown-it-py py3-cryptography"
                 OPT="gtksourceview5 libsecret py3-Pillow py3-pip gnupg desktop-file-utils" ;;
        pkgin) P=$(pyver)
               REQ="python311 $P-gobject3 gtk4 libadwaita $P-slixmpp $P-markdown-it-py $P-cryptography"
               OPT="gtksourceview5 libsecret $P-Pillow $P-pip gnupg2 desktop-file-utils" ;;
        *) REQ=""; OPT="" ;;
    esac
}

deps_hint() {
    packages
    case "$(pm_name)" in
        apt-get) echo "sudo apt install $REQ" ;;
        dnf) echo "sudo dnf install $REQ" ;;
        zypper) echo "sudo zypper install $REQ" ;;
        pacman) echo "sudo pacman -S $REQ" ;;
        xbps-install) echo "sudo xbps-install -S $REQ" ;;
        apk) echo "doas apk add $REQ" ;;
        emerge) echo "sudo emerge --ask $REQ" ;;
        pkg) echo "sudo pkg install $REQ" ;;
        pkg_add) echo "doas pkg_add $REQ" ;;
        pkgin) echo "sudo pkgin install $REQ" ;;
        *) echo "install Python 3.11+, PyGObject, GTK 4.12+, libadwaita 1.4+, slixmpp, markdown-it-py and cryptography" ;;
    esac
}

# the package manager runs as root: directly when we are root, through sudo or doas otherwise
need_root() {
    [ "$(id -u)" -eq 0 ] && return 0
    for t in sudo doas; do
        if have "$t"; then AS_ROOT=$t; return 0; fi
    done
    return 1
}

install_pkgs() {
    # shellcheck disable=SC2086   # AS_ROOT is empty or one word
    case "$(pm_name)" in
        apt-get) $AS_ROOT env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
        dnf) $AS_ROOT dnf install -y "$@" ;;
        zypper) $AS_ROOT zypper --non-interactive install "$@" ;;
        pacman) $AS_ROOT pacman -S --needed --noconfirm "$@" ;;
        xbps-install) $AS_ROOT xbps-install -y "$@" ;;
        apk) $AS_ROOT apk add "$@" ;;
        emerge) $AS_ROOT emerge --noreplace "$@" ;;
        pkg) $AS_ROOT pkg install -y "$@" ;;
        pkg_add) $AS_ROOT pkg_add -I "$@" ;;
        pkgin) $AS_ROOT pkgin -y install "$@" ;;
        *) return 1 ;;
    esac
}

install_deps() {
    [ "$(pm_name)" != none ] || die "no supported package manager found; $(deps_hint)"
    need_root || die "installing packages needs root: run sudo sh $0, or install them yourself: $(deps_hint)"
    say "Installing dependencies with ${AS_ROOT:+$AS_ROOT }$(pm_name)"
    # shellcheck disable=SC2086
    [ "$(pm_name)" = apt-get ] && { $AS_ROOT apt-get update || warn "apt-get update failed"; }
    # shellcheck disable=SC2086
    [ "$(pm_name)" = xbps-install ] && { $AS_ROOT xbps-install -S >/dev/null 2>&1 || true; }
    packages
    # all at once; if the package manager refuses (one name it doesn't know makes most refuse everything), one by
    # one, so the rest still get installed and the warning names exactly what is missing
    # shellcheck disable=SC2086
    if ! install_pkgs $REQ; then
        for p in $REQ; do install_pkgs "$p" || warn "required package not available here: $p"; done
    fi
    # one at a time: a recommended package missing from this release must not stop the others
    for p in $OPT; do install_pkgs "$p" >/dev/null 2>&1 || warn "optional package not available: $p"; done
}

# ------------------------------------------------------------------------------------------- Python
py_ok() {   # py_ok <python> : 3.11+ with GTK 4 and libadwaita bindings
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PY) else 1)" 2>/dev/null || return 1
    "$1" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null
}

find_python() {   # the first Python 3.11+ that has GTK 4 and libadwaita (falls back to any Python 3.11+)
    first=""
    for c in python3 python3.14 python3.13 python3.12 python3.11 python; do
        have "$c" || continue
        "$c" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PY) else 1)" 2>/dev/null || continue
        [ -n "$first" ] || first=$(command -v "$c")
        if py_ok "$c"; then command -v "$c"; return 0; fi
    done
    [ -n "$first" ] && { echo "$first"; return 0; }
    return 1
}

# what the system has, or a previous install fetched into Ward's own folder
module() { PYTHONPATH="$LIBDIR/vendor${PYTHONPATH:+:$PYTHONPATH}" "$PY" -c "import $1" 2>/dev/null; }

typelib() { "$PY" -c "import gi; gi.require_version('$1', '$2')" 2>/dev/null; }

# missing_deps: what this machine lacks, without stopping at the first gap.  Sets MISSING_SYS (things the package
# manager installs), MISSING_OMEMO (OMEMO libraries, fetched with pip) and MISSING (a list to show)
missing_deps() {
    MISSING_SYS=0; MISSING_OMEMO=0; MISSING=""
    gap() { MISSING="$MISSING
    - $1"; }
    if ! PY=$(find_python); then
        PY=""
        MISSING_SYS=1; gap "Python 3.$MIN_PY or newer, with GTK 4 and libadwaita (required)"
    else
        py_ok "$PY" || { MISSING_SYS=1; gap "PyGObject with GTK 4 and libadwaita (required)"; }
        for m in slixmpp:slixmpp markdown_it:markdown-it-py cryptography:cryptography; do
            module "${m%%:*}" || { MISSING_SYS=1; gap "${m#*:} (required)"; }
        done
        typelib GtkSource 5 || { MISSING_SYS=1; gap "GtkSourceView 5 (code blocks)"; }
        typelib Spelling 1 || { MISSING_SYS=1; gap "libspelling (spell checking)"; }
        typelib Secret 1 || { MISSING_SYS=1; gap "libsecret (passwords in the keyring)"; }
        module PIL || { MISSING_SYS=1; gap "Pillow (image previews)"; }
        module linkify_it || { MISSING_SYS=1; gap "linkify-it-py (links in messages)"; }
        "$PY" -m pip --version >/dev/null 2>&1 || { MISSING_SYS=1; gap "pip (to fetch the OMEMO libraries)"; }
        omemo="omemo twomemo.etree xeddsa x3dh doubleratchet"
        [ "$LEGACY" -eq 1 ] && omemo="$omemo oldmemo.etree"
        for m in $omemo; do
            module "$m" || MISSING_OMEMO=1
        done
        if [ "$MISSING_OMEMO" -eq 1 ]; then
            if [ "$LEGACY" -eq 1 ]; then
                gap "OMEMO encryption libraries, with legacy OMEMO (fetched with pip)"
            else
                gap "OMEMO encryption libraries (fetched with pip)"
            fi
        fi
    fi
    have gpg || have gpg2 || { MISSING_SYS=1; gap "GnuPG (OpenPGP encryption, optional)"; }
}

# Before installing: say what is missing and, in a terminal (or with --yes), offer to install it
offer_deps() {
    [ "$ASK" != no ] && [ -z "$DESTDIR" ] && [ "$DEPS" -eq 0 ] || return 0
    [ "$ASK" = yes ] || [ -t 0 ] || return 0
    say "Checking what Ward needs"
    missing_deps
    if [ -z "$MISSING" ]; then
        say "Every dependency is installed"
        return 0
    fi
    printf '
This machine is missing:%s

' "$MISSING"
    if [ "$ASK" = ask ]; then
        printf 'Install them now? [Y/n] '
        read -r answer || answer=n
        case "$answer" in
            ""|[Yy]*) ;;
            *) say "Not installing dependencies (install them later with: sh $0 --yes)"; return 0 ;;
        esac
    fi
    if [ "$MISSING_SYS" -eq 1 ]; then
        if [ "$(pm_name)" = none ]; then
            warn "no supported package manager found; $(deps_hint)"
        elif ! need_root; then
            warn "installing packages needs root (sudo sh $0), or install them yourself: $(deps_hint)"
        else
            install_deps
        fi
    fi
    # the OMEMO libraries (and anything required the distribution doesn't package) with pip, into Ward's folder
    VENDOR=1
}

preflight() {
    PY=$(find_python) || die "Python 3.$MIN_PY or newer is required. $(deps_hint)"
    say "Python: $PY ($("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"
    if py_ok "$PY"; then
        ver=$("$PY" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw; print('%d.%d %d.%d' % (Gtk.get_major_version(), Gtk.get_minor_version(), Adw.get_major_version(), Adw.get_minor_version()))")
        gtk=${ver% *}; adw=${ver#* }
        say "GTK $gtk, libadwaita $adw"
        "$PY" -c "from gi.repository import Gtk, Adw; import sys; sys.exit(0 if (Gtk.get_minor_version() >= 12 and Adw.get_minor_version() >= 4) else 1)" 2>/dev/null ||
            warn "Ward needs GTK 4.12 and libadwaita 1.4 or newer (found $gtk and $adw)"
    else
        warn "PyGObject with GTK 4 and libadwaita is missing. $(deps_hint)"
    fi
    for m in slixmpp markdown_it cryptography; do
        module "$m" || warn "the Python module '$m' is missing. $(deps_hint)   (or: --vendor)"
    done
    if module omemo && module twomemo && module xeddsa; then
        say "OMEMO encryption: available"
    elif [ "$VENDOR" -eq 1 ]; then
        say "OMEMO encryption: fetching it with pip"
    else
        say "OMEMO encryption: not installed yet - Ward offers to install it on first use (or: $0 --vendor)"
    fi
    have gpg || have gpg2 || say "OpenPGP: gpg not found (optional)"
}

vendor() {   # fetch modules the system lacks with pip into LIBDIR/vendor (VENDOR=2: only what Ward needs to start)
    want=""
    module slixmpp || want="$want slixmpp>=1.8"
    module markdown_it || want="$want markdown-it-py>=2"
    module cryptography || want="$want cryptography"
    if [ "$VENDOR" -eq 1 ]; then
        module mdit_py_plugins || want="$want mdit-py-plugins"
        module linkify_it || want="$want linkify-it-py"
        for m in omemo x3dh doubleratchet xeddsa; do module "$m" || want="$want $m>=1.0"; done
        module twomemo.etree || want="$want twomemo[xml]>=1.0"
        if [ "$LEGACY" -eq 1 ]; then module oldmemo.etree || want="$want oldmemo[xml]>=1.0"; fi
    fi
    if [ -z "$want" ]; then say "Nothing to vendor: every library is installed"; return 0; fi
    "$PY" -m pip --version >/dev/null 2>&1 || { warn "pip is missing, cannot fetch:$want"; return 0; }
    say "Fetching with pip:$want"
    mkdir -p "$D$LIBDIR/vendor"
    # shellcheck disable=SC2086
    # (installing for every user as root is what we mean: pip need not warn about it; older pips ignore the variable)
    PIP_ROOT_USER_ACTION=ignore "$PY" -m pip install --upgrade --target "$D$LIBDIR/vendor" --disable-pip-version-check --no-warn-script-location \
        $want || warn "pip could not install everything ($want); Ward still runs, with fewer features"
}

wrapper() {  # wrapper <path>
    out=$1
    mkdir -p "$(dirname "$out")"
    cat > "$out" <<WRAP
#!/bin/sh
# generated by Ward's install.sh
# Desktop launchers can run us with another PATH than a terminal (pyenv, conda, ~/.local/bin...), so pick a
# Python by what it can import instead of by name.
usable() {
    [ -x "\$1" ] || return 1
    "\$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, $MIN_PY) else 1)' 2>/dev/null || return 1
    "\$1" -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null
}
PY=\${WARD_PYTHON:-}
if [ -z "\$PY" ]; then
    FIRST=
    for c in "${PY:-}" /usr/bin/python3 /usr/local/bin/python3 /usr/pkg/bin/python3 python3 python3.14 python3.13 \\
             python3.12 python3.11 /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/pkg/bin/python3.12 \\
             /usr/pkg/bin/python3.11 python; do
        [ -n "\$c" ] || continue
        case "\$c" in /*) ;; *) c=\$(command -v "\$c" 2>/dev/null) || continue ;; esac
        [ -x "\$c" ] || continue
        [ -n "\$FIRST" ] || FIRST=\$c
        if usable "\$c"; then PY=\$c; break; fi
    done
    PY=\${PY:-\$FIRST}
fi
[ -n "\$PY" ] || { echo "ward: no Python 3 found (install python3)" >&2; exit 127; }
# never write bytecode into the (root-owned) install tree
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}"
export WARD_SHARE_DIR="$SHAREDIR/ward"
[ -d "$LIBDIR/vendor" ] && export WARD_VENDOR="$LIBDIR/vendor"
exec "\$PY" -m ward "\$@"
WRAP
    chmod 755 "$out"
}

compile_translations() {
    [ -d "$SRC/po" ] || return 0
    for po in "$SRC"/po/*.po; do
        [ -f "$po" ] || continue
        lang=$(basename "$po" .po)
        dest="$D$SHAREDIR/locale/$lang/LC_MESSAGES"
        mkdir -p "$dest"
        if have msgfmt; then
            msgfmt -o "$dest/ward.mo" "$po" || warn "could not compile $po"
        elif [ -n "${PY:-}" ] || PY=$(find_python); then
            "$PY" "$SRC/packaging/msgfmt.py" "$po" "$dest/ward.mo" || warn "could not compile $po"
        fi
    done
}

refresh_caches() {
    for theme in "$SHAREDIR/icons/hicolor" "$SYSTEM_SHARE/icons/hicolor"; do
        [ -d "$theme" ] || continue
        for t in gtk4-update-icon-cache gtk-update-icon-cache; do
            if have "$t"; then "$t" -q -f -t "$theme" 2>/dev/null && break; fi
        done
    done
    for d in "$SHAREDIR/applications" "$SYSTEM_SHARE/applications"; do
        if have update-desktop-database && [ -d "$d" ]; then update-desktop-database -q "$d" 2>/dev/null || true; fi
    done
    return 0
}

# Sessions that set XDG_DATA_DIRS themselves often leave /usr/local/share out, and the launcher and the xmpp: link
# handler then never show up.  A plain copy in /usr/share fixes that (Linux only: the BSDs use /usr/local).
link_system_dirs() {
    [ "$SYSLINKS" -eq 1 ] && [ "$USER_INSTALL" -eq 0 ] && [ "$OS" = Linux ] && [ "$SHAREDIR" != "$SYSTEM_SHARE" ] || return 0
    [ -d "$SYSTEM_SHARE" ] && [ -w "$SYSTEM_SHARE" ] || return 0
    for rel in "applications/$APPID.desktop" "metainfo/$APPID.metainfo.xml"; do
        mkdir -p "$(dirname "$SYSTEM_SHARE/$rel")" && cp "$SHAREDIR/$rel" "$SYSTEM_SHARE/$rel" 2>/dev/null || true
    done
    for f in "$SHAREDIR"/icons/hicolor/*/apps/"$APPID"*; do
        [ -f "$f" ] || continue
        rel=${f#"$SHAREDIR"/}
        mkdir -p "$(dirname "$SYSTEM_SHARE/$rel")" && cp "$f" "$SYSTEM_SHARE/$rel" 2>/dev/null || true
    done
    say "Copied the launcher and icons into $SYSTEM_SHARE (visible to every desktop session)"
}

uninstall() {
    say "Removing Ward from $PREFIX"
    rm -rf "$D$LIBDIR" "$D$SHAREDIR/ward"
    rm -f "$D$BINDIR/ward" "$D$PREFIX/lib/systemd/user/ward.service"
    rm -f "$D$SHAREDIR/applications/$APPID.desktop" "$D$SHAREDIR/metainfo/$APPID.metainfo.xml" \
          "$D$SHAREDIR/pixmaps/$APPID.svg" "$D$MANDIR/ward.1" "$D$SHAREDIR/bash-completion/completions/ward" \
          "$D$SHAREDIR/zsh/site-functions/_ward" "$D$SHAREDIR/fish/vendor_completions.d/ward.fish"
    rm -f "$D$SHAREDIR"/icons/hicolor/*/apps/"$APPID"*
    rm -f "$D$SHAREDIR"/locale/*/LC_MESSAGES/ward.mo
    if [ -z "$DESTDIR" ] && [ "$USER_INSTALL" -eq 0 ] && [ "$SHAREDIR" != "$SYSTEM_SHARE" ]; then
        rm -f "$SYSTEM_SHARE/applications/$APPID.desktop" "$SYSTEM_SHARE/metainfo/$APPID.metainfo.xml" \
              "$SYSTEM_SHARE"/icons/hicolor/*/apps/"$APPID"*
    fi
    [ -z "$DESTDIR" ] && refresh_caches
    say "Removed. Your chats and settings are kept in ~/.config/ward and ~/.local/share/ward."
    exit 0
}

# ------------------------------------------------------------------------------------------- main
if [ "$UNINSTALL" -eq 1 ]; then uninstall; fi
if [ "$CHECK" -eq 1 ]; then
    say "Checking this machine ($OS, package manager: $(pm_name))"
    preflight
    if [ -z "$WARNINGS" ]; then say "Everything Ward needs is installed."; else printf '\nTo fix:%s\n' "$WARNINGS"; fi
    exit 0
fi
[ -d "$SRC/ward" ] && [ -d "$SRC/data" ] || die "run install.sh from the Ward source folder (ward/ and data/ must be next to it)"
if [ "$DEPS" -eq 1 ]; then install_deps; fi
offer_deps
PY=""
if [ -z "$DESTDIR" ] || [ "$VENDOR" -eq 1 ]; then preflight; fi
# --install-deps: Python libraries the distribution doesn't package are fetched with pip into Ward's own folder
if [ "$DEPS" -eq 1 ] && [ "$VENDOR" -eq 0 ] && [ -n "$PY" ]; then
    for m in slixmpp markdown_it cryptography; do module "$m" || VENDOR=2; done
fi

say "Installing Ward to $D$PREFIX"
keep_vendor=""
if [ -d "$D$LIBDIR/vendor" ]; then      # libraries fetched before: kept, vendor() only adds what is missing
    keep_vendor=$(mktemp -d "${TMPDIR:-/tmp}/ward-vendor.XXXXXX")
    mv "$D$LIBDIR/vendor" "$keep_vendor/"
fi
rm -rf "$D$LIBDIR"
mkdir -p "$D$LIBDIR"
if [ -n "$keep_vendor" ]; then mv "$keep_vendor/vendor" "$D$LIBDIR/"; rmdir "$keep_vendor"; fi
cp -R "$SRC/ward" "$D$LIBDIR/ward"
find "$D$LIBDIR/ward" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
if [ "$VENDOR" -ge 1 ]; then vendor; fi

wrapper "$D$BINDIR/ward"

mkdir -p "$D$SHAREDIR/applications" "$D$SHAREDIR/metainfo" "$D$MANDIR" "$D$SHAREDIR/pixmaps" "$D$SHAREDIR/ward"
sed "s|^Exec=ward|Exec=$BINDIR/ward|" "$SRC/data/$APPID.desktop" > "$D$SHAREDIR/applications/$APPID.desktop"
cp "$SRC/data/$APPID.metainfo.xml" "$D$SHAREDIR/metainfo/"
for dir in "$SRC"/data/icons/hicolor/*/apps; do
    size=$(basename "$(dirname "$dir")")
    mkdir -p "$D$SHAREDIR/icons/hicolor/$size/apps"
    cp "$dir"/* "$D$SHAREDIR/icons/hicolor/$size/apps/"
done
cp "$SRC/data/icons/hicolor/scalable/apps/$APPID.svg" "$D$SHAREDIR/pixmaps/"
# a private copy that does not depend on the system icon cache being fresh
cp -R "$SRC/data/icons" "$D$SHAREDIR/ward/"
# the notification sound, for systems without the freedesktop sound theme (Void, minimal installs); Ward plays the
# theme's own sound when there is one
cp -R "$SRC/data/sounds" "$D$SHAREDIR/ward/"
cp "$SRC/data/ward.1" "$D$MANDIR/ward.1"
mkdir -p "$D$SHAREDIR/bash-completion/completions" "$D$SHAREDIR/zsh/site-functions" "$D$SHAREDIR/fish/vendor_completions.d"
cp "$SRC/data/completions/ward.bash" "$D$SHAREDIR/bash-completion/completions/ward"
cp "$SRC/data/completions/_ward" "$D$SHAREDIR/zsh/site-functions/_ward"
cp "$SRC/data/completions/ward.fish" "$D$SHAREDIR/fish/vendor_completions.d/ward.fish"
compile_translations
# optional background services; Ward needs neither (its "Start at login" uses XDG autostart, which every init
# system's desktop session honours)
render() {  # render <template> <dest> <mode>
    mkdir -p "$(dirname "$D$2")"
    sed -e "s|@BINDIR@|$BINDIR|g" -e "s|@SHAREDIR@|$SHAREDIR|g" "$1" > "$D$2"
    chmod "$3" "$D$2"
}
case "$OS:${WARD_NO_SYSTEMD:-}" in
    Linux:) render "$SRC/data/init/ward.service" "$PREFIX/lib/systemd/user/ward.service" 644 ;;
esac
render "$SRC/data/init/runit/run" "$SHAREDIR/ward/runit/run" 755
render "$SRC/data/init/runit/log/run" "$SHAREDIR/ward/runit/log/run" 755

# cp keeps the source modes: a tarball unpacked with a strict umask must still install world-readable files
chmod -R go+rX "$D$LIBDIR" "$D$SHAREDIR/ward"
chmod 755 "$D$SHAREDIR/ward/runit/run" "$D$SHAREDIR/ward/runit/log/run"
find "$D$SHAREDIR" \( -name "$APPID*" -o -name 'ward.1' -o -name 'ward' -o -name '_ward' -o -name 'ward.fish' \
    -o -name 'ward.mo' \) -type f -exec chmod 644 {} + 2>/dev/null || true
chmod 755 "$D$BINDIR/ward"
if [ -z "$DESTDIR" ] && [ -n "$PY" ]; then
    "$PY" -m compileall -q --invalidation-mode checked-hash "$D$LIBDIR/ward" >/dev/null 2>&1 || true
fi

if [ -z "$DESTDIR" ] && [ "$DO_POST" -eq 1 ]; then
    link_system_dirs
    refresh_caches
    if have desktop-file-validate; then
        desktop-file-validate "$SHAREDIR/applications/$APPID.desktop" >&2 2>&1 || warn "the launcher failed desktop-file-validate"
    fi
    case ":${XDG_DATA_DIRS:-/usr/local/share:/usr/share}:" in
        *":$SHAREDIR:"*) ;;
        *) [ "$USER_INSTALL" -eq 1 ] || warn "$SHAREDIR is not in XDG_DATA_DIRS; if Ward is missing from your menu, reinstall with --prefix /usr" ;;
    esac
    case ":$PATH:" in
        *":$BINDIR:"*) ;;
        *) warn "$BINDIR is not in your PATH; add it to use the ward command" ;;
    esac
fi

if [ -n "$WARNINGS" ]; then
    printf '\nWard is installed, with warnings:%s\n' "$WARNINGS"
else
    printf '\nWard is installed.\n'
fi
cat <<DONE

  Start it from your application menu, or run:  ward
  Check this machine any time:                  ward --check
DONE
