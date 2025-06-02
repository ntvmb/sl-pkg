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

__version__ = "0.0.5.4"

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
from io import StringIO
from typing import Optional, NoReturn, Union, Self, Literal
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
_ALLOWED_PACKAGE_NAMES = re.compile(r"[a-z0-9\-\+]+")
COMMANDS = {
    "version": "print version information and exit",
    "download": "download packages",
    "install": "install packages",
    "bootstrap": "deploy an LFS system",
}
os.environ["NPROC"] = str(cpu_count())


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
    if not _ALLOWED_PACKAGE_NAMES.fullmatch(pkg):
        raise ValueError(
            f"Invalid package name: {pkg}. Package names can only "
            "contain lowercase letters, numbers, dashes, and plus signs."
        )


def print_help(
    command: Optional[str] = None, *, parser: argparse.ArgumentParser
) -> NoReturn:
    if command is None:
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
    pkg_file = (pathlib.Path(pkg) / "PACKAGE").resolve()
    if not pkg_file.exists():
        raise FileNotFoundError("Where is the PACKAGE?")
    if yes_or_no(input(f"inspect PACKAGE file for {pkg}? (highly recommended) ")):
        _log.debug(f"Invoking `{os.environ["PAGER"]} '{pkg_file}'`...")
        subprocess.run([os.environ["PAGER"], str(pkg_file)])
        return yes_or_no(input("continue operations? "))
    return True


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


def get_pkgvar(pkg: str, var: str, is_usr: bool = False):
    _check_pkg_name(pkg)
    if is_usr:
        base_path = pathlib.Path(USR_CACHE_DIR)
    else:
        base_path = pathlib.Path(CACHE_DIR)
    pkg_file = (base_path / pkg / "PACKAGE").resolve()
    if not pkg_file.exists():
        get_pkginfo(pkg, is_usr)
    os.chdir(base_path)
    # Prevent hackers from fucking up our system.
    if not re.fullmatch(r"[A-Za-z0-9_]+", var):
        raise ValueError(f"Invalid variable name: {var}")
    out = subprocess.run(
        ["bash", "-c", "source %s; echo -n ${%s[@]}" % (pkg_file, var)],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    return out


def download_pkg(pkg: str):
    _check_pkg_name(pkg)
    ...


def download_cmd(
    *,
    dry_run: bool = False,
    build: bool = False,
    trust_all: bool = False,
    PACKAGES: list[str],
):
    raise NotImplementedError


def install_cmd(
    *,
    dry_run: bool = False,
    keep_going: bool = False,
    trust_all: bool = False,
    force_install: bool = False,
    PACKAGES: list[str],
):
    raise NotImplementedError


def bootstrap(
    *,
    lfs_version: str,
    dry_run: bool = False,
    keep_going: bool = False,
    force_install: bool = False,
    PACKAGES: list[str],
):
    TARGET = PACKAGES[0]
    raise NotImplementedError


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
        action="store_true",
        default=0,
        help="say what is being done",
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
    if args.verbose:
        log_level = logging.INFO
    else:
        log_level = logging.DEBUG
    logging.basicConfig(
        stream=sys.stderr,
        format="%(name)s: %(levelname)s: %(message)s",
        level=log_level,
    )
    read_config(CONFIG_FILE)
    main(parser, **vars(args))
