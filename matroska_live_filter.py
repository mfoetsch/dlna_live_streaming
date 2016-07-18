#!/usr/bin/python
# -*- coding: utf-8 -*-

# Rewrite a Matroska input file to a Matroska file suitable for live streaming
# Copyright (C) 2010  Johannes Sasongko <sasongko@gmail.com>
# Copyright (C) 2011  Michael Fötsch <foetsch@yahoo.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
#
#
# The developers of the Exaile media player hereby grant permission
# for non-GPL compatible GStreamer and Exaile plugins to be used and
# distributed together with GStreamer and Exaile. This permission is
# above and beyond the permissions granted by the GPL license by which
# Exaile is covered. If you modify this code, you may extend this
# exception to your version of the code, but you are not obligated to
# do so. If you do not wish to do so, delete this exception statement
# from your version.


# This code is heavily based on public domain code by "Omion" (from the
# Hydrogenaudio forums), as obtained from Matroska's Subversion repository at
# revision 858 (2004-10-03), under "/trunk/Perl.Parser/MatroskaParser.pm".


# Changes:
#    2011-02-08  Michael Fötsch  <foetsch@yahoo.com>
#
#    * Print formatted element tree of input file on-the-fly.
#
#    * Add classes for writing Matroska files.
#
#    * Write out a Matroska file suitable for live streaming (fixed Segment size,
#    fixed duration, no SeekHead or Cue segments).


import StringIO
import sys
from struct import pack, unpack

SINT, UINT, FLOAT, STRING, UTF8, DATE, MASTER, BINARY = range(8)

# In the pretty-printed structure, don't print more than this number of
# repeated elements of a certain type.
MAX_NUM_REPEATED_ELEMS = 3

# HACK: Write a Void segment of size PADDING_SIZE after this many Clusters.
# When I'm live streaming, I might not have as much data in the file yet as the
# DLNA player wants to read ahead. In these cases, the player appears to become
# impatient waiting for the data and stops playback. In order to avoid this,
# I make sure that there's lots of (junk) data for it to read.
NUM_CLUSTERS_BEFORE_PADDING = 1
PADDING_SIZE = 128 * 1024

class EbmlException(Exception): pass

class BinaryData(str): pass
class UnknownData: pass

# Base class for elements with writing capability.
class BaseElement:
    def __init__(self, elemID):
        self.elemID = elemID
        if elemID == 0:
            self.elemName = ""
        else:
            self.elemName = MatroskaTags[elemID][0]
            
    def writeElement(self, f):
        f.write(self.writeID())
        data = self.writeData()
        f.write(self.writeOwnSize(len(data)))
        f.write(data)
        
    def writeOwnSize(self, childSize):
        return self.writeSize(childSize)
        
    def writeSize(self, size):
        # Size fields can be written as 1 to 8 bytes, depending on the value.
        # For simplicity, we write all sizes either as 4 or 8 bytes. 
        if size <= (1 << 28) - 2:
            # The first byte has upper bits 0001, remaining bits are size in big endian.
            return pack(">L", (1 << 28) | size)
        else:
            # The first byte has upper bits 0000 0001, remaining bits are size in big endian.
            return pack(">LL", (1 << 24) | (size / 4294967296), size % 4294967296)

    def writeInteger(self, value):
        # Integers can be written as 1 to 8 bytes. For simplicity, we write
        # either 4 or 8 bytes.
        if value < (1 << 32):
            return pack(">L", value)
        else:
            return pack(">LL", value / 4294967296, value % 4294967296)

    def writeFloat(self, value):
        # Floats can be written with single, double, or extended precision.
        # For simplicity, we always write double precision.
        return pack("@d", value)[::-1]
                        # Need to reverse the bytes for little-endian machines

    def writeID(self):
        if self.elemID >= 0x10000000:
            # 4-byte ID
            return pack(">L", self.elemID)
        elif self.elemID >= 0x200000:
            # 3-byte ID
            return pack(">BH", (self.elemID & 0xFF0000) >> 16, self.elemID & 0xFFFF)
        elif self.elemID >= 0x4000:
            # 2-byte ID
            return pack(">H", self.elemID)
        elif self.elemID >= 0x80:
            # 1-byte ID
            return chr(self.elemID)
        else:
            raise EbmlException("Can't write element ID 0x%X for element %s"
                                % (self.elemID, self.elemName)) 

    def __str__(self):
        if self.elemID == 0:
            return ""
        return self.elemName

