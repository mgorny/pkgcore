# Copyright: 2006-2008 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD

__all__ = ("ProfileError", "ProfileNode", "EmptyRootNode", "OnDiskProfile",
    "UserProfile", "PkgProvided", "AliasedVirtuals")

import errno, os
from itertools import chain

from pkgcore.config import ConfigHint
from pkgcore.ebuild import const, ebuild_src
from pkgcore.ebuild.misc import (incremental_expansion, restrict_payload,
    _build_cp_atom_payload, chunked_data, ChunkedDataDict)
from pkgcore.repository import virtual

from snakeoil.osutils import abspath, join as pjoin, readlines_utf8
from snakeoil.containers import InvertedContains
from snakeoil.fileutils import iter_read_bash, read_bash_dict
from snakeoil.caching import WeakInstMeta
from snakeoil.currying import partial
from snakeoil.compatibility import next, is_py3k
from snakeoil.demandload import demandload

demandload(globals(),
    'snakeoil.data_source:local_source',
    'pkgcore.ebuild:cpv',
    'pkgcore.ebuild:atom',
    'pkgcore.repository:util',
    'pkgcore.restrictions:packages',
    'snakeoil.mappings:defaultdict,ImmutableDict',
)


class ProfileError(Exception):

    def __init__(self, path, filename, error):
        self.path, self.filename, self.error = path, filename, error

    def __str__(self):
        return "ProfileError: profile %r, file %r, error %s" % (
            self.path, self.filename, self.error)

def load_decorator(filename, handler=iter_read_bash, fallback=(),
    read_func=readlines_utf8):
    def f(func):
        def f2(self, *args):
            path = pjoin(self.path, filename)
            try:
                data = read_func(path, True, True, True)
                if data is None:
                    return func(self, fallback, *args)
                return func(self, handler(data), *args)
            except (KeyboardInterrupt, RuntimeError, SystemExit):
                raise
            except ProfileError:
                # no point in wrapping/throwing..
                raise
            except Exception, e:
                raise ProfileError(self.path, filename, e)
        return f2
    return f

def split_negations(data, func=str):
    neg, pos = [], []
    for line in data:
        if line[0] == '-':
            if len(line) == 1:
                raise ValueError("'-' negation without a token")
            neg.append(func(line[1:]))
        else:
            pos.append(func(line))
    return (tuple(neg), tuple(pos))


