# Copyright: 2006 Brian Harring <ferringb@gmail.com>
# License: GPL2

import os, stat
from pkgcore.util.osutils import listdir, ensure_dirs
from pkgcore.restrictions import packages, values
from pkgcore.ebuild.atom import atom
from pkgcore.package.errors import InvalidDependency
from pkgcore.os_data import portage_gid
from pkgcore.util.lists import iflatten_instance
from pkgcore.repository import virtual
from pkgcore.util.currying import partial
from pkgcore.util.file import read_dict, AtomicWriteFile
from pkgcore.util.demandload import demandload
demandload(globals(), "errno")

# generic functions.

def _collect_virtuals(virtuals, iterable):
    for pkg in iterable:
        for virtualpkg in iflatten_instance(
            pkg.provides.evaluate_depset(pkg.use)):
            virtuals.setdefault(virtualpkg.package, {}).setdefault(
                pkg.fullver, []).append(pkg.unversioned_atom)

def _finalize_virtuals(virtuals):
    for pkg_dict in virtuals.itervalues():
        for full_ver, rdep_atoms in pkg_dict.iteritems():
            if len(rdep_atoms) == 1:
                pkg_dict[full_ver] = rdep_atoms[0]
            else:
                pkg_dict[full_ver] = \
                    packages.OrRestriction(finalize=True, *rdep_atoms)

# noncaching...

def _grab_virtuals(repo):
    virtuals = {}
    _collect_virtuals(virtuals, repo)
    _finalize_virtuals(virtuals)
    return virtuals

def non_caching_virtuals(repo, livefs=True):
    return virtual.tree(partial(_grab_virtuals, repo), livefs=livefs)


#caching

def _get_mtimes(loc):
    d = {}
    pjoin = os.path.join
    sdir = stat.S_ISDIR
    for x in listdir(loc):
        st = os.stat(pjoin(loc, x))
        if sdir(st.st_mode):
            d[x] = st.st_mtime
    return d

def _write_mtime_cache(mtimes, data, location):
    old = os.umask(0664)
    try:
        if not ensure_dirs(os.path.dirname(location),
            gid=portage_gid, mode=0775):
            # bugger, can't update..
            return
        f = AtomicWriteFile(location)
        # invert the data...
        rev_data = {}
        for pkg, ver_dict in data.iteritems():
            for fullver, virtual in ver_dict.iteritems():
                rev_data.setdefault(virtual.category, []).extend(
                    virtual.package, fullver, str(virtual))
        for cat, mtime in mtimes.iteritems():
            if cat in rev_data:
                f.write("%s\t%i\t%s\n" % (cat, mtime,
                     '\t'.join(rev_data[cat])))
            else:
                f.write("%s\t%i\n" % (cat, mtime))
        f.close()
        del f
    finally:
        os.umask(old)
    os.chown(location, -1, portage_gid)

def _read_mtime_cache(location):
    f = None
    try:
        d = {}
        for k,v in read_dict(open(location, 'r'), 
            source_isiter=True).iteritems():
            v = v.split()
            # mtime pkg1 fullver1 virtual1 pkg2 fullver2 virtual2...
            # if it's not the right length, skip this entry, 
            # cache validation will update it.
            if len(v) -1 % 3 == 0:
                d[k] = v
        return d  
    except OSError, e:
        if e.errno != errno.ENOENT:
            raise
        return {}

def _convert_cached_virtuals(data):
    iterable = iter(data)
    # skip the mtime entry.
    iterable.next()
    d = {}
    try:
        for item in iterable:
            d.setdefault(item, {}).setdefault(iterable.next(), []).append(
                atom(iterable.next()))
    except InvalidDependency:
        return None
    return d

def _caching_grab_virtuals(repo, cache_basedir):
    virtuals = {}
    update = False
    cache = _read_mtime_cache(os.path.join(cache_basedir, 'virtuals.cache'))
    existing = _get_mtimes(repo_locations)
    for cat, mtime in existing.iteritems():
        d = cache.pop(cat, None)
        if d is not None and long(d[1]) == mtime:
            d = _convert_cache_virtuals(d)
            if d is not None:
                for pkg, fullver_d in d.iteritems():
                    for fullver, virtuals in fullver_d.iteritems():
                        virtuals.setdefault(pkg, {}).setdefault(
                            fullver, []).extend(virtuals)
        if d is None:
            update = True
            _collect_virtuals(virtuals, repo.itermatch(
                packages.PackageRestriction("category",
                    values.StrExactMatch(cat))))

    if update or cache:
        _write_mtime_cache(existing, virtuals,
            os.path.join(cache_basedir, 'virtuals.cache'))
    _finalize_virtuals(virtuals)
    return virtuals

def caching_virtuals(repo, cache_basedir, livefs=True):
    return virtual.tree(partial(_caching_grab_virtuals, repo, cache_basedir),
        livefs=livefs)
