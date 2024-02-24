"""
1:N rsync implementation on top of execnet.

(c) 2006-2009, Armin Rigo, Holger Krekel, Maciej Fijalkowski
"""
from __future__ import annotations

import os
import stat
from hashlib import md5
from queue import Queue
from typing import Callable
from typing import Literal

import execnet.rsync_remote
from execnet.gateway import Gateway
from execnet.gateway_base import BaseGateway
from execnet.gateway_base import Channel


class RSync:
    """This class allows to send a directory structure (recursively)
    to one or multiple remote filesystems.

    There is limited support for symlinks, which means that symlinks
    pointing to the sourcetree will be send "as is" while external
    symlinks will be just copied (regardless of existence of such
    a path on remote side).
    """

    def __init__(self, sourcedir, callback=None, verbose: bool = True) -> None:
        self._sourcedir = str(sourcedir)
        self._verbose = verbose
        assert callback is None or callable(callback)
        self._callback = callback
        self._channels: dict[Channel, Callable[[], None] | None] = {}
        self._receivequeue: Queue[
            tuple[
                Channel,
                (
                    None
                    | tuple[Literal["send"], tuple[list[str], bytes]]
                    | tuple[Literal["list_done"], None]
                    | tuple[Literal["ack"], str]
                    | tuple[Literal["links"], None]
                    | tuple[Literal["done"], None]
                ),
            ]
        ] = Queue()
        self._links: list[tuple[Literal["linkbase", "link"], str, str]] = []

    def filter(self, path: str) -> bool:
        return True

    def _end_of_channel(self, channel: Channel) -> None:
        if channel in self._channels:
            # too early!  we must have got an error
            channel.waitclose()
            # or else we raise one
            raise OSError(f"connection unexpectedly closed: {channel.gateway} ")

    def _process_link(self, channel: Channel) -> None:
        for link in self._links:
            channel.send(link)
        # completion marker, this host is done
        channel.send(42)

    def _done(self, channel: Channel) -> None:
        """Call all callbacks"""
        finishedcallback = self._channels.pop(channel)
        if finishedcallback:
            finishedcallback()
        channel.waitclose()

    def _list_done(self, channel: Channel) -> None:
        # sum up all to send
        if self._callback:
            s = sum([self._paths[i] for i in self._to_send[channel]])
            self._callback("list", s, channel)

    def _send_item(
        self,
        channel: Channel,
        modified_rel_path_components: list[str],
        checksum: bytes,
    ) -> None:
        """Send one item"""
        modifiedpath = os.path.join(self._sourcedir, *modified_rel_path_components)
        try:
            f = open(modifiedpath, "rb")
            data = f.read()
        except OSError:
            data = None

        # provide info to progress callback function
        modified_rel_path = "/".join(modified_rel_path_components)
        if data is not None:
            self._paths[modified_rel_path] = len(data)
        else:
            self._paths[modified_rel_path] = 0
        if channel not in self._to_send:
            self._to_send[channel] = []
        self._to_send[channel].append(modified_rel_path)
        # print "sending", modified_rel_path, data and len(data) or 0, checksum

        if data is not None:
            f.close()
            if checksum is not None and checksum == md5(data).digest():
                data = None  # not really modified
            else:
                self._report_send_file(channel.gateway, modified_rel_path)
        channel.send(data)

    def _report_send_file(self, gateway: BaseGateway, modified_rel_path: str) -> None:
        if self._verbose:
            print(f"{gateway} <= {modified_rel_path}")

    def send(self, raises: bool = True) -> None:
        """Sends a sourcedir to all added targets. Flag indicates
        whether to raise an error or return in case of lack of
        targets
        """
        if not self._channels:
            if raises:
                raise OSError(
                    "no targets available, maybe you " "are trying call send() twice?"
                )
            return
        # normalize a trailing '/' away
        self._sourcedir = os.path.dirname(os.path.join(self._sourcedir, "x"))
        # send directory structure and file timestamps/sizes
        self._send_directory_structure(self._sourcedir)

        # paths and to_send are only used for doing
        # progress-related callbacks
        self._paths: dict[str, int] = {}
        self._to_send: dict[Channel, list[str]] = {}

        # send modified file to clients
        while self._channels:
            channel, req = self._receivequeue.get()
            if req is None:
                self._end_of_channel(channel)
            else:
                if req[0] == "links":
                    self._process_link(channel)
                elif req[0] == "done":
                    self._done(channel)
                elif req[0] == "ack":
                    if self._callback:
                        self._callback("ack", self._paths[req[1]], channel)
                elif req[0] == "list_done":
                    self._list_done(channel)
                elif req[0] == "send":
                    self._send_item(channel, req[1][0], req[1][1])
                else:
                    assert "Unknown command %s" % req[0]  # type: ignore[unreachable]

    def add_target(
        self,
        gateway: Gateway,
        destdir: str | os.PathLike[str],
        finishedcallback: Callable[[], None] | None = None,
        **options,
    ) -> None:
        """Adds a remote target specified via a gateway
        and a remote destination directory.
        """
        for name in options:
            assert name in ("delete",)

        def itemcallback(req) -> None:
            self._receivequeue.put((channel, req))

        channel = gateway.remote_exec(execnet.rsync_remote)
        channel.reconfigure(py2str_as_py3str=False, py3str_as_py2str=False)
        channel.setcallback(itemcallback, endmarker=None)
        channel.send((str(destdir), options))
        self._channels[channel] = finishedcallback

    def _broadcast(self, msg: object) -> None:
        for channel in self._channels:
            channel.send(msg)

    def _send_link(
        self,
        linktype: Literal["linkbase", "link"],
        basename: str,
        linkpoint: str,
    ) -> None:
        self._links.append((linktype, basename, linkpoint))

    def _send_directory(self, path: str) -> None:
        # dir: send a list of entries
        names = []
        subpaths = []
        for name in os.listdir(path):
            p = os.path.join(path, name)
            if self.filter(p):
                names.append(name)
                subpaths.append(p)
        mode = os.lstat(path).st_mode
        self._broadcast([mode, *names])
        for p in subpaths:
            self._send_directory_structure(p)

    def _send_link_structure(self, path: str) -> None:
        sourcedir = self._sourcedir
        basename = path[len(self._sourcedir) + 1 :]
        linkpoint = os.readlink(path)
        # On Windows, readlink returns an extended path (//?/) for
        # absolute links, but relpath doesn't like mixing extended
        # and non-extended paths. So fix it up ourselves.
        if (
            os.path.__name__ == "ntpath"
            and linkpoint.startswith("\\\\?\\")
            and not self._sourcedir.startswith("\\\\?\\")
        ):
            sourcedir = "\\\\?\\" + self._sourcedir
        try:
            relpath = os.path.relpath(linkpoint, sourcedir)
        except ValueError:
            relpath = None
        if (
            relpath is not None
            and relpath not in (os.curdir, os.pardir)
            and not relpath.startswith(os.pardir + os.sep)
        ):
            self._send_link("linkbase", basename, relpath)
        else:
            # relative or absolute link, just send it
            self._send_link("link", basename, linkpoint)
        self._broadcast(None)

    def _send_directory_structure(self, path: str) -> None:
        try:
            st = os.lstat(path)
        except OSError:
            self._broadcast((None, 0, 0))
            return
        if stat.S_ISREG(st.st_mode):
            # regular file: send a mode/timestamp/size pair
            self._broadcast((st.st_mode, st.st_mtime, st.st_size))
        elif stat.S_ISDIR(st.st_mode):
            self._send_directory(path)
        elif stat.S_ISLNK(st.st_mode):
            self._send_link_structure(path)
        else:
            raise ValueError(f"cannot sync {path!r}")
