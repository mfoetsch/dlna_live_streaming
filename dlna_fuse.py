#!/usr/bin/python
# -*- coding: utf-8 -*-

# Present a live capture of the desktop as a file to a DLNA streaming server. 
# Copyright 2011 Michael Fötsch <foetsch@yahoo.com>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   1. Redistributions of source code must retain the above copyright notice,
#      this list of conditions and the following disclaimer.
#   2. Redistributions in binary form must reproduce the above copyright notice,
#      this list of conditions and the following disclaimer in the documentation
#      and/or other materials provided with the distribution.
#   3. The name of the author may not be used to endorse or promote products
#      derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR IMPLIED
# WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
# EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import errno
import fuse
import os
import signal
import stat
import subprocess
# import tempfile
# import threading
import time

fuse.fuse_python_api = (0, 2)

# # The captured video is saved to a temporary file. TODO: Instead of writing
# # to a file, keep some kind of FIFO queue in memory. As the player progresses
# # through the file, throw out old parts of the file that it has already read.
# # For now, storing a temporary file makes things easier to debug and keeps the
# # code simple.
# TEMP_FILE = os.path.expanduser("~/Videos/live.mkv")

FRAME_RATE = "20"
VIDEO_SIZE = "1024x576"
X_OFF = "128"
Y_OFF = "224"
OTHER_OPTS = ""

class MyStat(fuse.Stat):
    def __init__(self):
        self.st_mode = stat.S_IFDIR | 0755
        self.st_ino = 0         # handled by FUSE
        self.st_dev = 0         # handled by FUSE
        self.st_nlink = 2       # a directory has two links: itself and ".."
        self.st_uid = 0
        self.st_gid = 0
        self.st_size = 4096
        self.st_atime = 0
        self.st_mtime = 0
        self.st_ctime = 0
        
# # Producer thread that reads data from a stream, writes it to a file (if given),
# # and notifies other threads via a condition variable.
# class ReadThread(threading.Thread):
#     class Output:
#         def __init__(self):
#             self.outputFile = tempfile.NamedTemporaryFile("wb", 0, ".mkv")
#             self.fileSize = 0
#
#     def __init__(self, process, strm, output, condition):
#         threading.Thread.__init__(self)
#         self.process = process
#         self.strm = strm
#         self.output = output
#         self.condition = condition
#
#     def run(self):
#         while self.process.poll() is None:
#             data = self.strm.read(4096)
#             if data and self.output:
#                 if self.condition:
#                     self.condition.acquire()
#                 try:
#                     self.output.outputFile.write(data)
#                     self.output.outputFile.flush()
#                     self.output.fileSize += len(data)
#                     if self.condition:
#                         self.condition.notify_all()
#                 finally:
#                     if self.condition:
#                         self.condition.release()
#         print "Process has exited with code", self.process.returncode
#         if self.condition:
#             self.condition.acquire()
#             self.output.fileSize = -1
#             self.condition.notify_all()
#             self.condition.release()

