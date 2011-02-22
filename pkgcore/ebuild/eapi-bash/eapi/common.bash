# Copyright 2011 Brian Harring <ferringb@gmail.com>
# license GPL2/BSD 3

use()
{
    if useq ${1}; then
        return 0
    fi
    return 1
}

use_with()
{
    if [ -z "$1" ]; then
        echo "!!! use_with() called without a parameter." >&2
        echo "!!! use_with <USEFLAG> [<flagname> [value]]" >&2
        return
    fi

    local UW_SUFFIX=""
	if has "${EAPI:-0}" 0 1 2 3; then
		UW_SUFFIX=${3:+=$3}
	else
		UW_SUFFIX=${3+=$3}
	fi

    local UWORD="$2"
    if [ -z "${UWORD}" ]; then
        UWORD="$1"
    fi

    if useq $1; then
        echo "--with-${UWORD}${UW_SUFFIX}"
        return 0
    else
        echo "--without-${UWORD}"
        return 1
    fi
}

use_enable()
{
    if [ -z "$1" ]; then
        echo "!!! use_enable() called without a parameter." >&2
        echo "!!! use_enable <USEFLAG> [<flagname> [value]]" >&2
        return
    fi

    local UE_SUFFIX=""
	if has "${EAPI:-0}" 0 1 2 3; then
		UE_SUFFIX=${3:+=$3}
	else
		UE_SUFFIX=${3+=$3}
	fi

    local UWORD="$2"
    if [ -z "${UWORD}" ]; then
        UWORD="$1"
    fi

    if useq $1; then
        echo "--enable-${UWORD}${UE_SUFFIX}"
        return 0
    else
        echo "--disable-${UWORD}"
        return 1
    fi
}