# Base class for Master elements, i.e., elements with children.
class MasterElementBase(BaseElement):
    def __init__(self, elemID):
        BaseElement.__init__(self, elemID)
        self.childElems = []
        
    def addChildElem(self, childElem):
        self.childElems.append(childElem)
        
    def writeData(self):
        data = StringIO.StringIO()
        for child in self.childElems:
            child.writeElement(data)
        return data.getvalue()
        
    def __str__(self):
        childStr = []
        for child in self.childElems:
            childStr.append(str(child))
        if childStr:
            return BaseElement.__str__(self) + "\n" + "\n".join(childStr)
        else:
            return BaseElement.__str__(self)
        
# Special Master element for "Segment"
class SegmentElement(MasterElementBase):
    def __init__(self, elemID):
        MasterElementBase.__init__(self, elemID)
        self.hasCluster = False
        
    def writeOwnSize(self, size):
        # Always pretend the largest possible segment size, so that the DLNA
        # player keeps on reading. The EBML specs define -1 as "unknown",
        # but this might lead to the player trying to seek through the entire
        # file in order to determine the real size.
        return self.writeSize(2**56 - 2)

# Special Master element for "Info".
class InfoElement(MasterElementBase):
    def __init__(self, elemID):
        MasterElementBase.__init__(self, elemID)
        self.durationAdded = False

    def addChildElem(self, childElem):
        if childElem.elemName == "Duration":
            # Skip Duration now. Will be added in writeData().
            return
        MasterElementBase.addChildElem(self, childElem)
        
    def writeData(self):
        if not self.durationAdded:
            # Pretend a duration of 100 hours.
            MasterElementBase.addChildElem(
                self, FloatElement(0x4489, 100 * 60 * 60 * 1000.0))
            self.durationAdded = True
        return MasterElementBase.writeData(self)
        
def MasterElement(elemID):
    special_elements = {0x18538067: SegmentElement,
                        0x1549a966: InfoElement}
    return special_elements.get(elemID, MasterElementBase)(elemID)
        
class IntElement(BaseElement):
    def __init__(self, elemID, type_, value):
        BaseElement.__init__(self, elemID)
        self.type_ = type_  #  (SINT, UINT, DATE)
        self.value = value
        
    def writeData(self):
        return self.writeInteger(self.value)
        
    def __str__(self):
        typeStr = {SINT: "Int", UINT: "Uint", DATE: "Date"}
        return "%s: %s = %s" % (BaseElement.__str__(self), typeStr[self.type_], self.value)
        
class FloatElement(BaseElement):
    def __init__(self, elemID, value):
        BaseElement.__init__(self, elemID)
        self.value = value
        
    def writeData(self):
        return self.writeFloat(self.value)

    def __str__(self):
        return "%s: Float = %s" % (BaseElement.__str__(self), self.value)
        
class StringElement(BaseElement):
    def __init__(self, elemID, value):
        BaseElement.__init__(self, elemID)
        self.value = value
        
    def writeData(self):
        return self.value.encode("ascii")
        
    def __str__(self):
        return "%s: String = %r" % (BaseElement.__str__(self), self.value)
        
class Utf8Element(BaseElement):
    def __init__(self, elemID, value):
        BaseElement.__init__(self, elemID)
        self.value = value
        
    def writeData(self):
        return self.value.encode("utf-8")
        
    def __str__(self):
        return "%s: UTF-8 = %r" % (BaseElement.__str__(self), self.value)
        
class BinaryElement(BaseElement):
    def __init__(self, elemID, value):
        BaseElement.__init__(self, elemID)
        self.value = value
        
    def writeData(self):
        return self.value

    def __str__(self):
        return "%s: Binary = %s" % (BaseElement.__str__(self), len(self.value))

