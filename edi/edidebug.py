#!/usr/bin/env python2
#
# Decode EDI for debugging

import sys
import struct

from crc import crc16
from reedsolo import RSCodec

class Printer:
    def __init__(self):
        self.indent = 0
    def pr(self, s):
        print(" " * self.indent + s)
    def hexpr(self, header, seq):
        if isinstance(seq, str):
            seq = bytearray(seq)

        print(header + ": " + " ".join("{0:02x}".format(el) for el in seq))

    def inc(self):
        self.indent += 1
    def dec(self):
        self.indent -= 1

p = Printer()

# keys=findex
defragmenters = {}

def decode(stream):
    p.pr("start")
    success = False

    pos = stream.tell()
    sync = stream.read(2)
    stream.seek(pos)

    if len(sync) < 2:
        p.pr("EOF")
        return False

    if sync == "PF":
        if decode_pft(stream):
            p.pr("PFT decode success")
            success = True
        else:
            p.pr("PFT decode fail")
    elif sync == "AF":
        if decode_af(stream, is_stream=True):
            p.pr("AF decode success")
            success = True
        else:
            p.pr("AF decode fail")
    return success


pft_head_struct = "!2sH3B3BH"
pft_rs_head_struct = "!2B"
pft_addr_head_struct = "!2H"
def decode_pft(stream):
    p.inc()
    p.pr("start decoding PF")

    headerdata = stream.read(12)
    header = struct.unpack(pft_head_struct, headerdata)

    psync, pseq, findex1, findex2, findex3, fcount1, fcount2, fcount3, fec_ad_plen = header

    findex = (findex1 << 16) | (findex2 << 8) | findex3
    fcount = (fcount1 << 16) | (fcount2 << 8) | fcount3

    fec = (fec_ad_plen & 0x8000) != 0x00
    addr = (fec_ad_plen & 0x4000) != 0x00
    plen = fec_ad_plen & 0x3FFF

    # try to sync according to TS 102 821 Clause 7.4.1
    if psync != "PF":
        p.pr("No PF Sync")
        p.dec()
        return False

    rs_k = 0
    rs_z = 0
    if fec:
        rs_head = stream.read(2)
        rs_k, rs_z = struct.unpack(pft_rs_head_struct, rs_head)
        headerdata += rs_head

    addr_source = 0
    addr_dest   = 0
    if addr:
        addr_head = stream.read(4)
        addr_source, addr_dest = struct.unpack(pft_addr_head_struct, addr_head)
        headerdata += addr_head

    # read CRC
    crc = struct.unpack("!H", stream.read(2))[0]

    crc_calc = crc16(headerdata)
    crc_calc ^= 0xFFFF

    crc_ok = crc_calc == crc

    if crc_ok:
        p.pr("CRC ok")
    else:
        p.pr("CRC not ok!")
        p.pr("  read 0x{:04x}, calculated 0x{:04x}".format(crc, crc_calc))

    p.pr("pseq {}".format(pseq))
    p.pr("findex {}".format(findex))
    p.pr("fcount {}".format(fcount))
    if fec:
        p.pr("with fec:")
        p.pr(" RSk={}".format(rs_k))
        p.pr(" RSz={}".format(rs_z))
    if addr:
        p.pr("with transport header:")
        p.pr(" source={}".format(addr_source))
        p.pr(" dest={}".format(addr_dest))
    p.pr("payload length={}".format(plen))

    payload = stream.read(plen)

    success = False
    if crc_ok and fec:
        # Fragmentation and
        # Reed solomon decode
        if pseq not in defragmenters:
            defragmenters[pseq] = Defragmenter(fcount, get_rs_decoder(rs_k, rs_z))
        success = defragmenters[pseq].push_fragment(findex, payload)
    elif crc_ok and fcount > 1:
        # Fragmentation
        if pseq not in defragmenters:
            defragmenters[pseq] = Defragmenter(fcount, decode_af_fragments)
        success = defragmenters[pseq].push_fragment(findex, payload)
    elif crc_ok and fcount == 1:
        success = decode_af(payload)

    p.dec()
    return success

class Defragmenter():
    def __init__(self, fcount, callback):
        self.fragments = [None for i in range(fcount)]
        self.fcount = fcount
        self.cb = callback

    def push_fragment(self, findex, fragment):
        self.fragments[findex] = fragment

        received_fragments = [f for f in self.fragments if f is not None]
        if len(received_fragments) >= self.fcount:
            return self.cb(received_fragments)
            # The RS decoder should be able to handle partial lists
            # of fragments. The AF decoder cannot

        return True

    def __repr__(self):
        return "<Defragmenter with fcount={}>".format(self.fcount)

def get_rs_decoder(rs_k, rs_z):
    p.pr("Build RS decoder for RSk={}, RSz={}".format(rs_k, rs_z))
    def decode_rs(fragments):
        # Transpose fragments to get an RS block
        rs_blocks = zip(*fragments)

        rs_codec = RSCodec(48)

        # chunks have size RSk + 48
        # The tuples here are (data, parity)

        rs_chunks = [ ( "".join(rs_block[:rs_k]),
                        "".join(rs_block[rs_k:rs_k+48]) )
                    for rs_block in rs_blocks]

        assert(False)

        for chunk, ecc in rs_chunks:
            p.pr("ECC")
            p.hexpr(" orig", ecc)
            p.hexpr(" calc", rs_codec.encode(bytearray(chunk)))

        afpacket = "".join(data for (data, parity) in rs_chunks)

        return afpacket

    return decode_rs


