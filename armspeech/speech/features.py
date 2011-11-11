"""Representation and I/O for acoustic features."""

# Copyright 2011 Matt Shannon

# This file is part of armspeech.
# See `License` for details of license and warranty.


from __future__ import division
from __future__ import with_statement

import os
import sys
import struct
import numpy as np
import itertools
import subprocess

def readParamFile(paramFile, paramOrder, decode = None):
    floatLittleEndian = struct.Struct('<'+''.join([ 'f' for i in range(paramOrder) ]))
    with open(paramFile, 'rb') as f:
        while True:
            bytes = f.read(paramOrder * 4)
            if bytes == '':
                break
            curr = list(floatLittleEndian.unpack(bytes))
            yield curr if decode == None else decode(curr)
# (FIXME : use instead of above??)
# (N.B. perhaps surprisingly, it seems quite a bit slower (43 sec vs 60 sec in one test)
#   than readParamFile!)
def readParamFileAlt(paramFile, paramOrder):
    return np.reshape(np.fromfile(paramFile, dtype = np.float32), (-1, paramOrder))

def writeParamFile(outSeq, paramFile, paramOrder, encode = None):
    floatLittleEndian = struct.Struct('<'+''.join([ 'f' for i in range(paramOrder) ]))
    with open(paramFile, 'wb') as f:
        for out in outSeq:
            curr = out if encode == None else encode(out)
            bytes = floatLittleEndian.pack(*curr)
            f.write(bytes)

class NoneEncoder(object):
    def __init__(self):
        self.decode = None
        self.encode = None

class Stream(object):
    def __init__(self, name, order, encoder = NoneEncoder()):
        self.name = name
        self.order = order
        self.encoder = encoder

    def __repr__(self):
        return 'Stream('+repr(self.name)+', '+repr(self.order)+', '+repr(self.encoder)+')'

def readAcousticGen(streams, paramFileFor):
    return itertools.izip(*[
        readParamFile(paramFileFor(stream), stream.order, stream.encoder.decode)
        for stream in streams
    ])

def writeAcousticSeq(outSeq, streams, paramFileFor):
    for stream, outSeqStream in zip(streams, zip(*outSeq)):
        writeParamFile(outSeqStream, paramFileFor(stream), stream.order, stream.encoder.encode)

class Msd01Encoder(object):
    def __init__(self, specialValue):
        self.specialValue = specialValue

    def decode(self, xs):
        if len(xs) != 1:
            raise RuntimeError('encoded vector should be 1-dimensional')
        x = xs[0]
        if x == self.specialValue:
            return 0, None
        else:
            return 1, x
    def encode(self, value):
        comp, x = value
        if comp == 0:
            return [self.specialValue]
        else:
            return [x]

def doHtsDemoWaveformGeneration(scriptsDir, synthOutDir, basenames, logFile = None):
    """HTS-demo-with-STRAIGHT-style waveform generation.

    N.B. assumes files to synthesize are <basename>.{mgc,lf0,bap} in synthOutDir.
    Also assumes a matching Config.pm configuration file.
    """
    args = ['/usr/bin/perl', os.path.join(scriptsDir, 'gen_wave.pl'), os.path.join(scriptsDir, 'Config.pm'), synthOutDir] + basenames
    p = subprocess.Popen(args, stdout = subprocess.PIPE, stderr = subprocess.STDOUT)
    logOutput = p.communicate()[0]
    if p.returncode != 0:
        sys.stderr.write('WARNING: waveform generation failed (exit code '+str(p.returncode)+')\n')
    if logFile != None:
        with open(logFile, 'w') as f:
            f.write(logOutput)