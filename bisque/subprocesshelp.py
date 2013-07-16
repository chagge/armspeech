"""Helper functions for running external processes using subprocess."""

# Copyright 2011, 2012, 2013 Matt Shannon

# This file is part of armspeech.
# See `License` for details of license and warranty.


from __future__ import division

from codedep import codeDeps

import subprocess
from subprocess import PIPE

# emulate python 2.7 function (roughly -- in particular, exception generated is not the same)
@codeDeps()
def check_output(*popenargs, **kwargs):
    p = subprocess.Popen(stdout = PIPE, *popenargs, **kwargs)
    stdout = p.communicate()[0]
    if p.returncode != 0:
        raise subprocess.CalledProcessError('process had non-zero return code', p.returncode)
    return stdout