# Master element to collect level-0 elements. (Doesn't exist in EBML. In EBML,
# that's just the file.)        
class RootElement(MasterElementBase):
    def __init__(self):
        MasterElementBase.__init__(self, 0)
        
    def writeElement(self, f):
        f.write(self.writeData())
        
class Ebml:
    """EBML parser.

    Usage: Ebml(location, tags).parse()
    tags is a dictionary of the form { id: (name, type) }.
    """

    ## Constructor and destructor

    def __init__(self, location, tags):
        self.tags = tags
        self.open(location)
        self.live_mode = (location == "-")
        if self.live_mode:
            self.stdout = open("/dev/stdout", "wb")
        self.clusterIdx = 0        

    def __del__(self):
        self.close()

    ## File access.
    ## These can be overridden to provide network support.

    def open(self, location):
        """Open a location and set self.size."""
        self.file = f = open(location, 'rb')
        f = self.file
        f.seek(0, 2)
        self.size = f.tell()
        f.seek(0, 0)

    def seek(self, offset, mode):
        self.file.seek(offset, mode)

    def tell(self):
        return self.file.tell()

    def read(self, length):
        return self.file.read(length)

    def close(self):
        self.file.close()

    ## Element reading

    def readSize(self):
        b1 = self.read(1)
        b1b = ord(b1)
        if b1b & 0x80:
            # 1 byte
            return b1b & 0x7f
        elif b1b & 0x40:
            # 2 bytes
            # JS: BE-ushort
            return unpack(">H", chr(0x40 ^ b1b) + self.read(1))[0]
        elif b1b & 0x20:
            # 3 bytes
            # JS: BE-ulong
            return unpack(">L", "\0" + chr(0x20 ^ b1b) + self.read(2))[0]
        elif b1b & 0x10:
            # 4 bytes
            # JS: BE-ulong
            return unpack(">L", chr(0x10 ^ b1b) + self.read(3))[0]
        elif b1b & 0x08:
            # 5 bytes
            # JS: uchar BE-ulong. We change this to BE uchar ulong.
            high, low = unpack(">BL", chr(0x08 ^ b1b) + self.read(4))
            return high * 4294967296 + low
        elif b1b & 0x04:
            # 6 bytes
            # JS: BE-slong BE-ulong
            high, low = unpack(">HL", chr(0x04 ^ b1b) + self.read(5))
            return high * 4294967296 + low
        elif b1b & 0x02:
            # 7 bytes
            # JS: BE-ulong BE-ulong
            high, low = unpack(">LL",
                    "\0" + chr(0x02 ^ b1b) + self.read(6))
            return high * 4294967296 + low
        elif b1b & 0x01:
            # 8 bytes
            # JS: BE-ulong BE-ulong
            high, low = unpack(">LL", chr(0x01 ^ b1b) + self.read(7))
            return high * 4294967296 + low
        else:
            raise EbmlException(
                    "invalid element size with leading byte 0x%X" % b1b)

    def readInteger(self, length):
        if length == 1:
            # 1 byte
            return ord(self.read(1))
        elif length == 2:
            # 2 bytes
            return unpack(">H", self.read(2))[0]
        elif length == 3:
            # 3 bytes
            return unpack(">L", "\0" + self.read(3))[0]
        elif length == 4:
            # 4 bytes
            return unpack(">L", self.read(4))[0]
        elif length == 5:
            # 5 bytes
            high, low = unpack(">BL", self.read(5))
            return high * 4294967296 + low
        elif length == 6:
            # 6 bytes
            high, low = unpack(">HL", self.read(6))
            return high * 4294967296 + low
        elif length == 7:
            # 7 bytes
            high, low = unpack(">LL", "\0" + (self.read(7)))
            return high * 4294967296 + low
        elif length == 8:
            # 8 bytes
            high, low = unpack(">LL", self.read(8))
            return high * 4294967296 + low
        else:
            raise EbmlException(
                    "don't know how to read %r-byte integer" % length)

    def readFloat(self, length):
        # Need to reverse the bytes for little-endian machines
        if length == 4:
            # single
            return unpack('@f', self.read(4)[::-1])[0]
        elif length == 8:
            # double
            return unpack('@d', self.read(8)[::-1])[0]
        elif length == 10:
            # extended (don't know how to handle it)
            return 'EXTENDED'
        else:
            raise EbmlException("don't know how to read %r-byte float" % length)

    def readID(self):
        b1 = self.read(1)
        if not b1:
            raise EOFError()
        b1b = ord(b1)
        if b1b & 0x80:
            # 1 byte
            return b1b
        elif b1b & 0x40:
            # 2 bytes
            return unpack(">H", chr(b1b) + self.read(1))[0]
        elif b1b & 0x20:
            # 3 bytes
            return unpack(">L", "\0" + chr(b1b) + self.read(2))[0]
        elif b1b & 0x10:
            # 4 bytes
            return unpack(">L", chr(b1b) + self.read(3))[0]
        else:
            raise EbmlException(
                    "invalid element ID with leading byte 0x%X" % b1b)

    ## Parsing

    def parse(self, from_=0, to=None, parentElem=None, indent_level=0,
              silent=False):
        """Parses EBML from `from_` to `to`.

        Note that not all streams support seeking backwards, so prepare to handle
        an exception if you try to parse from arbitrary position.
        """
        if to is None:
            to = self.size
        self.seek(from_, 0)
        node = {}
        last_key_freq = ("", 0)
        # Iterate over current node's children.
        while self.tell() < to:
            start_ofs = self.tell()
            try:
                id = self.readID()
            except EOFError:
                return parentElem
            except EbmlException, e:
                # Invalid EBML header. We can't reliably get any more data from
                # this level, so just return anything we have.
                print >>sys.stderr, "ERROR:", e
                return node
            size = self.readSize()
            try:
                key, type_ = self.tags[id]
            except KeyError:
                self.seek(size, 1)
            else:
                suppress_print = silent
                if key == last_key_freq[0]:
                    # If this is the same element type as the last, check how
                    # many elements of this type we have printed already.
                    # In the pretty-printed structure, we don't want to see
                    # thousands of Clusters, for example.
                    last_key_freq = (last_key_freq[0], last_key_freq[1] + 1)
                    if last_key_freq[1] > MAX_NUM_REPEATED_ELEMS:
                        suppress_print = True
                else:
                    # This is a different element type than the last. Print the
                    # number of suppressed elements.
                    if not silent:
                        suppress_print = False
                        if (last_key_freq[1] > 0):
                            print >> sys.stderr, " " * indent_level, "+", \
                                last_key_freq[1] - MAX_NUM_REPEATED_ELEMS - 1, last_key_freq[0]
                    last_key_freq = (key, 0)
                    
                if not suppress_print:
                    print >> sys.stderr, " " * indent_level, "%s (size = %s, ofs = %s):" % (
                                                            key, size, start_ofs),                                                        
                try:
                    if type_ is MASTER:
                        tell = self.tell()
                        if not suppress_print:
                            print >> sys.stderr, "first child ofs = %s" % self.tell()

                        masterElem = MasterElement(id)

                        if self.live_mode and masterElem.elemName == "Segment":
                            # We have reached the Segment. Write out all elements
                            # up to now and write the Segment itself (which, at
                            # this point, just consists of its ID and fake size).
                            parentElem.writeElement(self.stdout)
                            masterElem.writeElement(self.stdout)

                        # Parse child elements recursively.
                        value = self.parse(tell, tell + size, parentElem=masterElem,
                                           indent_level=indent_level + 1,
                                           silent=suppress_print)

                        if self.live_mode and parentElem.elemName == "Segment":
                            if value.elemName == "Cluster":
                                # HACK: See comment for NUM_CLUSTERS_BEFORE_PADDING.
                                if (self.clusterIdx % NUM_CLUSTERS_BEFORE_PADDING) == 0:
                                    voidElem = BinaryElement(0xec, "\0" * PADDING_SIZE)
                                    value.addChildElem(voidElem)
                                self.clusterIdx += 1
                            # Write out the child element, unless it's a SeekHead.
                            # We don't want the DLNA player to seek inside the
                            # file that we're just writing.
                            if value.elemName != "SeekHead":
                                value.writeElement(self.stdout)
                        elif self.live_mode:
                            parentElem.addChildElem(masterElem)
                    elif type_ in (SINT, UINT, DATE):
                        value = self.readInteger(size)
                        if not suppress_print:
                            print >> sys.stderr, "int =", value
                        parentElem.addChildElem(IntElement(id, type_, value))
                    elif type_ is FLOAT:
                        value = self.readFloat(size)
                        if not suppress_print:
                            print >> sys.stderr, "float =", value
                        parentElem.addChildElem(FloatElement(id, value))
                    elif type_ is STRING:
                        value = unicode(self.read(size), 'ascii')
                        if not suppress_print:
                            print >> sys.stderr, "string =", repr(value)
                        parentElem.addChildElem(StringElement(id, value))
                    elif type_ is UTF8:
                        value = unicode(self.read(size), 'utf-8')
                        if not suppress_print:
                            print >> sys.stderr, "utf8 =", repr(value)
                        parentElem.addChildElem(Utf8Element(id, value))
                    elif type_ is BINARY:
                        value = BinaryData(self.read(size))
                        if not suppress_print:
                            print >> sys.stderr, "binary =", " ".join([hex(ord(x)) for x in value[:4]]), "..."
                        parentElem.addChildElem(BinaryElement(id, value))
                    else:
                        assert False
                except (EbmlException, UnicodeDecodeError), e:
                    print >>sys.stderr, "WARNING:", e
        if not silent and last_key_freq[1] > MAX_NUM_REPEATED_ELEMS:
            print >> sys.stderr, " " * indent_level, "+", \
                last_key_freq[1] - MAX_NUM_REPEATED_ELEMS - 1, last_key_freq[0]
        return parentElem


