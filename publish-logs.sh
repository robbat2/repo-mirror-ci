#!/bin/bash

set -e -x

bindir=~/bin
datadir=~/bin
target=~/git/repo-qa-check-results
logdir=${1}
date=${logdir##*/}

[[ -d ${logdir} ]]

rm -f "${target}"/*
cp "${logdir}"/* "${target}"/
cp "${datadir}"/repo-status.css "${target}"/

cd "${target}"
"${bindir}"/summary2html.py > index.html
git add *
git commit -a -m "${date}"
git push