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
from typing import Optional, NoReturn, Union, Self
from multiprocessing import cpu_count
from itertools import zip_longest

if sys.platform != "linux":
    raise ValueError("This program requires Linux.")

__version__ = "0.0.5.1"
_log = logging.getLogger(__name__)
START_DIR = pathlib.PosixPath(os.getcwd())
CONFIG_FILE = pathlib.PosixPath("/etc/sl-pkg.json")
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
                f"can only compare {__name__} to {__name__}, str, bytes, int, \
or float, not {type(other)}."
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


def read_config():
    if not CONFIG_FILE.exists():
        try:
            CONFIG_FILE = pathlib.PosixPath("./sl-pkg.conf").resolve()
        except FileNotFoundError as e:
            _log.critical("no config file found")


def install(
    *,
    dry_run: bool = False,
    keep_going: bool = False,
    trust_all: bool = False,
    force_install: bool = False,
):
    pass


def main(
    parser: argparse.ArgumentParser,
    **kwargs,
) -> None:
    if kwargs["help"]:
        print_help(kwargs["COMMAND"], parser=parser)

    to_delete = set()
    for kwarg, val in kwargs.items():
        if not val:
            to_delete.add(kwarg)
    for kwarg in to_delete:
        del kwargs[to_delete]
    del kwargs["verbose"]  # we don't need to pass this to other commands

    if not sys.stdout.isatty():
        _log.warning(
            "sl-pkg does not have a stable CLI interface. Use with caution in scripts."
        )
    match kwargs["COMMAND"]:
        case "install":
            install(**kwargs)


if __name__ == "__main__":
    # we want our own help option here so add_help must be False
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
    parser.add_argument("COMMAND")
    parser.add_argument("PACKAGES", nargs="*")
    logging.basicConfig(stream=sys.stderr, format="{name}: {levelname}: {message}")
    try:
        args = parser.parse_args()
    except argparse.ArgumentError as e:
        if str(e).endswith("COMMAND"):
            if not ("-h" in sys.argv or "--help" in sys.argv):
                _log.critical("no command specified")
                _log.critical(f"try {parser.prog} --help")
                sys.exit(2)
            else:
                print_help(parser=parser)
        else:
            print(e)
            parser.print_usage()
            sys.exit(2)
    read_config()
    main(parser, **vars(args))