class ProfileNode(object):

    __metaclass__ = WeakInstMeta
    __inst_caching__ = True

    def __init__(self, path):
        if not os.path.isdir(path):
            raise ProfileError(path, "", "profile doesn't exist")
        self.path = path

    def __str__(self):
        return "Profile at %r" % self.path

    def __repr__(self):
        return '<%s path=%r, @%#8x>' % (self.__class__.__name__, self.path,
            id(self))

    @load_decorator("packages")
    def _load_packages(self, data):
        # sys packages and visibility
        sys, neg_sys, vis, neg_vis = [], [], [], []
        for line in data:
            if line[0] == '-':
                if line[1] == '*':
                    neg_sys.append(self.eapi_atom(line[2:]))
                else:
                    neg_vis.append(self.eapi_atom(line[1:], negate_vers=True))
            else:
                if line[0] == '*':
                    sys.append(self.eapi_atom(line[1:]))
                else:
                    vis.append(self.eapi_atom(line, negate_vers=True))
        self.system = (tuple(neg_sys), tuple(sys))
        self.visibility = (tuple(neg_vis), tuple(vis))

    @load_decorator("parent")
    def _load_parents(self, data):
        kls = getattr(self, 'parent_node_kls', self.__class__)
        self.parents = tuple(kls(abspath(pjoin(self.path, x)))
            for x in data)
        return self.parents

    @load_decorator("package.provided")
    def _load_pkg_provided(self, data):
        self.pkg_provided = split_negations(data, cpv.versioned_CPV)
        return self.pkg_provided

    @load_decorator("virtuals")
    def _load_virtuals(self, data):
        d = {}
        for line in data:
            l = line.split()
            if len(l) != 2:
                raise ValueError("%r is malformated" % line)
            d[cpv.CPV.unversioned(l[0]).package] = self.eapi_atom(l[1])
        self.virtuals = ImmutableDict(d)
        return d

    @load_decorator("package.mask")
    def _load_masks(self, data):
        self.masks = split_negations(data, self.eapi_atom)
        return self.masks

    @load_decorator("deprecated", (lambda i:i), None)
    def _load_deprecated(self, data):
        if data is not None:
            data = iter(data)
            try:
                replacement = next(data).strip()
                msg = "\n".join(x.lstrip("#").strip()
                    for x in data)
                data = (replacement, msg)
            except StopIteration:
                # only an empty replacement could trigger this; thus
                # formatted badly.
                raise ValueError("didn't specify a replacement profile")
        self.deprecated = data
        return data

    @load_decorator("use.mask")
    def _load_masked_use(self, data):
        d = self.pkg_use_mask
        neg, pos = split_negations(data)
        if neg or pos:
            d = ImmutableDict(chain(d.iteritems(),
                [(packages.AlwaysTrue, (chunked_data(packages.AlwaysTrue, neg, pos),))]
                ))
        self.masked_use = d
        return d

    def _parse_package_use(self, data):
        d = defaultdict(list)
        # split the data down ordered cat/pkg lines
        for line in data:
            l = line.split()
            a = self.eapi_atom(l[0])
            if len(l) == 1:
                raise Exception("malformed line- %r" % (line,))
            d[a.key].append(chunked_data(a,
                *split_negations(l[1:])))

        return ImmutableDict((k, _build_cp_atom_payload(v, atom.atom(k))) for k,v in d.iteritems())

    @load_decorator("package.use")
    def _load_pkg_use(self, data):
        self.pkg_use = d = self._parse_package_use(data)
        return d

    @load_decorator("package.use.force")
    def _load_pkg_use_force(self, data):
        self.pkg_use_force = d = self._parse_package_use(data)
        return d

    @load_decorator("package.use.mask")
    def _load_pkg_use_mask(self, data):
        self.pkg_use_mask = d = self._parse_package_use(data)
        return d

    @load_decorator("use.force")
    def _load_forced_use(self, data):
        d = self.pkg_use_force
        neg, pos = split_negations(data)
        if neg or pos:
            d = ImmutableDict(chain(d.iteritems(),
                [(packages.AlwaysTrue, (chunked_data(packages.AlwaysTrue, neg, pos),))]
                ))
        self.forced_use = d
        return d

    def _load_default_env(self):
        path = pjoin(self.path, "make.defaults")
        try:
            if is_py3k:
                f = open(path, 'r', encoding='utf8')
            else:
                f = open(path, "r")
        except IOError, ie:
            if ie.errno != errno.ENOENT:
                raise ProfileError(self.path, "make.defaults", ie)
            self.default_env = {}
            return self.default_env
        try:
            try:
                d = read_bash_dict(f)
            finally:
                f.close()
        except (KeyboardInterrupt, RuntimeError, SystemExit):
            raise
        except Exception ,e:
            raise ProfileError(self.path, "make.defaults", e)
        self.default_env = ImmutableDict(d)
        return d

    def _load_bashrc(self):
        path = pjoin(self.path, "profile.bashrc")
        if os.path.exists(path):
            self.bashrc = local_source(path)
        else:
            self.bashrc = None
        return self.bashrc

    @load_decorator('eapi', fallback=('0',))
    def _load_eapi(self, data):
        data = [x.strip() for x in data]
        data = filter(None, data)
        if len(data) != 1:
            raise ProfileError(self.path, 'eapi', "multiple lines detected")
        elif not (data[0].isdigit() and int(data[0]) in const.eapi_capable):
            raise ProfileError(self.path, 'eapi', 'unsupported eapi: %s' % data[0])
        self.eapi = data[0]
        return self.eapi

    def _load_eapi_atom(self):
        self.eapi_atom = getattr(atom.atom, 'eapi%s_atom' % self.eapi)
        return self.eapi_atom

    def __getattr__(self, attr):
        if attr in ("system", "visibility"):
            self._load_packages()
            return getattr(self, attr)
        # use objects getattr to bypass our own; prevents infinite recursion
        # if they request something non existant
        try:
            func = object.__getattribute__(self, "_load_%s" % attr)
        except AttributeError:
            raise AttributeError(self, attr)
        if func is None:
            raise AttributeError(attr)
        return func()


class EmptyRootNode(ProfileNode):

    __inst_caching__ = True

    parents = ()
    deprecated = None
    forced_use = masked_use = {}
    pkg_provided = visibility = system = ((), ())
    virtuals = {}


