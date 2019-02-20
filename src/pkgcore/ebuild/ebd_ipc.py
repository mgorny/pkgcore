from functools import wraps
import itertools
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys

from snakeoil.cli import arghparse
from snakeoil.compatibility import IGNORED_EXCEPTIONS
from snakeoil.demandload import demandload
from snakeoil.iterables import partition
from snakeoil.osutils import pjoin

from pkgcore.exceptions import PkgcoreException, PkgcoreUserException

demandload(
    'grp',
    'operator:itemgetter',
    'pwd',
    'pkgcore:os_data',
)


class IpcError(PkgcoreException):
    """Generic IPC errors."""

    def __init__(self, msg='', code=1):
        self.msg = msg
        self.code = code
        self.ret = f'{code}\x07{msg}'


class IpcInternalError(IpcError):
    """IPC errors related to internal bugs."""


class IpcCommandError(IpcError, PkgcoreUserException):
    """IPC errors related to parsing arguments or running the command."""


class UnknownOptions(IpcCommandError):
    """Unknown options passed to IPC command."""

    def __init__(self, options):
        super().__init__(f"unknown options: {', '.join(map(repr, options))}")


class IpcArgumentParser(arghparse.OptionalsParser, arghparse.CustomActionsParser):
    """Raise IPC exception for argparse errors.

    Otherwise standard argparse prints the parser usage then outputs the error
    message to stderr.
    """

    def error(self, msg):
        raise IpcCommandError(msg)


class IpcCommand(object):
    """Commands sent from the bash side of the ebuild daemon to run."""

    # argument parser for internal options
    parser = None
    # argument parser for user options
    opts_parser = None

    def __init__(self, op):
        self.ED = op.ED
        self.pkg = op.pkg
        self.eapi = op.pkg.eapi
        self.observer = op.observer
        self._helper = self.__class__.__name__.lower().replace('_', '.')
        self.opts = arghparse.Namespace()

    def __call__(self, ebd):
        self.ebd = ebd
        ret = 0

        try:
            # read info from bash side
            nonfatal = self.read() == 'true'
            self.cwd = self.read()
            options = shlex.split(self.read())
            args = self.read().strip('\0')
            args = args.split('\0') if args else []
            # parse args and run command
            args = self.parse_options(options, args)
            args = self.finalize(args)
            self.run(args)
        except IGNORED_EXCEPTIONS:
            raise
        except IpcCommandError as e:
            if nonfatal:
                self.warn(str(e))
            else:
                raise
        except Exception as e:
            raise IpcInternalError(f'internal failure') from e

        # return completion status to the bash side
        self.write(ret)

    def parse_options(self, opts, args):
        """Parse internal args passed from the bash side."""
        if self.parser is not None:
            opts, unknown = self.parser.parse_known_args(opts, namespace=self.opts)
            if unknown:
                raise UnknownOptions(unknown)

        # pull user options off the start of the argument list
        if self.opts_parser is not None:
            opts, args = self.opts_parser.parse_optionals(args, namespace=self.opts)
        return args

    def finalize(self, args):
        """Finalize the options and arguments for the IPC command."""
        return args

    def run(self, args):
        """Run the requested IPC command."""
        raise NotImplementedError

    def read(self):
        """Read a line from the ebuild daemon."""
        return self.ebd.read().strip()

    def write(self, data):
        """Write data to the ebuild daemon.

        Args:
            data: data to be sent to the bash side
        """
        self.ebd.write(data)

    def warn(self, msg):
        """Output warning message.

        Args:
            msg: message to be output
        """
        self.observer.warn(f'{self._helper}: {msg}')
        self.observer.flush()


def _parse_group(group):
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        pass
    return int(group)


def _parse_user(user):
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        pass
    return int(user)


def _parse_mode(mode):
    try:
        return int(mode, 8)
    except ValueError:
        return None


def command_options(s):
    """Split string of command line options into list."""
    return shlex.split(s)


