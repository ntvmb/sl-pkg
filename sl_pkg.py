#!/usr/bin/env python3
"""
sl-pkg -- the source-based package manager from Hell
Copyright (C) 2025 Virtual Nate.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This program is distributed as-is, and WITHOUT WARRANTY OF ANY KIND;
not even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more
details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

__version__ = "0.0.8.5"

import os
import sys
import urllib.request as request
import pathlib
import argparse
import logging
import re
import subprocess
import json
import shutil
import tarfile
from datetime import datetime, timezone
from io import StringIO
from typing import Optional, NoReturn, Union, Self
from multiprocessing import cpu_count
from itertools import zip_longest
from http.client import HTTPException

if sys.platform != "linux":
    raise ValueError("This program requires Linux.")

_log = logging.getLogger(__name__)
START_DIR = pathlib.PosixPath(os.getcwd())
CONFIG_FILE = pathlib.PosixPath("/etc/sl-pkg.json")
MIRROR = "."
CACHE_DIR = pathlib.Path("/tmp/sl-pkg")
USR_CACHE_DIR = pathlib.Path(os.environ["HOME"]) / ".cache" / "sl-pkg"
_PACKAGES_DB = pathlib.Path("/var/lib/sl-pkg/packages.json")
_INSTALLED_PACKAGES_DB = pathlib.Path("/var/lib/sl-pkg/installed_packages.json")
_ALLOWED_PACKAGE_NAMES = re.compile(r"[a-z0-9\-\+]+")
VERBOSE = 0
COMMANDS = {
    "version": "print version information and exit",
    "download": "download packages",
    "install": "install packages",
    "bootstrap": "deploy an LFS system",
}
os.environ["NPROC"] = str(cpu_count())

# sl-pkg uses a custom TarFile extraction filter which is required to
# ensure our source files end up in the right place.
# TarFile.extraction_filter is only available in Python 3.12 and later.
assert hasattr(
    tarfile.TarFile, "extraction_filter"
), "This program requires Python 3.12 or later. Update your Python version."


class VersionNumber:
    __slots__ = ("_version",)

    def _is_compatible(self, other) -> None:
        if isinstance(other, str):
            other = self.convert(other)
        if not isinstance(other, __class__):
            raise TypeError(
                f"can only compare {__name__} to {__name__}, str, bytes, int,"
                f" or float, not {type(other)}."
            )

    def __init__(self, version: Union[str, bytes, int, float]):
        if isinstance(version, bytes):
            version = version.decode("utf-8")
        elif isinstance(version, str):
            pass
        elif isinstance(version, (int, float)):
            version = str(version)
        else:
            raise TypeError(
                "Version numbers must be of type str, bytes, int, or float."
            )
        pattern = re.compile(r"[0-9a-z\.\-]+")
        if not pattern.fullmatch(version):
            raise ValueError(
                "Version strings must only contain digits, lowercase letters, dots, and dashes."
            )
        pattern2 = re.compile(r"[\.\-]{2,}")
        if pattern2.match(version):
            raise ValueError(
                "Version numbers cannot have two or more consecutive delimiters."
            )
        pattern3 = re.compile("^[0-9]")
        if pattern3.match(version):
            raise ValueError("Version numbers must start with a digit.")
        self._version: str = version

    def __repr__(self):
        return self.version

    def __str__(self):
        return self.version

    def __eq__(self, other: Union[Self, str, bytes]) -> bool:
        self._is_compatible(other)

        if self.version == other.version:
            return True

        for point1, point2 in zip_longest(
            self.with_only_numbers().split(),
            other.with_only_numbers().split(),
            fillvalue=0,
        ):
            if int(point1) != int(point2):
                return False
        return True

    def __lt__(self, other: Union[Self, str, bytes]) -> bool:
        self._is_compatible(other)

        for point1, point2 in zip_longest(
            self.with_only_numbers().split(),
            other.with_only_numbers().split(),
            fillvalue=0,
        ):
            if int(point1) < int(point2):
                return True
            elif int(point1) > int(point2):
                return False
        return False

    def __gt__(self, other: Union[Self, str, bytes]) -> bool:
        self._is_compatible(other)

        for point1, point2 in zip_longest(
            self.with_only_numbers().split(),
            other.with_only_numbers().split(),
            fillvalue=0,
        ):
            if int(point1) > int(point2):
                return True
            elif int(point1) < int(point2):
                return False
        return False

    def __le__(self, other: Union[Self, str]) -> bool:
        return self.__lt__(other) or self.__eq__(other)

    def __ge__(self, other: Union[Self, str]) -> bool:
        return self.__gt__(other) or self.__eq__(other)

    @property
    def version(self):
        return self._version

    @classmethod
    def convert(cls, version: Union[str, bytes]):
        return cls(version)

    def split(self) -> list[str]:
        return re.split(r"[\.\-]", self.version)

    def with_only_numbers(self) -> Self:
        pattern = re.compile("[a-z]")
        with StringIO() as version_tmp:
            for i, char in enumerate(self.version):
                if pattern.match(char):
                    version_tmp.write(f".{ord(char)}")
                else:
                    version_tmp.write(char)
            return __class__(version_tmp.getvalue())


def _check_pkg_name(pkg: str):
    """Raise ValueError is the package name does not match the
    _ALLOWED_PACKAGE_NAMES regex.
    """
    _log.debug(f"Making sure {pkg} is a valid name...")
    if not _ALLOWED_PACKAGE_NAMES.fullmatch(pkg):
        raise ValueError(
            f"Invalid package name: {pkg}. Package names can only "
            "contain lowercase letters, numbers, dashes, and plus signs."
        )


def put_installed_pkg(pkg: str) -> None:
    installed_pkg_info = {
        pkg: {
            "VERSION": get_pkgvar(pkg, "VERSION"),
            "DEPENDS": get_pkgvar(pkg, "DEPENDS").split(),
            "BUILD_DEPENDS": get_pkgvar(pkg, "BUILD_DEPENDS").split(),
            "OPTDEPENDS": get_pkgvar(pkg, "OPTDEPENDS").split(),
            "DESCRIPTION": get_pkgvar(pkg, "DESCRIPTION"),
            "ESSENTIAL": True if get_pkgvar(pkg, "ESSENTIAL") == "true" else False,
            "INSTALL_TIME": datetime.now(timezone.utc).isoformat(),
        }
    }
    if not _INSTALLED_PACKAGES_DB.exists():
        _INSTALLED_PACKAGES_DB.parent.mkdir(0o755, True, True)
        with _INSTALLED_PACKAGES_DB.open("w") as fp:
            fp.write(json.dumps(installed_pkg_info))
    else:
        with _INSTALLED_PACKAGES_DB.open("r+") as fp:
            data = json.load(fp)
            data.update(installed_pkg_info)
            # Reset the file pointer back to the beginning
            fp.seek(0)
            json.dump(data, fp)


def get_installed_pkg(pkg: str) -> Optional[dict]:
    if not _INSTALLED_PACKAGES_DB.exists():
        _log.warning(
            f"Tried to query installed package {pkg}, "
            f"but {_INSTALLED_PACKAGES_DB} does not exist."
        )
        return None
    with _INSTALLED_PACKAGES_DB.open() as fp:
        data = json.load(fp)
        return data.get(pkg)


def has_all_deps(pkg: str) -> bool:
    """This will be implemented eventually. For now it always returns True."""
    return True


def print_help(
    command: Optional[str] = None, *, parser: argparse.ArgumentParser
) -> NoReturn:
    if command is None:
        print(f"{parser.prog} -- the package manager from Hell")
        print(f"usage: {parser.prog} [options] COMMAND")
        print("commands:")
        for c, h in COMMANDS.items():
            print(f"  {c}: {h}")
        print("Be careful... you might break your system.")
    else:
        ...
    sys.exit(0)


def read_config(file: pathlib.Path = CONFIG_FILE):
    _log.debug(f"Reading config file {file}...")
    if not file.exists():
        file = pathlib.Path("./sl-pkg.json").resolve()
        if not file.exists():
            raise FileNotFoundError(
                f"Couldn't find config file {file}, "
                "nor could I find a config in the current directory."
            )
    with file.open() as conf:
        data = json.load(conf)
        pattern = re.compile(r"\$\([a-zA-Z0-9_]+\)")
        for var, val in data.items():
            # The above regex tells us if a substring should be treated as
            # an environment variable. Expand these instances.
            # If you try to use pattern.sub here, you're gonna regret it.
            # This is a while loop to ensure variables are expanded even
            # if there's nesting (which there shouldn't be but some people
            # are insane).
            while pattern.search(val) and var != "MIRROR":
                for match in pattern.findall(val):
                    # The use of str.strip is appropriate here because of
                    # the restrictions we put using the regex pattern.
                    val = val.replace(match, os.environ.get(match.strip("$()"), ""))
            if not re.match("[A-Za-z0-9_]+", var.removeprefix("env:")):
                raise ValueError(
                    f"Illegal variable name: {var.removeprefix("env:")}\n"
                    "Variable names can only contain alphanumeric characters and underscores."
                )
            _log.debug(f"{var} = {val}")
            if var.startswith("env:"):
                os.environ[var.removeprefix("env:")] = val
            else:
                globals()[var] = val


def yes_or_no(resp: str) -> bool:
    return resp.lower().startswith("y")


def create_cache_dir(cdir: pathlib.Path):
    _log.debug(f"Creating {cdir} if it does not exist...")
    cdir = pathlib.Path(cdir) if isinstance(cdir, str) else cdir
    try:
        cdir.mkdir(0o755, True, True)
    except FileExistsError:
        _log.exception(
            f"Cannot create cache dir: {cdir} already exists and is not a directory"
        )
        raise
    except OSError as e:
        _log.critical(f"Cannot create cache dir: {e}")
        raise


def passed_inspection(pkg: str) -> bool:
    _check_pkg_name(pkg)
    _log.debug(f"Prompting to inspect {pkg}...")
    pkg_file = (pathlib.Path(CACHE_DIR) / pkg / "PACKAGE").resolve()
    if not pkg_file.exists():
        pkg_file = (pathlib.Path(USR_CACHE_DIR) / pkg / "PACKAGE").resolve()
        if not pkg_file.exists():
            raise FileNotFoundError("Where is the PACKAGE?")
    if yes_or_no(input(f"inspect PACKAGE file for {pkg}? (highly recommended) ")):
        _log.debug(f"Invoking `{os.environ["PAGER"]} '{pkg_file}'`...")
        subprocess.run([os.environ["PAGER"], str(pkg_file)])
        return yes_or_no(input("continue operations? "))
    return True


def _sl_pkg_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo:
    new_attrs = {}
    name = member.name
    dest_path = os.path.realpath(dest_path)
    # Strip leading . and / from filenames.
    if name.startswith((".", "/")):
        name = new_attrs["name"] = member.path.lstrip("./")
    # Also strip the first component from file names
    name = new_attrs["name"] = re.sub(r"^[^\/]+\/", "", name, count=1)
    mode = member.mode
    if mode is not None:
        # No high bits or writing by others allowed.
        mode &= 0o755
        if member.isreg() or member.islnk():
            if not mode & 0o100:
                # Clear executable bits if not executable by owner
                mode &= ~0o111
            # Make sure we can read and write to this
            mode |= 0o600
        elif member.isdir() or member.issym():
            # If an attribute is none it's ignored
            mode = None
        if mode != member.mode:
            new_attrs["mode"] = mode
    # Ignore ownership (this is especially important if we are root)
    if member.uid is not None:
        new_attrs["uid"] = None
    if member.gid is not None:
        new_attrs["gid"] = None
    if member.uname is not None:
        new_attrs["uname"] = None
    if member.gname is not None:
        new_attrs["gname"] = None
    if new_attrs:
        return member.replace(**new_attrs, deep=False)
    return member


def get_pkginfo(pkg: str, is_usr: bool = False) -> None:
    _check_pkg_name(pkg)
    if is_usr:
        base_path = pathlib.Path(USR_CACHE_DIR)
    else:
        base_path = pathlib.Path(CACHE_DIR)
    pkg_dir = (base_path / pkg).resolve()
    pkg_dir.mkdir(0o755, exist_ok=True)
    pkg_file = pkg_dir / "PACKAGE"
    _log.debug(f"Looking for {pkg}...")
    _log.debug(f"Will attempt to retrieve {MIRROR}/{pkg}/PACKAGE.")
    with request.urlopen(f"{MIRROR}/{pkg}/PACKAGE") as resp:
        if resp.status == 404:
            try:
                pkg_dir.rmdir()
            except OSError as e:
                _log.warning(f"directory {pkg_dir} was not removed: {e}")
            raise FileNotFoundError(f"Unable to locate package {pkg}")
        if resp.status != 200:
            raise HTTPException(f"Got unexpected status code {resp.status}.")
        with pkg_file.open("wb") as f:
            f.write(resp.read())
            _log.debug(f"Successfully saved {pkg_file}.")


def get_pkgvar(pkg: str, var: str, is_usr: bool = False) -> str:
    _check_pkg_name(pkg)
    _log.debug(f"Retrieving {var} from {pkg}...")
    if is_usr:
        base_path = pathlib.Path(USR_CACHE_DIR)
    else:
        base_path = pathlib.Path(CACHE_DIR)
    pkg_file = (base_path / pkg / "PACKAGE").resolve()
    if not pkg_file.exists():
        _log.debug(f"{pkg_file} does not exist...")
        get_pkginfo(pkg, is_usr)
    # Prevent hackers from fucking up our system.
    if not re.fullmatch(r"[A-Za-z0-9_]+", var):
        raise ValueError(f"Invalid variable name: {var}")
    # Run through env -i so we don't expose our environment to the shell.
    command = [
        "env",
        "-i",
        "bash",
        "-c",
        "source %s; echo -n ${%s[@]}" % (pkg_file, var),
    ]
    _log.debug(f"Creating sub-process with command line {command}")
    out = subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    _log.debug(f"In {pkg}, {var} = '{out}'.")
    return out


# This function is safe to test on your main machine.
# Don't let it delete or overwrite any of your shit, though.
def download_pkg(
    pkg: str, is_usr: bool = False, dest: Optional[pathlib.Path] = None
) -> pathlib.Path:
    """Retrieve pkg and its patches and save it to dest, or to the default
    cache directory if dest is not specified.

    pkg -- the package to retrieve
    is_usr -- if dest is None, specify if this should be saved to
              USR_CACHE_DIR instead of the global CACHE_DIR.
    dest -- save the package to the specified file name or directory, or
            None to use the default location.
    """
    _check_pkg_name(pkg)
    if is_usr:
        base_path = pathlib.Path(USR_CACHE_DIR)
    else:
        base_path = pathlib.Path(CACHE_DIR)
    if get_pkgvar(pkg, "METAPACKAGE", is_usr) == "true":
        _log.info(f"Not downloading {pkg} because it is a metapackage.")
        return
    dest = pathlib.Path(dest) if isinstance(dest, str) else dest
    if not (dest is None or dest.is_absolute()):
        dest = (START_DIR / dest).resolve()
    if get_pkgvar(pkg, "VERSION", is_usr) == "git":
        if dest is None:
            dest = base_path / pkg / f"{pkg}-git"
        elif dest.is_dir():
            dest /= f"{pkg}-git"
            dest.resolve()
        if not dest.parent.exists():
            _log.debug(f"Creating destination directory {dest.parent}...")
            dest.parent.mkdir(parents=True)
        if dest.exists():
            _log.warning(f"{dest} exists already. Removing...")
            if dest.is_dir():
                shutil.rmtree(str(dest))
            else:
                os.unlink(dest)
        command = [
            "git",
            "clone",
            "--recursive",
            get_pkgvar(pkg, "URL", is_usr),
            str(dest),
        ]
        _log.debug(f"Creating sub-process with command line {command}")
        try:
            subprocess.run(
                command,
                check=True,
            )
        except FileNotFoundError:
            _log.exception("Cannot call git (is git installed?)")
        except subprocess.CalledProcessError as e:
            try:
                os.chdir(dest)
                _log.debug(f"Trying to update git repository for {pkg}...")
                subprocess.run(["git", "pull"], check=True)
            except (OSError, subprocess.CalledProcessError) as e:
                _log.exception(f"Failed to clone or update git repository for {pkg}.")
    else:
        url = get_pkgvar(pkg, "URL", is_usr)
        if dest is None:
            dest = (
                base_path
                / pkg
                / (
                    f"{pkg}-{get_pkgvar(pkg, "VERSION", is_usr)}.tar"
                    f".{pathlib.Path(url).suffix}"
                )
            )
        elif dest.is_dir():
            dest /= (
                f"{pkg}-{get_pkgvar(pkg, "VERSION", is_usr)}.tar"
                f"{pathlib.Path(url).suffix}"
            )
            dest.resolve()
        _log.info(f"Saving {url} to {dest}...")
        with request.urlopen(url) as resp:
            if resp.status != 200:
                raise HTTPException(
                    f"Got status code {resp.status} trying to download {pkg}."
                )
            with dest.open("wb") as f:
                f.write(resp.read())

        for patch_url in get_pkgvar(pkg, "PATCHES", is_usr).split():
            patch_name = pathlib.Path(patch_url).name
            dest = base_path / pkg / patch_name
            if dest.exists():
                _log.warning(f"{dest} exists already. Overwriting...")
            _log.info(f"Obtaining patch {patch_name} for {pkg}...")
            with request.urlopen(patch_url) as resp:
                if resp.status != 200:
                    raise HTTPException(
                        f"Got status code {resp.status} trying to download "
                        f"patch {patch_name} for {pkg}."
                    )
                with dest.open("wb") as f:
                    f.write(resp.read())
    return dest


# This function is also safe to test on your main machine.
def build_pkg(pkg: str, is_usr: bool = False, src: Optional[pathlib.Path] = None):
    _check_pkg_name(pkg)
    if is_usr:
        base_path = pathlib.Path(USR_CACHE_DIR)
    else:
        base_path = pathlib.Path(CACHE_DIR)
    if get_pkgvar(pkg, "METAPACKAGE", is_usr) == "true":
        _log.info(f"Not building {pkg} because it is a metapackage.")
        return
    if (
        get_pkgvar(pkg, "REQUIRES_MANUAL_INTERACTION", is_usr) == "true"
        and not sys.stdin.isatty()
    ):
        raise RuntimeError(f"Package '{pkg}' requires manual interaction to build.")
    src = pathlib.Path(src) if isinstance(src, str) else src
    if not (src is None or src.is_absolute()):
        src = (START_DIR / src).resolve()
    if get_pkgvar(pkg, "VERSION", is_usr) == "git":
        if src is None:
            src = base_path / pkg / f"{pkg}-git"
        elif src.is_dir():
            src /= f"{pkg}-git"
            src.resolve()
        _log.debug(f"Will attempt to build from {src}.")
        if not src.exists():
            raise FileNotFoundError(f"Source directory {src} does not exist.")
        os.chdir(src)
    else:
        if src is None:
            src = base_path / pkg / f"{pkg}-{get_pkgvar(pkg, "VERSION", is_usr)}"
            src.mkdir(0o755, True, True)
            src_glob = list(src.parent.glob("*.tar*"))
            if len(src_glob) > 1:
                raise ValueError(
                    f"I found multiple tar files in {src} and "
                    "I don't know which one to extract."
                )
            elif not src_glob:
                raise FileNotFoundError(
                    f"I can't build anything without a tar file in {src}!"
                )
            src_tar = src_glob[0]
        elif src.is_dir():
            src = src / f"{pkg}-{get_pkgvar(pkg, "VERSION", is_usr)}"
            src.mkdir(0o755, True, True)
            src_glob = list(src.parent.glob("*.tar*"))
            if len(src_glob) > 1:
                raise ValueError(
                    f"I found multiple tar files in {src} and "
                    "I don't know which one to extract."
                )
            elif not src_glob:
                raise FileNotFoundError(
                    f"I can't build anything without a tar file in {src}!"
                )
            src_tar = src_glob[0]
            src_tar.resolve()
        elif src.exists():
            src_tar = src
            src = src_tar.parent / f"{pkg}-{get_pkgvar(pkg, "VERSION", is_usr)}"
            src.mkdir(0o755, True, True)
        else:
            raise FileNotFoundError(f"I could not find {src}.")
        os.chdir(src)
        _log.info(f"Extracting {src_tar}...")
        with tarfile.open(src_tar, debug=VERBOSE) as tf:
            tf.extractall(filter=_sl_pkg_filter)
    os.chdir(src)
    _log.debug(f"The working directory is now {os.getcwd()}.")
    _log.info(f"Preparing to build {pkg}...")
    out = subprocess.run(
        ["bash", "-xc", f"source {base_path / pkg / "PACKAGE"}; prepare"],
        capture_output=(
            get_pkgvar(pkg, "REQUIRES_MANUAL_INTERACTION", is_usr) != "true"
        ),
    )
    if out.stdout is not None:
        with (src / "prepare.log").open("ab") as log:
            log.write(b"Started prepare\nstderr:\n")
            log.write(out.stderr)
            log.write(b"\nstdout:\n")
            log.write(out.stdout)
    if out.returncode != 0:
        raise RuntimeError(
            f"Subprocess returned code {out.returncode}. "
            f"Consult {src / "build.log"} for more information."
        )
    # ensure we're in the correct build directory before going any farther
    if (src / "build").is_dir():
        os.chdir(src / "build")
        _log.debug(f"The working directory is now {os.getcwd()}.")
    _log.info(f"Building {pkg}...")
    out = subprocess.run(
        ["bash", "-xc", f"source {base_path / pkg / "PACKAGE"}; build"],
        capture_output=(
            get_pkgvar(pkg, "REQUIRES_MANUAL_INTERACTION", is_usr) != "true"
        ),
    )
    if out.stdout is not None:
        with (src / "build.log").open("ab") as log:
            log.write(b"Started build\nstderr:\n")
            log.write(out.stderr)
            log.write(b"\nstdout:\n")
            log.write(out.stdout)
    if out.returncode != 0:
        raise RuntimeError(
            f"Subprocess returned code {out.returncode}. "
            f"Consult {src / "build.log"} for more information."
        )
    _log.info(f"{pkg} built successfully.")


# This function is not safe to test on your main machine unless you
# are 100% certain that you're in chroot.
def install_pkg(pkg: str):
    _check_pkg_name(pkg)
    base_path = pathlib.Path(CACHE_DIR)
    version = get_pkgvar(pkg, "VERSION")
    src = base_path / pkg / f"{pkg}-{version}"
    os.chdir(src)
    if (src / "build").is_dir():
        os.chdir(src / "build")
    _log.debug(f"The working directory is now {os.getcwd()}.")
    _log.info(f"Installing {pkg} ({version})...")
    out = subprocess.run(
        ["bash", "-xc", f"source {base_path / pkg / "PACKAGE"}; do_install"],
        capture_output=(
            get_pkgvar(pkg, "REQUIRES_MANUAL_INTERACTION", False) != "true"
        ),
    )
    if out.stdout is not None:
        with (src / "install.log").open("ab") as log:
            log.write(b"Started install\nstderr:\n")
            log.write(out.stderr)
            log.write(b"\nstdout:\n")
            log.write(out.stdout)
    if out.returncode != 0:
        raise RuntimeError(
            f"Subprocess returned code {out.returncode}. "
            f"Consult {src / "install.log"} for more information."
        )
    _log.info(f"Running post-installation script for {pkg}...")
    out = subprocess.run(
        ["bash", "-xc", f"source {base_path / pkg / "PACKAGE"}; postinst"],
        capture_output=(
            get_pkgvar(pkg, "REQUIRES_MANUAL_INTERACTION", False) != "true"
        ),
    )
    if out.stdout is not None:
        with (src / "postinst.log").open("ab") as log:
            log.write(b"Started postinst\nstderr:\n")
            log.write(out.stderr)
            log.write(b"\nstdout:\n")
            log.write(out.stdout)
    if out.returncode != 0:
        _log.warning(
            f"Post-install script for {pkg} returned non-zero status code "
            f"{out.returncode}. Please check {src / "postinst.log"}."
        )
    put_installed_pkg(pkg)


def download_cmd(
    *,
    dry_run: bool = False,
    build: bool = False,
    trust_all: bool = False,
    dest: os.PathLike = START_DIR,
    PACKAGES: list[str],
):
    if not PACKAGES:
        _log.error("no packages specified")
        sys.exit(1)
    for package in PACKAGES:
        # Ensure the pwd is START_DIR before going any farther
        os.chdir(START_DIR)
        get_pkginfo(package, True)
        if not (trust_all or passed_inspection(package)):
            continue
        tar_file = download_pkg(package, True, dest)
        if build:
            try:
                build_pkg(package, True, tar_file)
            except subprocess.CalledProcessError:
                _log.exception(f"{package} failed to build")
    os.chdir(START_DIR)


def install_cmd(
    *,
    dry_run: bool = False,
    keep_going: bool = False,
    trust_all: bool = False,
    force_install: bool = False,
    PACKAGES: list[str],
):
    if os.geteuid():
        raise PermissionError(f"You're not root. I can't let you do that.")
    if not PACKAGES:
        _log.error("no packages specified")
        sys.exit(1)
    for package in PACKAGES:
        os.chdir(START_DIR)
        get_pkginfo(package)
        if not (trust_all or passed_inspection(package)):
            break
        download_pkg(package)
        try:
            build_pkg(package)
        except (OSError, RuntimeError):
            if not force_install:
                if keep_going:
                    _log.exception(f"Failed to build {package}!")
                    continue
                _log.error(f"Failed to build {package}!")
                raise
            _log.exception(f"Failed to build {package}!")
        try:
            install_pkg(package)
        except (OSError, RuntimeError):
            if keep_going:
                _log.exception(f"Failed to install {package}!")
                continue
            _log.error(f"Failed to build {package}!")
            raise
    os.chdir(START_DIR)


def bootstrap(
    *,
    lfs_version: str,
    dry_run: bool = False,
    keep_going: bool = False,
    force_install: bool = False,
    PACKAGES: list[str],
):
    if os.geteuid():
        raise PermissionError(f"You're not root. I can't let you do that.")
    if not PACKAGES:
        raise ValueError(f"no target specified")
    elif len(PACKAGES) > 1:
        raise ValueError(f"extraneous targets specified")
    target = pathlib.Path(PACKAGES[0]).resolve()
    if not target.exists():
        raise FileNotFoundError(f"{target} does not exist.")
    elif not target.is_dir():
        raise NotADirectoryError(f"{target} is not a directory.")
    if not os.path.ismount(target):
        _log.warning(f"{target} is not a mountpoint.")
    PACKAGES.clear()
    _log.debug(f"Looking for LFS {lfs_version}...")
    _log.debug(f"Attempting to retrieve {MIRROR}/base-{lfs_version}/RELEASE.json...")
    with request.urlopen(f"{MIRROR}/base-{lfs_version}/RELEASE.json") as resp:
        if resp.status == 404:
            raise FileNotFoundError(f"Unable to locate release {lfs_version}")
        if resp.status != 200:
            raise HTTPException(f"Got unexpected status code {resp.status}.")
        release_meta = json.load(resp)
        _log.debug(f"Successfully loaded release metadata.")
    with_packages: str = release_meta["WITH_PACKAGES"]
    _log.info("Resolving required packages...")
    _log.debug(
        f"Attempting to retrieve {MIRROR}/base-{lfs_version}" f"/{with_packages}..."
    )
    pkgs_file = pathlib.Path(CACHE_DIR) / with_packages
    with request.urlopen(f"{MIRROR}/base-{lfs_version}/{with_packages}") as resp:
        if resp.status != 200:
            raise HTTPException(f"Got unexpected status code {resp.status}.")
        with pkgs_file.open("wb") as fp:
            fp.write(resp.read())
    tarball_url = release_meta["URL"]
    tarball = (
        pathlib.Path(CACHE_DIR)
        / f"base-{lfs_version}.tar{pathlib.Path(tarball_url).suffix}"
    )
    _log.info("Downloading base tarball...")
    _log.debug(f"Retrieving {tarball_url}...")
    with request.urlopen(tarball_url) as resp:
        if resp.status == 404:
            raise FileNotFoundError(f"Cannot find base tarball.")
        if resp.status != 200:
            raise HTTPException(f"Got unexpected status code {resp.status}.")
        with tarball.open("wb") as fp:
            fp.write(resp.read())
    _log.info("Extracting...")
    # This is very insecure... oh well...
    with tarfile.open(tarball) as tf:
        tf.extractall(f"{target}", filter="tar")

    _log.info("Preparing chroot environment...")
    for d in ["dev", "proc", "sys", "run"]:
        (target / d).mkdir(0o755, exist_ok=True)
    # Mount virtual filesystems
    subprocess.run(["mount", "--bind", "/dev", f"{target}/dev"], check=True)
    subprocess.run(
        [
            "mount",
            "-t",
            "devpts",
            "devpts",
            "-o",
            "gid=5,mode=0620",
            f"{target}/dev/pts",
        ],
        check=True,
    )
    subprocess.run(["mount", "-t", "proc", "proc", f"{target}/proc"], check=True)
    subprocess.run(["mount", "-t", "sysfs", "sysfs", f"{target}/sys"], check=True)
    subprocess.run(["mount", "-t", "tmpfs", "tmpfs", f"{target}/proc"], check=True)
    if pathlib.Path("/dev/shm").is_symlink():
        (target / "dev" / "shm").mkdir(0o1777, exist_ok=True)
    else:
        subprocess.run(
            [
                "mount",
                "-t",
                "tmpfs",
                "tmpfs",
                "-o",
                "nosuid,nodev",
                f"{target}/dev/shm",
            ],
            check=True,
        )
    for file in ["inittab", "profile", "inputrc", "resolv.conf", "hosts", "hostname"]:
        if (pathlib.Path("/etc") / file).exists():
            try:
                shutil.copy(f"/etc/{file}", f"{target}/etc/{file}")
            except shutil.SameFileError:
                pass
    for tree in ["sysconfig", "udev/rules.d"]:
        if (pathlib.Path("/etc") / tree).is_dir():
            (target / tree).mkdir(0o755, True, True)
            shutil.copytree(f"/etc/{tree}", f"{target}/etc/{tree}", dirs_exist_ok=True)
    (target / "etc" / "shells").write_text(
        "# Begin /etc/shells\n\n" "/bin/sh\n" "/bin/bash\n\n" "# End /etc/shells\n"
    )

    _log.info("Entering chroot environment...")
    command = [
        "xargs",
        "-a",
        pkgs_file,
        "chroot",
        str(target),
        "/usr/bin/env",
        "-i",
        "HOME=/root",
        f"TERM={os.getenv("TERM")}",
        "PATH=/usr/bin:/usr/sbin",
        f"MAKEFLAGS={os.getenv("MAKEFLAGS")}",
        f"TESTSUITEFLAGS={os.getenv("TESTSUITEFLAGS")}",
    ]
    if VERBOSE:
        command.append("/usr/bin/python3")
        if VERBOSE > 1:
            command.append("-v")
    command += ["/usr/bin/sl-pkg", "install", "--trust-all"]
    if VERBOSE:
        command.append("-v")
    if keep_going:
        command.append("-k")
    if force_install:
        command.append("--force-install")
    out = subprocess.run(command)
    _log.info("Cleaning up...")
    for d in ["dev", "proc", "sys", "run"]:
        subprocess.run(["umount", "-R", f"{target}/{d}"])
    try:
        shutil.rmtree(f"{target}/var/cache/sl-pkg")
        shutil.rmtree(f"{target}/root/.cache/sl-pkg")
    except OSError:
        _log.warning("Cache not cleared.")
    if out.returncode != 0:
        raise RuntimeError("Bootstrap failed.")
    _log.info("Bootstrap success.")


def main(
    parser: argparse.ArgumentParser,
    **kwargs,
) -> None:
    _log.debug(f"Starting {parser.prog} version {__version__}...")
    _log.debug(f"Command line: {sys.argv}")
    if kwargs["help"]:
        print_help(kwargs["COMMAND"], parser=parser)

    to_delete = set()
    for kwarg, val in kwargs.items():
        if not val:
            to_delete.add(kwarg)
    for kwarg in to_delete:
        del kwargs[kwarg]
    if "verbose" in kwargs:
        del kwargs["verbose"]
    if not sys.stdout.isatty():
        _log.warning(
            "sl-pkg does not have a stable CLI interface. Use with caution in scripts."
        )
    command = kwargs["COMMAND"]
    del kwargs["COMMAND"]

    create_cache_dir(CACHE_DIR)
    create_cache_dir(USR_CACHE_DIR)
    match command.lower():
        case "install":
            install_cmd(**kwargs)
        case "version":
            print(f"{parser.prog} {__version__}")
            sys.exit(0)
        case "download":
            download_cmd(**kwargs)
        case "bootstrap":
            bootstrap(**kwargs)
        case _:
            raise ValueError(f"unrecognized command {command}")
    os.chdir(START_DIR)


if __name__ == "__main__":
    # We want our own help option here so add_help must be False.
    parser = argparse.ArgumentParser(
        prog="sl-pkg",
        description="Scratch Linux Packager -- The package manager from Hell",
        add_help=False,
        exit_on_error=False,
    )
    parser.add_argument(
        "-h", "--help", action="store_true", help="show this help message and exit"
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        "--simulate",
        action="store_true",
        help="do not change the system; only simulate what would happen",
    )
    parser.add_argument(
        "-b",
        "--build",
        action="store_true",
        help="when downloading, also build the source package",
    )
    parser.add_argument(
        "-k",
        "--keep-going",
        action="store_true",
        help="keep going even if some packages fail to build or install",
    )
    parser.add_argument(
        "--trust-all",
        action="store_true",
        help="do not prompt to inspect PACKAGE files",
    )
    parser.add_argument(
        "--lfs-version", help="when bootstrapping, use this version of LFS as the base"
    )
    parser.add_argument(
        "--force-install",
        action="store_true",
        help="attempt to install a package even if the build fails (dangerous)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="say what is being done (specify twice for even more verbose)",
    )
    parser.add_argument(
        "-d", "--dest", "--destination", help="when downloading, save to this directory"
    )
    parser.add_argument("COMMAND")
    parser.add_argument("PACKAGES", nargs="*")

    try:
        args = parser.parse_args()
    except argparse.ArgumentError as e:
        if str(e).endswith("COMMAND"):
            if not ("-h" in sys.argv or "--help" in sys.argv):
                print(f"{parser.prog}: fatal: no command specified")
                print(f"try {parser.prog} --help")
                sys.exit(2)
            else:
                print_help(parser=parser)
        else:
            print(e)
            parser.print_usage()
            sys.exit(2)
    VERBOSE = args.verbose
    if not VERBOSE:
        log_level = logging.INFO
    elif VERBOSE == 1:
        log_level = logging.DEBUG
    elif VERBOSE > 1:
        log_level = logging.NOTSET
    logging.basicConfig(
        stream=sys.stderr,
        format="%(name)s: %(levelname)s: %(message)s",
        level=log_level,
    )
    read_config(CONFIG_FILE)
    main(parser, **vars(args))