econf()
{
    local ret
    ECONF_SOURCE="${ECONF_SOURCE:-.}"
    if [ ! -x "${ECONF_SOURCE}/configure" ]; then
        [ -f "${ECONF_SOURCE}/configure" ] && die "configure script isn't executable"
        die "no configure script found"
    fi

    if [[ -d /usr/share/gnuconfig ]]; then
        local x
        find "${WORKDIR}" -type f \( -name config.guess -o -name config.sub \) | \
            while read x; do
            echo "econf: replacing ${x} with /usr/share/gnuconfig/${x##*/}"
            cp -f "/usr/share/gnuconfig/${x##*/}" "$x"
        done
    fi

    if [ ! -z "${CBUILD}" ]; then
        EXTRA_ECONF="--build=${CBUILD} ${EXTRA_ECONF}"
    fi

    # if the profile defines a location to install libs to aside from default, pass it on.
    # if the ebuild passes in --libdir, they're responsible for the conf_libdir fun.
    LIBDIR_VAR="LIBDIR_${ABI}"
    if [ -n "${ABI}" -a -n "${!LIBDIR_VAR}" ]; then
        CONF_LIBDIR="${!LIBDIR_VAR}"
    fi
    unset LIBDIR_VAR
    if [ -n "${CONF_LIBDIR}" ] && [ "${*/--libdir}" == "$*" ]; then
        if [ "${*/--exec-prefix}" != "$*" ]; then
            local args="$(echo $*)"
            local -a pref=($(echo ${args/*--exec-prefix[= ]}))
            CONF_PREFIX=${pref}
            [ "${CONF_PREFIX:0:1}" != "/" ] && CONF_PREFIX="/${CONF_PREFIX}"
        elif [ "${*/--prefix}" != "$*" ]; then
            local args="$(echo $*)"
 			local -a pref=($(echo ${args/*--prefix[= ]}))
 			CONF_PREFIX=${pref}
            [ "${CONF_PREFIX:0:1}" != "/" ] && CONF_PREFIX="/${CONF_PREFIX}"
        else
            CONF_PREFIX="/usr"
 	    fi
 		export CONF_PREFIX
        [ "${CONF_LIBDIR:0:1}" != "/" ] && CONF_LIBDIR="/${CONF_LIBDIR}"

        CONF_LIBDIR_RESULT="${CONF_PREFIX}${CONF_LIBDIR}"
        for X in 1 2 3; do
            # The escaping is weird. It will break if you escape the last one.
            CONF_LIBDIR_RESULT="${CONF_LIBDIR_RESULT//\/\///}"
        done

        EXTRA_ECONF="--libdir=${CONF_LIBDIR_RESULT} ${EXTRA_ECONF}"
    fi
    local EECONF_CACHE
    echo ${ECONF_SOURCE}/configure \
        --prefix=/usr \
        --host=${CHOST} \
        --mandir=/usr/share/man \
        --infodir=/usr/share/info \
        --datadir=/usr/share \
        --sysconfdir=/etc \
        --localstatedir=/var/lib \
        ${EXTRA_ECONF} \
        ${EECONF_CACHE} \
        "$@"

    if ! ${ECONF_SOURCE}/configure \
        --prefix=/usr \
        --host=${CHOST} \
        --mandir=/usr/share/man \
        --infodir=/usr/share/info \
        --datadir=/usr/share \
        --sysconfdir=/etc \
        --localstatedir=/var/lib \
        ${EXTRA_ECONF} \
        ${EECONF_CACHE} \
        "$@" ; then

        if [ -s config.log ]; then
            echo
            echo "!!! Please attach the config.log to your bug report:"
            echo "!!! ${PWD}/config.log"
        fi
        die "econf failed"
    fi
    return $?
}

strip_duplicate_slashes ()
{
    if [ -n "${1}" ]; then
        local removed="${1/\/\///}"
        [ "${removed}" != "${removed/\/\///}" ] && removed=$(strip_duplicate_slashes "${removed}")
        echo ${removed}
    fi
}

einstall()
{
    # CONF_PREFIX is only set if they didn't pass in libdir above
    local LOCAL_EXTRA_EINSTALL="${EXTRA_EINSTALL}"
    LIBDIR_VAR="LIBDIR_${ABI}"
    if [ -n "${ABI}" -a -n "${!LIBDIR_VAR}" ]; then
        CONF_LIBDIR="${!LIBDIR_VAR}"
    fi
    unset LIBDIR_VAR
    if [ -n "${CONF_LIBDIR}" ] && [ "${CONF_PREFIX:-unset}" != "unset" ]; then
        EI_DESTLIBDIR="${D}/${CONF_PREFIX}/${CONF_LIBDIR}"
        EI_DESTLIBDIR="$(strip_duplicate_slashes ${EI_DESTLIBDIR})"
        LOCAL_EXTRA_EINSTALL="${LOCAL_EXTRA_EINSTALL} libdir=${EI_DESTLIBDIR}"
        unset EI_DESTLIBDIR
    fi

    if [ -f ./[mM]akefile -o -f ./GNUmakefile ] ; then
        if [ ! -z "${PKGCORE_DEBUG}" ]; then
            ${MAKE:-make} -n prefix=${D}/usr \
                datadir=${D}/usr/share \
                infodir=${D}/usr/share/info \
          		localstatedir=${D}/var/lib \
                mandir=${D}/usr/share/man \
                sysconfdir=${D}/etc \
                ${LOCAL_EXTRA_EINSTALL} \
                "$@" install
        fi
        ${MAKE:-make} prefix=${D}/usr \
            datadir=${D}/usr/share \
            infodir=${D}/usr/share/info \
            localstatedir=${D}/var/lib \
            mandir=${D}/usr/share/man \
            sysconfdir=${D}/etc \
            ${LOCAL_EXTRA_EINSTALL} \
            "$@" install || die "einstall failed"
    else
        die "no Makefile found"
    fi
}

pkgcore_common_pkg_nofetch()
{
    [ -z "${SRC_URI}" ] && return

    echo "!!! The following are listed in SRC_URI for ${PN}:"
    for MYFILE in `echo ${SRC_URI}`; do
        echo "!!!   $MYFILE"
    done
}

pkgcore_common_src_unpack()
{
    if [ "${A}" != "" ]; then
        unpack ${A}
    fi
}

pkgcore_common_src_compile()
{
    # only eapi 0/1 invoke configure...
    if has "${EAPI:-0}" 0 1; then
        if [ "${EAPI:-0}" == 0 ] ; then
            [ -x ./configure ] && econf
        elif [ -x "${ECONF_SOURCE:-.}/configure" ]; then
            econf
        fi
    fi
    if [ -f Makefile ] || [ -f GNUmakefile ] || [ -f makefile ]; then
        emake || die "emake failed"
    fi
}

pkgcore_common_src_test()
{
    addpredict /
    if make check -n &> /dev/null; then
        echo ">>> Test phase [check]: ${CATEGORY}/${PF}"
        emake -j1 check || die "Make check failed. See above for details."
    elif make test -n &> /dev/null; then
        make test || die "Make test failed. See above for details."
    else
        echo ">>> Test phase [none]: ${CATEGORY}/${PF}"
    fi
    SANDBOX_PREDICT="${SANDBOX_PREDICT%:/}"
}

into()
{
    if [ $1 == "/" ]; then
        export DESTTREE=""
    else
        export DESTTREE=$1
        if [ ! -d "${D}${DESTTREE}" ]; then
            install -d "${D}${DESTTREE}"
        fi
    fi
}

insinto()
{
    if [ "$1" == "/" ]; then
        export INSDESTTREE=""
    else
        export INSDESTTREE=$1
        if [ ! -d "${D}${INSDESTTREE}" ]; then
            install -d "${D}${INSDESTTREE}"
        fi
    fi
}

exeinto()
{
    if [ "$1" == "/" ]; then
        export EXEDESTTREE=""
    else
        export EXEDESTTREE="$1"
        if [ ! -d "${D}${EXEDESTTREE}" ]; then
            install -d "${D}${EXEDESTTREE}"
        fi
    fi
}

docinto()
{
    if [ "$1" == "/" ]; then
        export DOCDESTTREE=""
    else
        export DOCDESTTREE="$1"
        if [ ! -d "${D}usr/share/doc/${PF}/${DOCDESTTREE}" ]; then
            install -d "${D}usr/share/doc/${PF}/${DOCDESTTREE}"
        fi
    fi
}

pkgcore_common_pre_phase()
{
	if [ -d "${S}" ]; then
		cd "${S}"
	else
		cd "$WORKDIR"
	fi
}

pkgcore_default_pre_src_unpack()
{
	export S
	cd "${WORKDIR}"
}

pkgcore_default_pre_src_prepare() { pkgcore_common_pre_phase; }
pkgcore_default_pre_src_test()    { pkgcore_common_pre_phase; }

pkgcore_default_pre_src_configure()
{
	for x in C{BUILD,HOST,TARGET,C,XX} {AS,LD,{,LIB}C{,XX}}FLAGS; do
		[[ -z ${!x} ]] && export ${x}="${!x}"
	done
	# this is/was used by libtool bits to filter bad pathways out
	# likely dead, but keep it around for the time being.
	export PWORKDIR=${WORKDIR}
	pkgcore_common_pre_phase
}

pkgcore_default_pre_src_compile()
{
	# just reuse the default_pre_src_configure; this means we don't have to care
	# if the eapi has configure or not.
	pkgcore_default_pre_src_configure

	[[ -n ${DISTCC_DIR} ]] && addwrite "${DISTCC_DIR}"
}

pkgcore_default_pre_src_install()
{
	local x
	export DESTTREE=/usr INSDESTTREE='' EXEDESTTREE='' DOCDESTTREE=''
	export INSOPTIONS="-m0644" EXEOPTIONS="-m0755"
	export LIBOPTIONS="-m0644" DIROPTIONS="-m0755"
	export MOPREFIX=${PN}
	export D
	rm -rf "${D}"
	mkdir "${D}"
	pkgcore_common_pre_phase
}

pkgcore_inject_phase_funcs()
{
    local pref=$1
    shift
    while [ -n "$1" ]; do
        if ! is_function "$1"; then
            eval "${1}() { ${pref}_${1}; }";
        fi
        shift
    done
}

pkgcore_inject_common_phase_funcs()
{
    pkgcore_inject_phase_funcs pkgcore_common pkg_nofetch src_{unpack,compile,test}
}

DONT_EXPORT_FUNCS="${DONT_EXPORT_FUNCS} pkgcore_inject_phase_funcs pkgcore_inject_common_phase_funcs"
true
