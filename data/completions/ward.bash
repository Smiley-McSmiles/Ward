# bash completion for ward
_ward() {
    local cur=${COMP_WORDS[COMP_CWORD]}
    if [[ $cur == -* ]]; then
        COMPREPLY=($(compgen -W "--background --quit --check --debug --version --help" -- "$cur"))
    elif [[ $cur == x* ]]; then
        COMPREPLY=($(compgen -W "xmpp:" -- "$cur"))
        compopt -o nospace 2>/dev/null
    fi
}
complete -F _ward ward
