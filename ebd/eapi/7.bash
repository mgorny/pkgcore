# Copyright: 2016-2018 Tim Harder <radhermit@gmail.com>
# license GPL2/BSD 3

source "${PKGCORE_EBD_PATH}"/eapi/6.bash
source "${PKGCORE_EBD_PATH}"/eapi/7-ver-funcs.bash

PKGCORE_BANNED_FUNCS+=( libopts )

: