"""Representation for decision tree questions.

A full question consists of a label valuer together with a question. The label
valuer is a callable that maps a label to a value (e.g. extracts the left-hand
phone from a full-context label). A question is a callable that maps this
value to a boolean representing yes or no.
"""

# Copyright 2011, 2012 Matt Shannon

# This file is part of armspeech.
# See `License` for details of license and warranty.


from __future__ import division

from codedep import codeDeps

@codeDeps()
class IdLabelValuer(object):
    def __init__(self):
        pass
    def __repr__(self):
        return 'IdLabelValuer()'
    def shortRepr(self):
        return 'label'
    def __call__(self, label):
        return label

@codeDeps()
class AttrLabelValuer(object):
    def __init__(self, labelKey):
        self.labelKey = labelKey
    def __repr__(self):
        return 'AttrLabelValuer('+repr(self.labelKey)+')'
    def shortRepr(self):
        return self.labelKey
    def __call__(self, label):
        return getattr(label, self.labelKey)

@codeDeps()
class Question(object):
    pass

@codeDeps(Question)
class SubsetQuestion(Question):
    def __init__(self, subset, name):
        self.subset = subset
        self.name = name
    def __repr__(self):
        return 'SubsetQuestion('+repr(self.subset)+', '+repr(self.name)+')'
    def shortRepr(self):
        return 'is '+self.name
    def __call__(self, value):
        return value in self.subset

@codeDeps(Question)
class EqualityQuestion(Question):
    def __init__(self, value):
        self.value = value
    def __repr__(self):
        return 'EqualityQuestion('+repr(self.value)+')'
    def shortRepr(self):
        return '== '+str(self.value)
    def __call__(self, value):
        return value == self.value

@codeDeps(Question)
class ThreshQuestion(Question):
    def __init__(self, thresh):
        self.thresh = thresh
    def __repr__(self):
        return 'ThreshQuestion('+repr(self.thresh)+')'
    def shortRepr(self):
        return '<= '+str(self.thresh)
    def __call__(self, value):
        return value <= self.thresh

@codeDeps(SubsetQuestion)
def getSubsetQuestions(namedSubsets):
    return [ SubsetQuestion(subset, subsetName) for subsetName, subset in namedSubsets ]
@codeDeps(EqualityQuestion)
def getEqualityQuestions(values):
    return [ EqualityQuestion(value) for value in values ]
@codeDeps(ThreshQuestion)
def getThreshQuestions(threshes):
    return [ ThreshQuestion(thresh) for thresh in threshes ]