class _InstallWrapper(IpcCommand):
    """Python wrapper for commands using `install`."""

    parser = IpcArgumentParser(add_help=False)
    parser.add_argument('--dest', required=True)
    parser.add_argument('--insoptions', default='', type=command_options)
    parser.add_argument('--diroptions', default='', type=command_options)

    # supported install options
    install_parser = IpcArgumentParser(add_help=False)
    install_parser.add_argument('-g', '--group', default=-1, type=_parse_group)
    install_parser.add_argument('-o', '--owner', default=-1, type=_parse_user)
    install_parser.add_argument('-m', '--mode', default=0o755, type=_parse_mode)
    install_parser.add_argument('-p', '--preserve-timestamps', action='store_true')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_symlinks = False
        self.insoptions = arghparse.Namespace()
        self.diroptions = arghparse.Namespace()
        # default to python-based install cmds
        self.install = self._install
        self.install_dirs = self._install_dirs

    def parse_options(self, opts, args):
        args = super().parse_options(opts, args)
        return self.parse_install_options(opts, args)

    def parse_install_options(self, opts, args):
        """Parse install command options."""
        if self.opts.insoptions:
            if not self._parse_install_options(self.opts.insoptions, self.insoptions):
                self.install = self._install_cmd
        if self.opts.diroptions:
            if not self._parse_install_options(self.opts.diroptions, self.diroptions):
                self.install_dirs = self._install_dirs_cmd
        return args

    def _parse_install_options(self, options, namespace):
        """Internal install command option parser.

        Args:
            options: list of options to parse
            namespace: argparse namespace to populate
        Returns:
            True when all options are handled,
            otherwise False if unknown/unhandled options exist.
        """
        opts, unknown = self.install_parser.parse_known_args(options, namespace=namespace)
        if unknown or opts.mode is None:
            msg = "falling back to 'install'"
            if unknown:
                msg += f": unhandled options: {' '.join(map(repr, unknown))}"
            self.warn(msg)
            return False
        return True

    def finalize(self, args):
        self.dest_dir = self.opts.dest.lstrip(os.path.sep)

        if not args:
            raise IpcCommandError('missing targets to install')
        args = (x.rstrip(os.path.sep) for x in args)
        return args

    def run(self, args):
        os.chdir(self.cwd)
        try:
            dest_dir = pjoin(self.ED, self.dest_dir)
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            raise IpcCommandError(
                f'failed creating dir: {dest_dir!r}: {e.strerror}')
        self._install_targets(args)

    def _prefix_targets(files):
        """Decorator to prepend targets being installed with the destination path."""
        def wrapper(func):
            @wraps(func)
            def wrapped(self, targets):
                if files:
                    targets = ((s, pjoin(self.ED, self.dest_dir, d)) for s, d in targets)
                else:
                    targets = (pjoin(self.ED, self.dest_dir, d) for d in targets)
                return func(self, targets)
            return wrapped
        return wrapper

    def _install_targets(self, targets):
        """Install targets.

        Args:
            targets: files/symlinks/dirs/etc to install
        """
        files = ((f, os.path.basename(f)) for f in targets)
        self._install_files(files)

    def _install_files(self, files):
        """Install files into a given directory.

        Args:
            files: iterable of (path, target dir) tuples of files to install
        """
        files, symlinks = partition(files, predicate=lambda x: os.path.islink(x[0]))
        self.install(files)
        # TODO: warn on symlinks when not supported?
        if self.allow_symlinks:
            self.install_symlinks(symlinks)

    def _install_from_dirs(self, dirs):
        """Install all targets under given directories.

        Args:
            dirs: iterable of directories to install from
        """
        def scan_dirs(paths):
            for d in paths:
                dest_dir = os.path.basename(d)
                yield True, dest_dir
                for dirpath, dirnames, filenames in os.walk(d):
                    for dirname in dirnames:
                        source_dir = pjoin(dirpath, dirname)
                        relpath = os.path.relpath(source_dir, self.cwd)
                        if os.path.islink(source_dir):
                            dest = pjoin(dest_dir, os.path.basename(relpath))
                            yield False, (relpath, dest)
                        else:
                            yield True, relpath
                    for f in filenames:
                        source = os.path.relpath(pjoin(dirpath, f), self.cwd)
                        dest = pjoin(dest_dir, os.path.basename(f))
                        yield False, (source, dest)

        # determine all files and dirs under target directories and install them
        files, dirs = partition(scan_dirs(dirs), predicate=itemgetter(0))
        self.install_dirs(x for _, x in dirs)
        self._install_files(x for _, x in files)

    @staticmethod
    def _set_attributes(opts, path):
        """Set file attributes on a given path.

        Args:
            path: file/directory path
        """
        try:
            if opts.owner != -1 or opts.group != -1:
                os.lchown(path, opts.owner, opts.group)
            if opts.mode is not None:
                os.chmod(path, opts.mode)
        except OSError as e:
            raise IpcCommandError(
                f'failed setting file attributes: {path!r}: {e.strerror}')

    @staticmethod
    def _set_timestamps(source_stat, dest):
        """Apply timestamps from source_stat to dest.

        Args:
            source_stat: stat result for the source file
            dest: path to the dest file
        """
        os.utime(dest, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))

    def _is_install_allowed(self, source, source_stat, dest):
        """Determine if installing source into dest should work.

        This aims to aid compatibility with the `install` command.

        Args:
            source: path to the source file
            source_stat: stat result for the source file, using stat()
                rather than lstat(), in order to match the `install`
                command
            dest: path to the dest file
        Raises:
            IpcCommandError on failure
        Returns:
            True if the install should succeed
        """
        # matching `install` command, use stat() for source and lstat() for dest
        try:
            dest_lstat = os.lstat(dest)
        except FileNotFoundError:
            # installing file to a new path
            return True
        except OSError as e:
            raise IpcCommandError(f'cannot stat {dest!r}: {e.strerror}')

        # installing symlink
        if stat.S_ISLNK(dest_lstat.st_mode):
            return True

        # source file and dest file are different
        if not os.path.samestat(source_stat, dest_lstat):
            return True

        # installing hardlink if source and dest are different
        if (dest_lstat.st_nlink > 1 and os.path.realpath(source) != os.path.realpath(dest)):
            return True

        raise IpcCommandError(f'{source!r} and {dest!r} are identical')

    @_prefix_targets(files=True)
    def _install(self, files):
        """Install files.

        Args:
            files: iterable of (source, dest) tuples of files to install
        Raises:
            IpcCommandError on failure
        """
        for source, dest in files:
            try:
                sstat = os.stat(source)
            except OSError as e:
                raise IpcCommandError(f'cannot stat {source!r}: {e.strerror}')

            self._is_install_allowed(source, sstat, dest)

            # matching `install` command, remove dest before file install
            try:
                os.unlink(dest)
            except FileNotFoundError:
                pass
            except OSError as e:
                raise IpcCommandError(f'failed removing file: {dest!r}: {e.strerror}')

            try:
                shutil.copyfile(source, dest)
                if self.insoptions:
                    self._set_attributes(self.insoptions, dest)
                    if self.insoptions.preserve_timestamps:
                        self._set_timestamps(sstat, dest)
            except OSError as e:
                raise IpcCommandError(
                    f'failed copying file: {source!r} to {dest!r}: {e.strerror}')

    @_prefix_targets(files=True)
    def _install_cmd(self, files):
        """Install files using `install` command.

        Args:
            files: iterable of (source, dest) tuples of files to install
        Raises:
            IpcCommandError on failure
        """
        files = sorted(files, key=itemgetter(1))
        for dest, files_group in itertools.groupby(files, itemgetter(1)):
            sources = list(path for path, _ in files_group)
            command = ['install'] + self.opts.insoptions + sources + [dest]
            try:
                subprocess.run(command, check=True, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as e:
                raise IpcCommandError(e.stderr.decode())

    @_prefix_targets(files=False)
    def _install_dirs(self, dirs):
        """Create directories.

        Args:
            dirs: iterable of paths where directories should be created
        Raises:
            IpcCommandError on failure
        """
        try:
            for d in dirs:
                os.makedirs(d, exist_ok=True)
                if self.diroptions:
                    self._set_attributes(self.diroptions, d)
        except OSError as e:
            raise IpcCommandError(f'failed creating dir: {d!r}: {e.strerror}')

    @_prefix_targets(files=False)
    def _install_dirs_cmd(self, dirs):
        """Create directories using `install` command.

        Args:
            dirs: iterable of paths where directories should be created
        Raises:
            IpcCommandError on failure
        """
        command = ['install', '-d'] + self.opts.diroptions + list(dirs)
        try:
            subprocess.run(command, check=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            raise IpcCommandError(e.stderr.decode())

    @_prefix_targets(files=True)
    def install_symlinks(self, symlinks):
        """Install iterable of symlinks.

        Args:
            symlinks: iterable of (path, target dir) tuples of symlinks to install
        Raises:
            IpcCommandError on failure
        """
        try:
            for symlink, dest in symlinks:
                os.symlink(os.readlink(symlink), dest)
        except OSError as e:
            raise IpcCommandError(
                f'failed creating symlink: {symlink!r} -> {dest!r}: {e.strerror}')


class Doins(_InstallWrapper):
    """Python wrapper for doins."""

    opts_parser = IpcArgumentParser(add_help=False)
    opts_parser.add_argument('-r', dest='recursive', action='store_true')

    def finalize(self, *args, **kwargs):
        self.allow_symlinks = self.eapi.options.doins_allow_symlinks
        return super().finalize(*args, **kwargs)

    def _install_targets(self, targets):
        files, dirs = partition(targets, predicate=os.path.isdir)
        if self.opts.recursive:
            self._install_from_dirs(dirs)
        files = ((f, os.path.basename(f)) for f in files)
        self._install_files(files)


class Dodoc(_InstallWrapper):
    """Python wrapper for dodoc."""

    opts_parser = IpcArgumentParser(add_help=False)
    opts_parser.add_argument('-r', dest='recursive', action='store_true')

    def parse_install_options(self, *args, **kwargs):
        self.opts.insoptions = ['-m0644']
        return super().parse_install_options(*args, **kwargs)

    def finalize(self, *args, **kwargs):
        self.allow_recursive = self.eapi.options.dodoc_allow_recursive
        return super().finalize(*args, **kwargs)

    def _install_targets(self, targets):
        files, dirs = partition(targets, predicate=os.path.isdir)
        # TODO: add peekable class for iterables to avoid list conversion
        dirs = list(dirs)
        if dirs:
            if self.opts.recursive and self.allow_recursive:
                self._install_from_dirs(dirs)
            else:
                missing_option = ', missing -r option?' if self.allow_recursive else ''
                raise IpcCommandError(f'{dirs[0]!r} is a directory{missing_option}')
        # TODO: skip/warn installing empty files
        files = ((f, os.path.basename(f)) for f in files)
        self._install_files(files)


class Doinfo(_InstallWrapper):
    """Python wrapper for doinfo."""

    def parse_install_options(self, *args, **kwargs):
        self.opts.insoptions = ['-m0644']
        return super().parse_install_options(*args, **kwargs)


class Doexe(_InstallWrapper):
    """Python wrapper for doexe."""


class Dobin(_InstallWrapper):
    """Python wrapper for dobin."""

    def parse_install_options(self, *args, **kwargs):
        # TODO: fix this to be prefix aware at some point
        self.opts.insoptions = [
            '-m0755', f'-g{os_data.root_gid}', f'-o{os_data.root_uid}']
        return super().parse_install_options(*args, **kwargs)


class Dosbin(Dobin):
    """Python wrapper for dosbin."""


class Dolib(_InstallWrapper):
    """Python wrapper for dolib."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_symlinks = True


class Dolib_so(Dolib):
    """Python wrapper for dolib.so."""


class Dolib_a(Dolib):
    """Python wrapper for dolib.a."""


class Doman(_InstallWrapper):
    """Python wrapper for doman."""

    opts_parser = IpcArgumentParser(add_help=False)
    opts_parser.add_argument('-i18n', action='store_true', default='')

    detect_lang_re = re.compile(r'^(\w+)\.([a-z]{2}([A-Z]{2})?)\.(\w+)$')
    valid_mandir_re = re.compile(r'man[0-9n](f|p|pm)?$')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_symlinks = True

    def parse_install_options(self, *args, **kwargs):
        self.opts.insoptions = ['-m0644']
        return super().parse_install_options(*args, **kwargs)

    def finalize(self, *args, **kwargs):
        self.language_detect = self.eapi.options.doman_language_detect
        self.language_override = self.eapi.options.doman_language_override
        return super().finalize(*args, **kwargs)

    def _filter_targets(self, files):
        dirs = set()
        for f in files:
            basename = os.path.basename(f)
            suffix = os.path.splitext(basename)[1][1:]

            if self.eapi.archive_suffixes_re.match(suffix):
                # TODO: uncompress/warn?
                suffix = os.path.splitext(basename.rsplit('.', 1)[0])[1][1:]

            name = basename
            mandir = f'man{suffix}'

            if self.language_override and self.opts.i18n:
                mandir = pjoin(self.opts.i18n, mandir)
            elif self.language_detect:
                match = self.detect_lang_re.match(basename)
                if match:
                    name = f'{match.group(0)}.{match.group(3)}'
                    mandir = pjoin(match.group(1), mandir)

            if self.valid_mandir_re.match(mandir):
                if mandir not in dirs:
                    yield True, mandir
                    dirs.add(mandir)
                yield False, (f, pjoin(mandir, name))
            else:
                raise IpcCommandError(f'invalid man page: {f}')

    def _install_targets(self, targets):
        # TODO: skip/warn installing empty files
        targets = self._filter_targets(targets)
        files, dirs = partition(targets, predicate=itemgetter(0))
        self.install_dirs(x for _, x in dirs)
        self._install_files(x for _, x in files)


class Domo(_InstallWrapper):
    """Python wrapper for domo."""

    def parse_install_options(self, *args, **kwargs):
        self.opts.insoptions = ['-m0644']
        return super().parse_install_options(*args, **kwargs)

    def _filter_targets(self, files):
        dirs = set()
        for f in files:
            d = pjoin(os.path.splitext(os.path.basename(f))[0], 'LC_MESSAGES')
            if d not in dirs:
                yield True, d
                dirs.add(d)
            yield False, (f, pjoin(d, f'{self.pkg.PN}.mo'))

    def _install_targets(self, targets):
        # TODO: skip/warn installing empty files
        targets = self._filter_targets(targets)
        files, dirs = partition(targets, predicate=itemgetter(0))
        self.install_dirs(x for _, x in dirs)
        self._install_files(x for _, x in files)


class Dohtml(_InstallWrapper):
    """Python wrapper for dohtml."""

    opts_parser = IpcArgumentParser(add_help=False)
    opts_parser.add_argument('-r', dest='recursive', action='store_true')
    opts_parser.add_argument('-V', dest='verbose', action='store_true')
    opts_parser.add_argument('-A', dest='extra_allowed_file_exts', action='csv', default=[])
    opts_parser.add_argument('-a', dest='allowed_file_exts', action='csv', default=[])
    opts_parser.add_argument('-f', dest='allowed_files', action='csv', default=[])
    opts_parser.add_argument('-x', dest='excluded_dirs', action='csv', default=[])
    opts_parser.add_argument('-p', dest='doc_prefix', default='')

    # default allowed file extensions
    default_allowed_file_exts = ('css', 'gif', 'htm', 'html', 'jpeg', 'jpg', 'js', 'png')

    def parse_install_options(self, *args, **kwargs):
        self.opts.insoptions = ['-m0644']
        return super().parse_install_options(*args, **kwargs)

    def finalize(self, *args, **kwargs):
        args = super().finalize(*args, **kwargs)
        self.dest_dir = pjoin(self.dest_dir, self.opts.doc_prefix.lstrip(os.path.sep))

        if not self.opts.allowed_file_exts:
            self.opts.allowed_file_exts = list(self.default_allowed_file_exts)
        self.opts.allowed_file_exts.extend(self.opts.extra_allowed_file_exts)

        self.opts.allowed_file_exts = set(self.opts.allowed_file_exts)
        self.opts.excluded_dirs = set(self.opts.excluded_dirs)
        self.opts.allowed_files = set(self.opts.allowed_files)

        if self.opts.verbose:
            self.observer.write(str(self) + '\n')
            self.observer.flush()

        return args

    def __str__(self):
        msg = ['dohtml:', f'  Installing to: /{self.dest_dir}']
        if self.opts.allowed_file_exts:
            msg.append(
                f"  Allowed extensions: {', '.join(sorted(self.opts.allowed_file_exts))}")
        if self.opts.excluded_dirs:
            msg.append(
                f"  Allowed extensions: {', '.join(sorted(self.opts.allowed_file_exts))}")
        if self.opts.allowed_files:
            msg.append(
                f"  Allowed files: {', '.join(sorted(self.opts.allowed_files))}")
        if self.opts.doc_prefix:
            msg.append(f"  Document prefix: {self.opts.doc_prefix!r}")
        return '\n'.join(msg)

    def _install_targets(self, targets):
        files, dirs = partition(targets, predicate=os.path.isdir)
        # TODO: add peekable class for iterables to avoid list conversion
        dirs = list(dirs)
        if dirs:
            if self.opts.recursive:
                dirs = (d for d in dirs if d not in self.opts.excluded_dirs)
                self._install_from_dirs(dirs)
            else:
                raise IpcCommandError(f'{dirs[0]!r} is a directory, missing -r option?')
        files = ((f, os.path.basename(f)) for f in files)
        self._install_files(files)

    def _allowed_file(self, item):
        """Determine if a file is allowed to be installed."""
        path, dest = item
        basename = os.path.basename(path)
        ext = os.path.splitext(basename)[1][1:]
        return (ext in self.opts.allowed_file_exts or basename in self.opts.allowed_files)

    def _install_files(self, files):
        self.install(f for f in files if self._allowed_file(f))