class DlnaFuse(fuse.Fuse):
    def __init__(self, *args, **kw):
        fuse.Fuse.__init__(self, *args, **kw)

        # self.output = ReadThread.Output()
        # self.hasMoreData = threading.Condition()
        self.refcnt = 0

    def open(self, path, flags):
        print "open", path, "%x"%flags
        accmode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
        if (flags & accmode) != os.O_RDONLY:
            return -errno.EACCES

        if self.refcnt == 0:
            pulseaudio_monitor = os.popen("pactl list | grep -A2 '^Source #'"
                " | grep 'Name: .*\.monitor$' | awk '{print $NF}'"
                " | tail -n1").read().strip()

            print "Using PulseAudio monitor", pulseaudio_monitor

            live_filter = os.path.abspath(os.path.join(os.path.dirname(__file__),
                "matroska_live_filter.py"))

            cmd = ("parec -d %(pulseaudio_monitor)s | ffmpeg -f s16le -ac 2"
                " -ar 44100 -i - -f x11grab -r "+FRAME_RATE+" -s "+VIDEO_SIZE+
                " "+OTHER_OPTS+" -i :0.0+"+X_OFF+","+Y_OFF+" -acodec ac3 -ac 1"
                " -vcodec libx264 -preset fast -pix_fmt yuv420p -threads 0"
                " -f matroska - 2> /dev/null #| %(live_filter)s - 2> /dev/null"
                ) % locals()

            print "Running capture command:", cmd
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                            # stderr=subprocess.PIPE,
                                            shell=True)

            # t = ReadThread(self.process, self.process.stdout,
            #                self.output, self.hasMoreData)
            # t.setDaemon(True)
            # t.start()
            #
            # stderr_thrd = ReadThread(self.process, self.process.stderr, None,
            #                                                             None)
            # stderr_thrd.setDaemon(True)
            # stderr_thrd.start()
            #
            # # Read some data so that it's ready immediately when the player requests
            # # it (my player appears to have a short timeout and gives up when it
            # # has to wait too long for the metadata).
            # self.read("", 1024 * 1024, 0)
        self.refcnt += 1
        print "The refcnt is now", self.refcnt

        # st = fuse.FuseFileInfo()
        # setattr(st, "keep_cache", False)
        # setattr(st, "direct_io", True)
        # return st

    def release(self, path, flags):
        print "release", path, "%x"%flags
        self.refcnt -= 1
        if self.refcnt == 0:
            while self.process.poll() == None:
                saved_handler = signal.signal(signal.SIGTERM, signal.SIG_IGN)
                os.killpg(os.getpgrp(), signal.SIGTERM)
                signal.signal(signal.SIGTERM, saved_handler)
        print "The refcnt is now", self.refcnt

    def getattr(self, path):
        print "getattr", path
        st = MyStat()
        st.st_atime = int(time.time())
        st.st_mtime = st.st_atime
        st.st_ctime = st.st_atime
        pe = path.split(os.path.sep)[1:]
        if path == "/":         # root of the FUSE filesystem
            pass
        elif pe[-1] in ["a", "b", "c"]: # first level is directories
            pass
        elif len(pe) == 2 and pe[-1] == "fuse_live.mkv":
            st.st_mode = stat.S_IFREG | 0666
            st.st_nlink = 1
            st.st_size = 1024**3 #self.fileSize
        else:
            return -errno.ENOENT
        return st

    def readdir(self, path, offset):
        print "readdir", path, offset
        dirents = [".", ".."]
        if path == "/":
            dirents.extend(["a", "b", "c"])
        else:
            dirents.extend(["fuse_live.mkv"])
        for r in dirents:
            yield fuse.Direntry(r)

    def mknod(self, path, mode, dev):
        print "mknod", path, mode, dev
        return -errno.EROFS

    def write(self, path, buf, offset):
        print "write", path, buf, offset
        return -errno.EROFS

    def read(self, path, size, offset):
        print "read", path, size, offset
        # neededSize = offset + size
        # self.hasMoreData.acquire()
        # try:
        #     while self.output.fileSize < neededSize + 4096:
        #         if self.output.fileSize == -1:
        #             print "Capture thread has exited"
        #             return ""
        #         # If the player reads more than 10 MB ahead, this is probably
        #         # an error. We cannot satisfy the request without waiting a
        #         # (potentially) long time for the data to accumulate.
        #         if neededSize > self.output.fileSize + 10 * 1024 * 1024:
        #             print "!!!! seeking 10MB ahead; signaling end of file"
        #             return ""
        #         print "need to wait for file size", neededSize, "have", self.output.fileSize
        #         self.hasMoreData.wait()
        #     f = open(self.output.outputFile.name, "rb", 0)
        #     f.seek(offset, 0)
        #     data = f.read(size)
        # finally:
        #     self.hasMoreData.release()
        # return data
        return self.process.stdout.read(size)

def main():
    server = DlnaFuse(version="%prog " + fuse.__version__,
                      usage="Run with './dlna_fuse -s -f <mount_point>' "
                            "to start live desktop capture",
                      dash_s_do="setsingle")
    server.parse(errex=1)
    old_handler = signal.signal(signal.SIGINT, signal.SIG_DFL)
    server.main()
    signal.signal(signal.SIGINT, old_handler)

if __name__ == "__main__":
    main()