af_head_struct = "!2sLHBc"
def decode_af_fragments(fragments):
    return decode_af("".join(fragments))

def decode_af(in_data, is_stream=False):
    p.pr("AF Packet")
    p.inc()

    if is_stream:
        headerdata = in_data.read(10)
    else:
        headerdata = in_data[:10]

    sync, plen, seq, ar, pt = struct.unpack(af_head_struct, headerdata)

    if sync != "AF":
        p.pr("No AF Sync")
        p.dec()
        return False

    crc_flag = (ar & 0x80) != 0x00
    revision = ar & 0x7F

    if is_stream:
        payload = in_data.read(plen)
        crc = struct.unpack("!H", in_data.read(2))[0]
    else:
        payload = in_data[10:10+plen]
        crc = struct.unpack("!H", in_data[10+plen:10+plen+2])[0]

    crc_calc = crc16(headerdata)
    crc_calc = crc16(payload, crc_calc)
    crc_calc ^= 0xFFFF

    crc_ok = crc_calc == crc

    if crc_flag and crc_ok:
        p.pr("CRC ok")
    elif crc_flag:
        p.pr("CRC not ok!")
        p.pr(" CRC: is 0x{0:04x}, calculated 0x{1:04x}".format(crc, crc_calc))
    else:
        p.pr("No CRC")

    p.pr("plen {}".format(plen))
    p.pr("seq {}".format(seq))
    p.pr("revision {}".format(revision))
    p.pr("protocol type {}".format(pt))

    success = False
    if pt == "T":
        success = decode_tag(payload)

    p.dec()
    return success

tag_item_head_struct = "!4sL"
def tagitems(tagpacket):
    i = 0
    while i+8 < len(tagpacket):
        name, length = struct.unpack(tag_item_head_struct, tagpacket[i:i+8])

        # length is in bits, because it's more annoying this way
        assert(length % 8 == 0)
        length /= 8

        tag_value = tagpacket[i+8:i+8+length]
        yield {'name': name, 'length': length, 'value': tag_value}

        i += 8 + length

def decode_tag(tagpacket):
    p.pr("Tag packet len={}".format(len(tagpacket)))
    p.inc()
    for item in tagitems(tagpacket):
        if item['name'] == "deti":
            decode_deti(item)
        elif item['name'].startswith("est"):
            decode_estn(item)
        else:
            p.pr("Tag item '{}' ({})".format(item['name'], item['length']))

    p.dec()
    return True

item_deti_header_struct = "!BBBBH"
def decode_deti(item):
    p.pr("TAG item {} ({})".format(item['name'], item['length']))
    p.inc()
    tag_value = item['value']

    unpacked = struct.unpack(item_deti_header_struct, tag_value[:6])
    flag_fcth, fctl, stat, mid_fp, mnsc = unpacked


    atstf = flag_fcth & 0x80 != 0
    if atstf:
        utco, seconds, tsta1, tsta2, tsta3 = struct.unpack("!BL3B", tag_value[6:6+8])
        tsta = (tsta1 << 16) | (tsta2 << 8) | tsta3

    ficf  = flag_fcth & 0x40 != 0
    rfudf = flag_fcth & 0x20 != 0
    fcth  = flag_fcth & 0x1F
    fct = fcth << 5 | fctl

    mid = (mid_fp >> 6) & 0x03
    fp  = (mid_fp >> 3) & 0x07


    p.pr("FICF        = {}".format(ficf))
    p.pr("ATST        = {}".format(atstf))
    p.pr("RFUDF       = {}".format(rfudf))
    p.pr("FCT         = {} (0x{:02x} 0x{:02x})".format(fct, fcth, fctl))
    p.pr("STAT        = 0x{:02x}".format(stat))
    p.pr("Mode id     = {}".format(mid))
    p.pr("Frame phase = {}".format(fp))
    p.pr("MNSC        = 0x{:02x}".format(mnsc))
    if atstf:
        p.pr("UTCOffset   = {}".format(utco))
        p.pr("Seconds     = {}".format(seconds))
        p.pr("TSTA        = {}".format(tsta))


    len_fic = len(tag_value) - 2 - 4

    if atstf:
        len_fic -= 8

    if rfudf:
        len_fic -= 3

    p.pr("FIC data len  {}".format(len_fic))

    p.dec()

item_estn_head_struct = "!HB"
def decode_estn(item):
    estN = chr(ord("0") + ord(item['name'][3]))
    p.pr("TAG item EST{} ({})".format(estN, item['length']))
    p.inc()
    tag_value = item['value']

    scid_sad, tpl_rfa = struct.unpack(item_estn_head_struct, tag_value[:3])
    scid = scid_sad >> 10
    sad  = scid_sad & 0x3F
    tpl  = tpl_rfa >> 2

    p.pr("SCID = {}".format(scid))
    p.pr("SAD  = {}".format(sad))
    p.pr("TPL  = {}".format(tpl))

    assert(item['length'] == len(tag_value))

    p.pr("MST len = {}".format(len(tag_value) - 3))

    p.dec()

if len(sys.argv) < 2:
    print("Filename expected")
    sys.exit(1)

filename = sys.argv[1]

edi_fd = open(filename, "r")
while decode(edi_fd):
    pass