## GIO-specific code

import gio

class GioEbml(Ebml):
    # NOTE: All seeks are faked using InputStream.skip because we need to use
    # BufferedInputStream but it does not implement Seekable.

    def open(self, location):
        f = gio.File(location)
        self.buffer = gio.BufferedInputStream(f.read())
        self._tell = 0

        self.size = f.query_info('standard::size').get_size()

    def seek(self, offset, mode):
        if mode == 0:
            skip = offset - self._tell
        elif mode == 1:
            skip = offset
        elif mode == 2:
            skip = self.size - self._tell + offset
        else:
            raise ValueError("invalid seek mode: %r" % offset)
        if skip < 0:
            raise gio.Error("cannot seek backwards from %d" % self._tell)
        self._tell += skip
        self.buffer.skip(skip)

    def tell(self):
        return self._tell

    def read(self, length):
        result = self.buffer.read(length)
        self._tell += len(result)
        return result

    def close(self):
        self.buffer.close()
        
class StdinEbml(Ebml):
    def open(self, location):
        self._tell = 0
        self.size = 2**64
    
    def seek(self, offset, mode):
        if mode == 0:
            skip = offset - self._tell
        elif mode == 1:
            skip = offset
        else:
            raise ValueError("invalid seek mode: %r" % mode)
        if skip < 0:
            raise IOError("cannot seek backwards from %d" % self._tell)

        while skip:
            size = min(skip, 1024 * 1024)
            sys.stdin.read(size)
            skip -= size
            self._tell += size
        
    def tell(self):
        return self._tell
    
    def read(self, length):
        result = sys.stdin.read(length)
        self._tell += len(result)
        return result

    def close(self):
        pass