def _empty_provides_iterable(*args, **kwds):
    return iter(())

def _empty_provides_has_match(*args, **kwds):
    return False


class OnDiskProfile(object):

    pkgcore_config_type = ConfigHint({'basepath':'str', 'profile':'str',
        'incrementals':'list'}, required=('basepath', 'profile'),
        typename='profile')

    _node_kls = ProfileNode

    def __init__(self, basepath, profile, incrementals=const.incrementals,
        load_profile_base=True,
        incrementals_unfinalized=const.incrementals_unfinalized):
        self.basepath = basepath
        self.profile = profile
        self.node = self._node_kls(pjoin(basepath, profile))
        self.incrementals = incrementals
        self.incrementals_unfinalized = frozenset(incrementals_unfinalized)
        self.load_profile_base = load_profile_base

    @property
    def arch(self):
        return self.default_env.get("ARCH")

    @property
    def deprecated(self):
        return self.node.deprecated

    def _load_stack(self):
        def f(node):
            for x in node.parents:
                for y in f(x):
                    yield y
            yield node

        l = list(f(self.node))
        if self.load_profile_base:
            l = [EmptyRootNode(self.basepath)] + l
        return tuple(l)

    def _collapse_use_dict(self, attr):

        stack = [getattr(x, attr) for x in self.stack]

        d = ChunkedDataDict()
        for mapping in stack:
            d.update_from_mapping(mapping)

        return d.render_to_payload()

    def _collapse_generic(self, attr):
        s = set()
        for node in self.stack:
            val = getattr(node, attr)
            s.difference_update(val[0])
            s.update(val[1])
        return s

    def _collapse_env(self):
        d = {}
        incrementals = self.incrementals
        for profile in self.stack:
            for key, val in profile.default_env.iteritems():
                if key in incrementals:
                    d.setdefault(key, []).extend(val.split())
                else:
                    d[key] = val
        for incremental in incrementals:
            if incremental not in d:
                continue
            val = d[incremental]
            if not val:
                del d[val]
                continue
            if incremental in self.incrementals_unfinalized:
                d[incremental] = tuple(val)
            else:
                s = set()
                incremental_expansion(s, val)
                if s:
                    d[incremental] = tuple(s)
                else:
                    del d[incremental]
        return d

    @property
    def use_expand(self):
        if "USE_EXPAND" in self.incrementals:
            return tuple(self.default_env.get("USE_EXPAND", ()))
        return tuple(self.default_env.get("USE_EXPAND", '').split())

    @property
    def use_expand_hidden(self):
        if "USE_EXPAND_HIDDEN" in self.incrementals:
            return tuple(self.default_env.get("USE_EXPAND_HIDDEN", ()))
        return tuple(self.default_env.get("USE_EXPAND_HIDDEN", "").split())

    def _collapse_virtuals(self):
        d = {}
        for profile in self.stack:
            d.update(profile.virtuals)
        self.virtuals = d
        self.make_virtuals_repo = partial(AliasedVirtuals, d)

    def _collapse_pkg_provided(self):
        d = {}
        for pkg in self._collapse_generic("pkg_provided"):
            d.setdefault(pkg.category, {}).setdefault(pkg.package,
                []).append(pkg.fullver)
        intermediate_parent = PkgProvidedParent()
        obj = util.SimpleTree(d, pkg_klass=partial(PkgProvided,
            intermediate_parent), livefs=True, frozen=True)
        intermediate_parent._parent_repo = obj

        if not d:
            obj.match = obj.itermatch = _empty_provides_iterable
            obj.has_match = _empty_provides_has_match
        return obj
    def _collapse_masks(self):
        return frozenset(chain(self._collapse_generic("masks"),
            self._collapse_generic("visibility")))

    def __getattr__(self, attr):
        if attr == "stack":
            self.stack = obj = self._load_stack()
        elif attr in ('forced_use', 'masked_use', 'pkg_use'):
            obj = self._collapse_use_dict(attr)
            setattr(self, attr, obj)
        elif attr == 'bashrc':
            obj = self.bashrc = tuple(x.bashrc
                for x in self.stack if x.bashrc is not None)
        elif attr == 'system':
            obj = self.system = self._collapse_generic(attr)
        elif attr == 'masks':
            obj = self.masks = self._collapse_masks()
        elif attr == 'default_env':
            obj = self.default_env = self._collapse_env()
        elif attr == 'virtuals':
            self._collapse_virtuals()
            obj = self.virtuals
        elif attr == 'make_virtuals_repo':
            self._collapse_virtuals()
            obj = self.make_virtuals_repo
        elif attr == 'provides_repo':
            obj = self.provides_repo = self._collapse_pkg_provided()
        elif attr == 'path':
            obj = self.node.path
        else:
            raise AttributeError(attr)
        return obj


