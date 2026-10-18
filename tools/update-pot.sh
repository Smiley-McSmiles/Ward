#!/bin/sh
# Regenerate po/ward.pot from the sources and merge it into every translation (needs gettext's xgettext).
set -eu
cd "$(dirname "$0")/.."
find ward -name '*.py' | sort > po/POTFILES
xgettext --from-code=UTF-8 --language=Python --keyword=_ --keyword=N_ --keyword=ngettext:1,2 \
    --keyword=pgettext:1c,2 --add-comments=TRANSLATORS --package-name=Ward \
    --package-version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ward/__init__.py)" \
    --msgid-bugs-address=https://github.com/smiley-mcsmiles/ward/issues \
    --files-from=po/POTFILES --output=po/ward.pot
for po in po/*.po; do
    [ -f "$po" ] || continue
    msgmerge --quiet --update --backup=none "$po" po/ward.pot
done
echo "po/ward.pot: $(grep -c '^msgid ' po/ward.pot) strings"