## Matroska-specific code

# Interesting Matroska tags.
# Tags not defined here are skipped while parsing.
MatroskaTags = {
    # EBML Header
    0x1a45dfa3: ('EBML', MASTER),
    0x4286: ('EBMLVersion', UINT),
    0x42f7: ('EBMLReadVersion', UINT),
    0x42f2: ('EBMLMaxIDLength', UINT),
    0x42f3: ('EBMLMaxSizeLength', UINT),
    0x4282: ('DocType', STRING),
    0x4287: ('DocTypeVersion', UINT),
    0x4285: ('DocTypeReadVersion', UINT),
    # Global elements (used everywhere in the format)
    0xbf: ('CRC-32', BINARY),
    0xec: ('Void', BINARY),
    # signature
    0x1b538667: ('SignatureSlot', MASTER),
    0x7e8a: ('SignatureAlgo', UINT),
    0x7e9a: ('SignatureHash', UINT),
    0x7ea5: ('SignaturePublicKey', BINARY),
    0x7eb5: ('Signature', BINARY),
    0x7e5b: ('SignatureElements', MASTER),
    0x7e7b: ('SignatureElementList', MASTER),
    0x6532: ('SignedElement', BINARY),
    # end of signature
    # Element Name
    # Segment
    0x18538067: ('Segment', MASTER),
    # Meta Seek Information
    0x114d9b74: ('SeekHead', MASTER),
    0x4dbb: ('Seek', MASTER),
    0x53ab: ('SeekID', BINARY),
    0x53ac: ('SeekPosition', UINT),
    # Segment Information
    0x1549a966: ('Info', MASTER),
    0x73a4: ('SegmentUID', BINARY),
    0x7384: ('SegmentFilename', UTF8),
    0x3cb923: ('PrevUID', BINARY),
    0x3c83ab: ('PrevFilename', UTF8),
    0x3eb923: ('NextUID', BINARY),
    0x3e83bb: ('NextFilename', UTF8),
    0x4444: ('SegmentFamily', BINARY),
    0x6924: ('ChapterTranslate', MASTER),
    0x69fc: ('ChapterTranslateEditionUID', UINT),
    0x69bf: ('ChapterTranslateCodec', UINT),
    0x69a5: ('ChapterTranslateID', BINARY),
    0x2ad7b1: ('TimecodeScale', UINT),
    0x4489: ('Duration', FLOAT),
    0x4461: ('DateUTC', DATE),
    0x7ba9: ('Title', UTF8),
    0x4d80: ('MuxingApp', UTF8),
    0x5741: ('WritingApp', UTF8),
    # Cluster
    0x1f43b675: ('Cluster', MASTER),
    0xe7: ('Timecode', UINT),
    0x5854: ('SilentTracks', MASTER),
    0x58d7: ('SilentTrackNumber', UINT),
    0xa7: ('Position', UINT),
    0xab: ('PrevSize', UINT),
    0xa3: ('SimpleBlock', BINARY),
    0xa0: ('BlockGroup', MASTER),
    0xa1: ('Block', BINARY),
    0xa2: ('BlockVirtual', BINARY),
    0x75a1: ('BlockAdditions', MASTER),
    0xa6: ('BlockMore', MASTER),
    0xee: ('BlockAddID', UINT),
    0xa5: ('BlockAdditional', BINARY),
    0x9b: ('BlockDuration', UINT),
    0xfa: ('ReferencePriority', UINT),
    0xfb: ('ReferenceBlock', SINT),
    0xfd: ('ReferenceVirtual', SINT),
    0xa4: ('CodecState', BINARY),
    0x8e: ('Slices', MASTER),
    0xe8: ('TimeSlice', MASTER),
    0xcc: ('LaceNumber', UINT),
    0xcd: ('FrameNumber', UINT),
    0xcb: ('BlockAdditionID', UINT),
    0xce: ('Delay', UINT),
    0xcf: ('Duration', UINT),
    0xaf: ('EncryptedBlock', BINARY),
    # Track
    0x1654ae6b: ('Tracks', MASTER),
    0xae: ('TrackEntry', MASTER),
    0xd7: ('TrackNumber', UINT),
    0x73c5: ('TrackUID', UINT),
    0x83: ('TrackType', UINT),
    0xb9: ('FlagEnabled', UINT),
    0x88: ('FlagDefault', UINT),
    0x55aa: ('FlagForced', UINT),
    0x9c: ('FlagLacing', UINT),
    0x6de7: ('MinCache', UINT),
    0x6df8: ('MaxCache', UINT),
    0x23e383: ('DefaultDuration', UINT),
    0x23314f: ('TrackTimecodeScale', FLOAT),
    0x537f: ('TrackOffset', SINT),
    0x55ee: ('MaxBlockAdditionID', UINT),
    0x536e: ('Name', UTF8),
    0x22b59c: ('Language', STRING),
    0x86: ('CodecID', STRING),
    0x63a2: ('CodecPrivate', BINARY),
    0x258688: ('CodecName', UTF8),
    0x7446: ('AttachmentLink', UINT),
    0x3a9697: ('CodecSettings', UTF8),
    0x3b4040: ('CodecInfoURL', STRING),
    0x26b240: ('CodecDownloadURL', STRING),
    0xaa: ('CodecDecodeAll', UINT),
    0x6fab: ('TrackOverlay', UINT),
    0x6624: ('TrackTranslate', MASTER),
    0x66fc: ('TrackTranslateEditionUID', UINT),
    0x66bf: ('TrackTranslateCodec', UINT),
    0x66a5: ('TrackTranslateTrackID', BINARY),
    # video
    0xe0: ('Video', MASTER),
    0x9a: ('FlagInterlaced', UINT),
    0x53b8: ('StereoMode', UINT),
    0xb0: ('PixelWidth', UINT),
    0xba: ('PixelHeight', UINT),
    0x54aa: ('PixelCropBottom', UINT),
    0x54bb: ('PixelCropTop', UINT),
    0x54cc: ('PixelCropLeft', UINT),
    0x54dd: ('PixelCropRight', UINT),
    0x54b0: ('DisplayWidth', UINT),
    0x54ba: ('DisplayHeight', UINT),
    0x54b2: ('DisplayUnit', UINT),
    0x54b3: ('AspectRatioType', UINT),
    0x2eb524: ('ColourSpace', BINARY),
    0x2fb523: ('GammaValue', FLOAT),
    0x2383e3: ('FrameRate', FLOAT),
    # end video
    # audio
    0xe1: ('Audio', MASTER),
    0xb5: ('SamplingFrequency', FLOAT),
    0x78b5: ('OutputSamplingFrequency', FLOAT),
    0x9f: ('Channels', UINT),
    0x7d7b: ('ChannelPositions', BINARY),
    0x6264: ('BitDepth', UINT),
    # end audio
    # content encoding
    0x80: ('ContentEncodings', MASTER),
    0x6240: ('ContentEncoding', MASTER),
    0x5031: ('ContentEncodingOrder', UINT),
    0x5032: ('ContentEncodingScope', UINT),
    0x5033: ('ContentEncodingType', UINT),
    0x5034: ('ContentCompression', MASTER),
    0x4254: ('ContentCompAlgo', UINT),
    0x4255: ('ContentCompSettings', BINARY),
    0x5035: ('ContentEncryption', MASTER),
    0x47: ('ContentEncAlgo', UINT),
    0x47: ('ContentEncKeyID', BINARY),
    0x47: ('ContentSignature', BINARY),
    0x47: ('ContentSigKeyID', BINARY),
    0x47: ('ContentSigAlgo', UINT),
    0x47: ('ContentSigHashAlgo', UINT),
    # end content encoding
    0xe2: ('TrackOperation', MASTER),
    0xe3: ('TrackCombinePlanes', MASTER),
    0xe4: ('TrackPlane', MASTER),
    0xe5: ('TrackPlaneUID', UINT),
    0xe6: ('TrackPlaneType', UINT),
    0xe9: ('TrackJoinBlocks', MASTER),
    0xed: ('TrackJoinUID', UINT),
    # Cueing Data
    0x1c53bb6b: ('Cues', MASTER),
    0xbb: ('CuePoint', MASTER),
    0xb3: ('CueTime', UINT),
    0xb7: ('CueTrackPositions', MASTER),
    0xf7: ('CueTrack', UINT),
    0xf1: ('CueClusterPosition', UINT),
    0x5378: ('CueBlockNumber', UINT),
    0xea: ('CueCodecState', UINT),
    0xdb: ('CueReference', MASTER),
    0x96: ('CueRefTime', UINT),
    0x97: ('CueRefCluster', UINT),
    0x535f: ('CueRefNumber', UINT),
    0xeb: ('CueRefCodecState', UINT),
    # Attachment
    0x1941a469: ('Attachments', MASTER),
    0x61a7: ('AttachedFile', MASTER),
    0x467e: ('FileDescription', UTF8),
    0x466e: ('FileName', UTF8),
    0x4660: ('FileMimeType', STRING),
    0x465c: ('FileData', BINARY),
    0x46ae: ('FileUID', UINT),
    0x4675: ('FileReferral', BINARY),
    # Chapters
    0x1043a770: ('Chapters', MASTER),
    0x45b9: ('EditionEntry', MASTER),
    0x45bc: ('EditionUID', UINT),
    0x45bd: ('EditionFlagHidden', UINT),
    0x45db: ('EditionFlagDefault', UINT),
    0x45dd: ('EditionFlagOrdered', UINT),
    0xb6: ('ChapterAtom', MASTER),
    0x73c4: ('ChapterUID', UINT),
    0x91: ('ChapterTimeStart', UINT),
    0x92: ('ChapterTimeEnd', UINT),
    0x98: ('ChapterFlagHidden', UINT),
    0x4598: ('ChapterFlagEnabled', UINT),
    0x6e67: ('ChapterSegmentUID', BINARY),
    0x6ebc: ('ChapterSegmentEditionUID', BINARY),
    0x63c3: ('ChapterPhysicalEquiv', UINT),
    0x8f: ('ChapterTrack', MASTER),
    0x89: ('ChapterTrackNumber', UINT),
    0x80: ('ChapterDisplay', MASTER),
    0x85: ('ChapString', UTF8),
    0x437c: ('ChapLanguage', STRING),
    0x437e: ('ChapCountry', STRING),
    0x6944: ('ChapProcess', MASTER),
    0x6955: ('ChapProcessCodecID', UINT),
    0x450d: ('ChapProcessPrivate', BINARY),
    0x6911: ('ChapProcessCommand', MASTER),
    0x6922: ('ChapProcessTime', UINT),
    0x6933: ('ChapProcessData', BINARY),
    # Tagging
    0x1254c367: ('Tags', MASTER),
    0x7373: ('Tag', MASTER),
    0x63c0: ('Targets', MASTER),
    0x68ca: ('TargetTypeValue', UINT),
    0x63ca: ('TargetType', STRING),
    0x63c5: ('TrackUID', UINT),
    0x63c9: ('EditionUID', UINT),
    0x63c4: ('ChapterUID', UINT),
    0x63c6: ('AttachmentUID', UINT),
    0x67c8: ('SimpleTag', MASTER),
    0x45a3: ('TagName', UTF8),
    0x447a: ('TagLanguage', STRING),
    0x4484: ('TagDefault', UINT),
    0x4487: ('TagString', UTF8),
    0x4485: ('TagBinary', BINARY)
}

def parse(location):
    if location == "-":
        ebml = StdinEbml(location, MatroskaTags)
    else:
        ebml = GioEbml(location, MatroskaTags)
    return ebml.parse(parentElem=RootElement())

def dump(location):
    parse(location)

def dump_tags(location):
    from pprint import pprint
    mka = parse(location)
    segment = mka['Segment'][0]
    info = segment['Info'][0]
    length = info['Duration'][0] * info['TimecodeScale'][0] / 1e9
    print >> sys.stderr, "Length = %f seconds" % length
    pprint(segment['Tags'][0]['Tag'])

if __name__ == '__main__':
    import sys
    location = sys.argv[1]
    if sys.platform == 'win32' and '://' not in location:
        # XXX: This is most likely a bug in the Win32 GIO port; it converts
        # paths into UTF-8 and requires them to be specified in UTF-8 as well.
        # Here we decode the path according to the FS encoding to get the
        # Unicode representation first. If the path is in a different encoding,
        # this step will fail.
        location = location.decode(sys.getfilesystemencoding()).encode('utf-8')
    dump(location)


# vi: et sts=4 sw=4 ts=4