class UserProfileNode(ProfileNode):

    parent_node_kls = ProfileNode

    def __init__(self, path, parent_path):
        self.override_path = pjoin(path, parent_path)
        ProfileNode.__init__(self, path)

    def _load_parents(self):
        self.parents = (ProfileNode(self.override_path),)
        return self.parents


class UserProfile(OnDiskProfile):

    pkgcore_config_type = ConfigHint({'user_path':'str', 'parent_path':'str',
        'parent_profile':'str', 'incrementals':'list'},
        required=('user_path','parent_path', 'parent_profile'),
        typename='profile')

    def __init__(self, user_path, parent_path, parent_profile,
        incrementals=const.incrementals, load_profiles_base=False):
        OnDiskProfile.__init__(self, parent_path, parent_profile,
            incrementals, load_profiles_base)
        self.node = UserProfileNode(user_path, pjoin(parent_path, parent_profile))


class PkgProvidedParent(object):

    def __init__(self, **kwds):
        self.__dict__.update(kwds)


class PkgProvided(ebuild_src.base):

    __slots__ = ('use',)

    package_is_real = False
    __inst_caching__ = True

    keywords = InvertedContains(())

    def __init__(self, *a, **kwds):
        ebuild_src.base.__init__(self, *a, **kwds)
        object.__setattr__(self, "use", [])
        object.__setattr__(self, "data", {})


class ForgetfulDict(dict):

    def __setitem__(self, key, attr):
        return

    def update(self, other):
        return


class AliasedVirtuals(virtual.tree):

    """
    repository generated from a profiles default virtuals
    """

    def __init__(self, virtuals, repo, *overrides):
        """
        :param virtuals: dict of virtual -> providers
        :param repo: L{pkgcore.ebuild.repository.UnconfiguredTree} parent repo
        :keyword overrides: mapping of virtual pkgname -> matches to override defaults
        """
        virtual.tree.__init__(self, livefs=False)
        self._original_virtuals = virtuals
        self._overrides = tuple(overrides)
        if not overrides:
            # no point in delaying.
            self.packages._cache['virtuals'] = tuple(virtuals.iterkeys())
            self._virtuals = virtuals
        self.aliased_repo = repo
        self._versions_map = {}

    def _load_data(self):
        self._virtuals = self._delay_apply_overrides(self._original_virtuals,
            self._overrides)
        self.packages._cache['virtual'] = tuple(self._virtuals.iterkeys())

    @staticmethod
    def _delay_apply_overrides(virtuals, overrides):
        d = {}
        for vtree in overrides:
            for virt, provider in vtree.default_providers.iteritems():
                if virt in d:
                    d[virt] &= d[virt] & provider
                else:
                    d[virt] = provider

        if not d:
            return virtuals
        for k, v in d.iteritems():
            if len(v) == 1:
                d[k] = tuple(v)[0]
            else:
                d[k] = packages.OrRestriction(*v)
        virtuals = virtuals.copy()
        virtuals.update(d)
        return virtuals

    def _get_versions(self, catpkg):
        if catpkg[0] != "virtual":
            raise KeyError("no %s package in this repository" % catpkg)
        vers = set()
        for pkg in self.aliased_repo.itermatch(self._virtuals[catpkg[1]]):
            self._versions_map.setdefault(catpkg[1], {}).setdefault(pkg.fullver, []).append(
                pkg.versioned_atom)
            vers.add(pkg.fullver)
        return tuple(vers)

    def _expand_vers(self, cp, ver):
        return self._versions_map.get(cp[1], {}).get(ver, ())

    def _fetch_metadata(self, pkg):
        data = self._virtuals[pkg.package]
        if isinstance(data, atom.atom):
            data = [data]
        data = [atom.atom("=%s-%s" % (x.key, pkg.fullver)) for x in data]
        if len(data) == 1:
            return data[0]
        return packages.OrRestriction(*data)
