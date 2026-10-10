#!/bin/zsh
cd "${0:A:h}" || exit 1
python3 deployment/update.py
result=$?
printf '\n'
if (( result != 0 )); then
  printf 'Update nicht installiert. Bitte die Meldung oben beachten.\n'
fi
printf 'Enter zum Schließen …'
read -r
exit "$result"
