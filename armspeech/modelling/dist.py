"""Probability distributions and their accumulators."""

# Copyright 2011, 2012 Matt Shannon

# This file is part of armspeech.
# See `License` for details of license and warranty.


from __future__ import division

from armspeech.util.mathhelp import logSum, sigmoid, sampleDiscrete
import nodetree
import semiring
import wnet
from armspeech.util.memoize import memoize
from armspeech.util.mathhelp import assert_allclose
from armspeech.util.util import orderedDictRepr

import logging
import math
import numpy as np
import armspeech.numpy_settings
import numpy.linalg as la
import armspeech.util.mylinalg as mla
from scipy import special
import random
from itertools import izip
from armspeech.util.iterhelp import contextualizeIter
from collections import deque

# (FIXME : add more checks to validate Dists and Accs on creation (including checking for NaNs))

def eval_local(reprString):
    # (FIXME : the contents of test_dist affects what needs to be included here)
    from questions import IdLabelValuer, SubsetQuestion
    from summarizer import VectorSeqSummarizer
    from transform import AddBias, LinearTransform, ShiftOutputTransform, VectorizeTransform, DotProductTransform, PolynomialTransform1D
    from wnet import ConcreteNet
    from armspeech.util.mathhelp import AsArray
    from armspeech.util.util import ConstantFunction

    from numpy import array, eye, float64, Inf, inf

    return eval(reprString)

class SynthMethod(object):
    Meanish = 0
    Sample = 1

class Rat(object):
    Exact = 0
    Approx = 1
    LowerBound = 2
ratToStringDict = {
    Rat.Exact: 'Exact',
    Rat.Approx: 'Approx',
    Rat.LowerBound: 'LowerBound',
}
def ratToString(rat):
    return ratToStringDict[rat]

def sumRats(rats):
    if any([ rat == Rat.Approx for rat in rats ]):
        return Rat.Approx
    elif any([ rat == Rat.LowerBound for rat in rats ]):
        return Rat.LowerBound
    else:
        assert all([ rat == Rat.Exact for rat in rats ])
        return Rat.Exact

def sumValuedRats(valuedRats):
    values, rats = zip(*valuedRats)
    return sum(values), sumRats(rats)

def accNodeList(parentNode):
    return nodetree.nodeList(
        parentNode,
        includeNode = lambda node: isinstance(node, AccCommon)
    )
def distNodeList(parentNode):
    return nodetree.nodeList(
        parentNode,
        includeNode = lambda node: isinstance(node, Dist)
    )

def getEstimateTotAux(estimateAuxPartials, idValue = id):
    estimateAuxPartial = nodetree.chainPartialFns(estimateAuxPartials)
    def estimateTotAux(acc):
        auxValuedRats = dict()
        def estimatePartial(acc, estimateChild):
            ret = estimateAuxPartial(acc, estimateChild)
            if ret is None:
                raise RuntimeError('none of the given partial functions was defined at acc '+repr(acc))
            dist, auxValuedRat = ret
            auxValuedRats[idValue(dist)] = auxValuedRat
            return dist
        dist = nodetree.getDagMap([estimatePartial])(acc)
        totAux, totAuxRat = sumValuedRats([ auxValuedRats[idValue(distNode)] for distNode in distNodeList(dist) ])
        return dist, (totAux, totAuxRat)
    return estimateTotAux

def defaultEstimateAuxPartial(acc, estimateChild):
    return acc.estimateAux(estimateChild)
defaultEstimateTotAux = getEstimateTotAux([defaultEstimateAuxPartial])

def defaultEstimatePartial(acc, estimateChild):
    dist, _ = acc.estimateAux(estimateChild)
    return dist
defaultEstimate = nodetree.getDagMap([defaultEstimatePartial])

def defaultCreateAccPartial(dist, createAccChild):
    return dist.createAcc(createAccChild)
defaultCreateAcc = nodetree.getDagMap([defaultCreateAccPartial])

def getParams(partialMaps):
    return nodetree.getDagMap(
        partialMaps,
        storeValue = lambda params, args: True,
        restoreValue = lambda b, args: []
    )
def getDerivParams(partialMaps):
    return nodetree.getDagMap(
        partialMaps,
        storeValue = lambda derivParams, args: True,
        restoreValue = lambda b, args: []
    )
def getParse(partialMaps):
    return nodetree.getDagMap(
        partialMaps,
        storeValue = lambda (node, paramsLeft), args: node,
        restoreValue = lambda node, args: (node, args[1])
    )
def getCreateAccG(partialMaps):
    return nodetree.getDagMap(partialMaps)

class ParamSpec(object):
    def __init__(self, paramsPartials, derivParamsPartials, parsePartials, createAccGPartials):
        self.params = getParams(paramsPartials)
        self.derivParams = getDerivParams(derivParamsPartials)
        self.parse = getParse(parsePartials)
        self.createAccG = getCreateAccG(createAccGPartials)
    def parseAll(self, dist, params):
        distNew, paramsLeft = self.parse(dist, params)
        if len(paramsLeft) != 0:
            raise RuntimeError('extra parameters left after parsing complete')
        return distNew

def defaultParamsPartial(node, paramsChild):
    return np.concatenate([node.paramsSingle(), node.paramsChildren(paramsChild)])
def defaultDerivParamsPartial(node, derivParamsChild):
    return np.concatenate([node.derivParamsSingle(), node.derivParamsChildren(derivParamsChild)])
def defaultParsePartial(node, params, parseChild):
    newNode, paramsLeft = node.parseSingle(params)
    return newNode.parseChildren(paramsLeft, parseChild)
def defaultCreateAccGPartial(dist, createAccChild):
    return dist.createAccG(createAccChild)
defaultPartial = (
    defaultParamsPartial,
    defaultDerivParamsPartial,
    defaultParsePartial,
    defaultCreateAccGPartial
)
defaultParamSpec = ParamSpec(*zip(*[defaultPartial]))

def nopParamsPartial(node, paramsChild):
    pass
def nopDerivParamsPartial(node, derivParamsChild):
    pass
def nopParsePartial(node, params, parseChild):
    pass
def nopCreateAccGPartial(dist, createAccChild):
    pass

def noLocalParamsPartial(node, paramsChild):
    return node.paramsChildren(paramsChild)
def noLocalDerivParamsPartial(node, derivParamsChild):
    return node.derivParamsChildren(derivParamsChild)
def noLocalParsePartial(node, params, parseChild):
    return node.parseChildren(params, parseChild)
noLocalPartial = (
    noLocalParamsPartial,
    noLocalDerivParamsPartial,
    noLocalParsePartial,
    defaultCreateAccGPartial
)

def isolateDist(dist):
    """Returns an isolated copy of a distribution.

    Creates a new DAG with the same content as the sub-DAG with head dist but
    with fresh objects at each node. Therefore no nodes in the new DAG are
    shared outside the new DAG.
    """
    return nodetree.defaultMap(dist)

def getByTagParamSpec(f):
    def byTagParamsPartial(node, paramsChild):
        if f(node.tag):
            return defaultParamsPartial(node, paramsChild)
    def byTagDerivParamsPartial(node, derivParamsChild):
        if f(node.tag):
            return defaultDerivParamsPartial(node, derivParamsChild)
    def byTagParsePartial(node, params, parseChild):
        if f(node.tag):
            return defaultParsePartial(node, params, parseChild)
    byTagPartial = (
        byTagParamsPartial,
        byTagDerivParamsPartial,
        byTagParsePartial,
        nopCreateAccGPartial
    )
    return ParamSpec(*zip(*[byTagPartial, noLocalPartial]))

def addAcc(accTo, accFrom):
    """Adds accumulator sub-DAG accFrom to accumulator sub-DAG accTo.

    Copes properly with sharing, and raises an exception in the case of invalid
    sharing.
    However assumes accTo is an isolated sub-DAG, i.e. that none of the child
    nodes of accTo are shared with parent nodes outside accTo's sub-DAG (and
    similarly for accFrom), and this method has undefined behaviour if this is
    not true.
    """
    lookup = dict()
    agenda = [(accTo, accFrom)]
    while agenda:
        nodeTo, nodeFrom = agenda.pop()
        identTo = id(nodeTo)
        identFrom = id(nodeFrom)
        if identFrom in lookup:
            assert lookup[identFrom] == identTo
        else:
            lookup[identFrom] = identTo
            nodeTo.addAccSingle(nodeFrom)
            agenda.extend(reversed(nodeTo.addAccChildPairs(nodeFrom)))

def parseConcat(dists, params, parseChild):
    distNews = []
    paramsLeft = params
    for dist in dists:
        distNew, paramsLeft = parseChild(dist, paramsLeft)
        distNews.append(distNew)
    return distNews, paramsLeft

class PruneSpec(object):
    pass
class SimplePruneSpec(PruneSpec):
    def __init__(self, betaThresh, logOccThresh):
        self.betaThresh = betaThresh
        self.logOccThresh = logOccThresh
    def __repr__(self):
        return 'SimplePruneSpec('+repr(self.betaThresh)+', '+repr(self.logOccThresh)+')'

class FillZerosToDepth(object):
    def __init__(self, depth):
        self.depth = depth
        assert self.depth >= 0
    def __repr__(self):
        return 'FillZerosToDepth('+repr(self.depth)+')'
    def __call__(self, acInput):
        acInput = np.asarray(acInput)
        assert len(np.shape(acInput)) == 1
        assert len(acInput) <= self.depth
        if len(acInput) < self.depth:
            acInput = np.concatenate((np.zeros((self.depth - len(acInput),)), acInput))
        return acInput

class Memo(object):
    def __init__(self, maxOcc):
        self.maxOcc = maxOcc

        self.occ = 0.0
        self.fakeOcc = 0.0
        self.inputs = []
        self.outputs = []

    def add(self, input, output, occ = 1.0):
        if occ != 1.0:
            raise RuntimeError('Memo occupancies must be 1.0')
        self.occ += occ
        if self.maxOcc is None or len(self.inputs) < self.maxOcc:
            self.fakeOcc += occ
            self.inputs.append(input)
            self.outputs.append(output)
        elif random.random() * self.occ < self.fakeOcc:
            # (FIXME : behind the scenes, only do subset selection every certain number of inputs (for efficiency)?)
            assert len(self.inputs) == self.maxOcc
            delIndex = random.randrange(self.maxOcc)
            self.inputs[delIndex] = input
            self.outputs[delIndex] = output
        assert_allclose(self.fakeOcc, len(self.inputs))

    # FIXME : do random subset selection for addAcc too
    def addAccSingle(self, acc):
        self.occ += acc.occ
        self.fakeOcc += acc.fakeOcc
        self.inputs += acc.inputs
        self.outputs += acc.outputs
        if self.maxOcc is not None and self.fakeOcc > self.maxOcc:
            self.fakeOcc = self.maxOcc
            self.inputs = self.inputs[:self.maxOcc]
            self.outputs = self.outputs[:self.maxOcc]

class EstimationError(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return str(self.msg)

class InvalidParamsError(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return str(self.msg)

class SynthSeqTooLongError(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return str(self.msg)

class AccCommon(object):
    def children(self):
        abstract
    def mapChildren(self, mapChild):
        raise RuntimeError('mapChildren not defined for accumulator nodes')
    #@property
    #def occ(self):
    #    abstract
    def add(self, input, output, occ = 1.0):
        abstract
    # (FIXME : for all of the Accs defined below, add more checks that acc is of the right type during addAccSingle?)
    def addAccSingle(self, acc):
        abstract
    def addAccChildPairs(self, acc):
        selfChildren = self.children()
        accChildren = acc.children()
        assert len(selfChildren) == len(accChildren)
        return zip(selfChildren, accChildren)
    def count(self):
        return self.occ
    def logLikeSingle(self):
        abstract
    def logLike(self):
        return sum([ accNode.logLikeSingle() for accNode in accNodeList(self) ])
    def withTag(self, tag):
        """Set tag and return self.

        This is intended to be used immediately after object creation, such as:

            acc = SomeAcc([2.0, 3.0, 4.0]).withTag('hi')
        """
        self.tag = tag
        return self

class AccEM(AccCommon):
    def estimateAux(self, estimateChild):
        abstract

class AccG(AccCommon):
    def derivParamsSingle(self):
        abstract
    def derivParamsChildren(self, derivParamsChild):
        children = self.children()
        return [] if not children else np.concatenate([ derivParamsChild(child) for child in children ])

class Acc(AccEM, AccG):
    pass

class TermAcc(Acc):
    """Acc with no children."""
    def children(self):
        return []
    def estimateSingleAux(self):
        abstract
    def estimateAux(self, estimateChild):
        return self.estimateSingleAux()

class DerivTermAccG(AccG):
    def __init__(self, distPrev, tag = None):
        assert len(distPrev.children()) == 0
        self.distPrev = distPrev
        self.tag = tag

        self.occ = 0.0
        self.logLikePrev = 0.0
        self.derivParams = np.zeros([len(distPrev.paramsSingle())])

    def children(self):
        return []

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.logLikePrev += self.distPrev.logProb(input, output) * occ
        self.derivParams += self.distPrev.logProbDerivParams(input, output) * occ

    # N.B. assumes distPrev is the same for self and acc (not checked).
    def addAccSingle(self, acc):
        self.occ += acc.occ
        self.logLikePrev += acc.logLikePrev
        self.derivParams += acc.derivParams

    def logLikeSingle(self):
        return self.logLikePrev

    def derivParamsSingle(self):
        return self.derivParams

class FixedValueAcc(TermAcc):
    def __init__(self, value, tag = None):
        self.value = value
        self.tag = tag

        self.occ = 0.0

    def add(self, input, output, occ = 1.0):
        if output != self.value:
            raise RuntimeError('output '+repr(output)+' != fixed value '+repr(self.value)+' for FixedValueAcc')
        self.occ += occ

    # N.B. assumes self and acc have same fixed value (not checked)
    def addAccSingle(self, acc):
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateSingleAux(self):
        return FixedValueDist(self.value, tag = self.tag), (0.0, Rat.Exact)

class OracleAcc(TermAcc):
    def __init__(self, tag = None):
        self.tag = tag

        self.occ = 0.0

    def add(self, input, output, occ = 1.0):
        self.occ += occ

    def addAccSingle(self, acc):
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateSingleAux(self):
        return OracleDist(tag = self.tag), (0.0, Rat.Exact)

class LinearGaussianAcc(TermAcc):
    def __init__(self, distPrev = None, inputLength = None, varianceFloor = None, tag = None):
        self.distPrev = distPrev
        if distPrev is not None:
            inputLength = len(distPrev.coeff)
        assert inputLength is not None and inputLength >= 0
        self.varianceFloor = varianceFloor if varianceFloor is not None else (distPrev.varianceFloor if distPrev is not None else 0.0)
        self.tag = tag

        self.occ = 0.0
        self.sumSqr = 0.0
        self.sumTarget = np.zeros([inputLength])
        self.sumOuter = np.zeros([inputLength, inputLength])

        assert self.varianceFloor is not None
        assert self.varianceFloor >= 0.0

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.sumSqr += (output ** 2) * occ
        self.sumTarget += input * output * occ
        self.sumOuter += np.outer(input, input) * occ

    # N.B. assumes distPrev (if present) is the same for self and acc (not checked).
    def addAccSingle(self, acc):
        self.occ += acc.occ
        self.sumSqr += acc.sumSqr
        self.sumTarget += acc.sumTarget
        self.sumOuter += acc.sumOuter

    def auxFn(self, coeff, variance):
        term = self.sumSqr - 2.0 * np.dot(self.sumTarget, coeff) + np.dot(np.dot(self.sumOuter, coeff), coeff)
        aux = -0.5 * math.log(2.0 * math.pi) * self.occ - 0.5 * math.log(variance) * self.occ - 0.5 * term / variance
        return aux, Rat.Exact

    def logLikeSingle(self):
        return self.auxFn(self.distPrev.coeff, self.distPrev.variance)[0]

    def auxDerivParams(self, coeff, variance):
        term = self.sumSqr - 2.0 * np.dot(self.sumTarget, coeff) + np.dot(np.dot(self.sumOuter, coeff), coeff)
        derivCoeff = (self.sumTarget - np.dot(self.sumOuter, coeff)) / variance
        derivLogPrecision = 0.5 * self.occ - 0.5 * term / variance
        return np.append(derivCoeff, derivLogPrecision), Rat.Exact

    def derivParamsSingle(self):
        return self.auxDerivParams(self.distPrev.coeff, self.distPrev.variance)[0]

    def estimateSingleAux(self):
        try:
            if self.occ == 0.0:
                raise EstimationError('require occ > 0')
            try:
                sumOuterInv = mla.pinv(self.sumOuter)
            except la.LinAlgError, detail:
                raise EstimationError('could not compute pseudo-inverse: '+str(detail))
            coeff = np.dot(sumOuterInv, self.sumTarget)
            variance = (self.sumSqr - np.dot(coeff, self.sumTarget)) / self.occ

            if variance < self.varianceFloor:
                variance = self.varianceFloor

            if variance <= 0.0:
                raise EstimationError('computed variance is zero or negative: '+str(variance))
            elif variance < 1e-10:
                raise EstimationError('computed variance too miniscule (variances this small can lead to substantial loss of precision during accumulation): '+str(variance))
            return LinearGaussian(coeff, variance, self.varianceFloor, tag = self.tag), self.auxFn(coeff, variance)
        except EstimationError, detail:
            if self.distPrev is None:
                raise
            else:
                logging.warning('reverting to previous dist due to error during LinearGaussian estimation: '+str(detail))
                coeff = self.distPrev.coeff
                variance = self.distPrev.variance
                return LinearGaussian(coeff, variance, self.varianceFloor, tag = self.tag), self.auxFn(coeff, variance)

    # N.B. assumes last component of input vector is bias (weakly checked)
    # (N.B. is geometric -- not invariant with respect to scaling of individual summarizers (at least for depth > 0))
    def estimateInitialMixtureOfTwoExperts(self):
        if self.occ == 0.0:
            logging.warning('not mixing up LinearGaussian with occ == 0.0')
            return self.estimateSingleAux()[0]
        sigmoidAbscissaAtOneStdev = 0.5
        occRecompute = self.sumOuter[-1, -1]
        S = self.sumOuter[:-1, :-1] / occRecompute
        mu = self.sumOuter[-1, :-1] / occRecompute
        if abs(occRecompute - self.occ) > 1e-10:
            raise RuntimeError('looks like last component of input vector is not bias ('+str(occRecompute)+' vs '+str(self.occ)+')!')
        # FIXME : completely different behaviour for depth 0 case!
        #   Can we unify (or improve depth > 0 case with something HTK-like)?
        # FIXME : what about len(self.sumOuter) == 0 case?
        # (FIXME : hard-coded flooring of 5.0)
        if len(self.sumOuter) == 1:
            # HTK-style mixture incrementing
            coeff = np.array([0.0])
            coeffFloor = np.array([float('inf')])
            blc = BinaryLogisticClassifier(coeff, coeffFloor)
            dist = self.estimateSingleAux()[0]
            mean, = dist.coeff
            variance = dist.variance
            dist0 = LinearGaussian(np.array([mean - 0.2 * math.sqrt(variance)]), variance, self.varianceFloor)
            dist1 = LinearGaussian(np.array([mean + 0.2 * math.sqrt(variance)]), variance, self.varianceFloor)
            return MixtureDist(blc, [dist0, dist1])
        else:
            l, U = la.eigh(S - np.outer(mu, mu))
            eigVal, (index, eigVec) = max(zip(l, enumerate(np.transpose(U))))
            if eigVal == 0.0:
                logging.warning('not mixing up LinearGaussian since eigenvalue 0.0')
                return self.estimateSingleAux()[0]
            w = eigVec * sigmoidAbscissaAtOneStdev / math.sqrt(eigVal)
            w0 = -np.dot(w, mu)
            coeff = np.append(w, w0)
            coeffFloor = np.append(np.ones((len(w),)) * 5.0, float('inf'))
            coeff = np.minimum(coeff, coeffFloor)
            coeff = np.maximum(coeff, -coeffFloor)
            blc = BinaryLogisticClassifier(coeff, coeffFloor)
            dist0 = self.estimateSingleAux()[0]
            dist1 = self.estimateSingleAux()[0]
            return MixtureDist(blc, [dist0, dist1])

class ConstantClassifierAcc(TermAcc):
    def __init__(self, distPrev = None, numClasses = None, probFloors = None, tag = None):
        self.distPrev = distPrev
        if distPrev is not None:
            numClasses = len(distPrev.probs)
        assert numClasses >= 1
        self.probFloors = probFloors if probFloors is not None else (distPrev.probFloors if distPrev is not None else np.zeros((numClasses,)))
        self.tag = tag

        self.occ = 0.0
        self.occs = np.zeros([numClasses])

        assert self.probFloors is not None
        assert len(self.probFloors) == len(self.occs)
        assert all(self.probFloors >= 0.0)
        assert sum(self.probFloors) <= 1.0

    def add(self, input, classIndex, occ = 1.0):
        self.occ += occ
        self.occs[classIndex] += occ

    # N.B. assumes class 0 in self corresponds to class 0 in acc, etc.
    #   Also assumes distPrev (if present) is the same for self and acc (not checked).
    def addAccSingle(self, acc):
        self.occ += acc.occ
        self.occs += acc.occs

    def auxFn(self, probs):
        return sum([ occ * logProb for occ, logProb in zip(self.occs, np.log(probs)) if occ > 0.0 ]), Rat.Exact

    def logLikeSingle(self):
        return self.auxFn(self.distPrev.probs)[0]

    def auxDerivParams(self, probs):
        return self.occs[:-1] - self.occs[-1] - (probs[:-1] - probs[-1]) * self.occ, Rat.Exact

    def derivParamsSingle(self):
        return self.auxDerivParams(self.distPrev.probs)[0]

    def estimateSingleAux(self):
        try:
            if self.occ == 0.0:
                raise EstimationError('require occ > 0')
            probs = self.occs / self.occ

            # find the probs which maximize the auxiliary function, subject to
            #   the given flooring constraints
            # FIXME : think more about maths of flooring procedure below. It
            #   is guaranteed to terminate, but think there are cases (for more
            #   than 2 classes) where it doesn't find the constrained ML
            #   optimum.
            floored = (probs < self.probFloors)
            done = False
            while not done:
                probsBelow = self.probFloors * floored
                probsAbove = probs * (-floored)
                probsAbove = probsAbove / sum(probsAbove) * (1.0 - sum(probsBelow))
                flooredOld = floored
                floored = floored + (probsAbove < self.probFloors)
                done = all(flooredOld == floored)
            probs = probsBelow + probsAbove
            assert_allclose(sum(probs), 1.0)
            assert all(probs >= self.probFloors)

            return ConstantClassifier(probs, self.probFloors, tag = self.tag), self.auxFn(probs)
        except EstimationError, detail:
            if self.distPrev is None:
                raise
            else:
                logging.warning('reverting to previous dist due to error during ConstantClassifier estimation: '+str(detail))
                probs = self.distPrev.probs
                return ConstantClassifier(probs, self.probFloors, tag = self.tag), self.auxFn(probs)

class BinaryLogisticClassifierAcc(TermAcc):
    def __init__(self, distPrev, tag = None):
        self.distPrev = distPrev
        self.tag = tag

        dim = len(self.distPrev.coeff)
        self.occ = 0.0
        self.sumTarget = np.zeros([dim])
        self.sumOuter = np.zeros([dim, dim])
        self.logLikePrev = 0.0

    def add(self, input, classIndex, occ = 1.0):
        if occ > 0.0:
            probPrev1 = self.distPrev.prob(input, 1)
            self.occ += occ
            self.sumTarget += input * (probPrev1 - classIndex) * occ
            self.sumOuter += np.outer(input, input) * probPrev1 * (1.0 - probPrev1) * occ
            self.logLikePrev += self.distPrev.logProb(input, classIndex) * occ

    # N.B. assumes class 0 in self corresponds to class 0 in acc, etc.
    # (FIXME : accumulated values encode a local quadratic approx of likelihood
    #   function at current params. However should the origin in parameter space
    #   be treated as absolute zero rather than the current params?
    #   Would allow decision tree clustering with BinaryLogisticClassifier (although
    #   quadratic approx may not be very good in this situation).)
    def addAccSingle(self, acc):
        # (FIXME : below assert is too strict, but better than nothing)
        assert self.distPrev == acc.distPrev
        self.occ += acc.occ
        self.sumTarget += acc.sumTarget
        self.sumOuter += acc.sumOuter
        self.logLikePrev += acc.logLikePrev

    def auxFn(self, coeff):
        coeffDelta = coeff - self.distPrev.coeff
        if np.all(coeffDelta == 0.0):
            return self.logLikePrev, Rat.Exact
        else:
            return self.logLikePrev - np.dot(self.sumTarget, coeffDelta) - 0.5 * np.dot(np.dot(self.sumOuter, coeffDelta), coeffDelta), Rat.Approx

    def logLikeSingle(self):
        return self.logLikePrev

    def derivParamsSingle(self):
        return -self.sumTarget

    # (FIXME : estimation doesn't always converge, even in the case where
    #   classes are not linearly separable and we have a clearly defined
    #   maximum. Come up with a better procedure? For example, could
    #   say that if current update decreases log likelihood, then take a
    #   half-step and try again (tho N.B. requires tracking previous log like
    #   somehow). Does this always converge? Could also try Firth adjustment,
    #   or other forms of regularization (though conceptually this is solving
    #   a different problem -- shouldn't have to use any regularization to get
    #   the nice non-linearly-separable case to work!).)
    def estimateSingleAux(self):
        try:
            if self.occ == 0.0:
                raise EstimationError('require occ > 0')
            try:
                sumOuterInv = mla.pinv(self.sumOuter)
            except la.LinAlgError, detail:
                raise EstimationError('could not compute pseudo-inverse: '+str(detail))
            coeffDelta = -np.dot(sumOuterInv, self.sumTarget)

            # approximate constrained maximum likelihood
            step = 0.7
            while any(np.abs(self.distPrev.coeff + coeffDelta * step) > self.distPrev.coeffFloor):
                step *= 0.5
            coeff = self.distPrev.coeff + coeffDelta * step
            assert all(np.abs(coeff) <= self.distPrev.coeffFloor)

            return BinaryLogisticClassifier(coeff, self.distPrev.coeffFloor, tag = self.tag), self.auxFn(coeff)
        except EstimationError, detail:
            logging.warning('reverting to previous dist due to error during BinaryLogisticClassifier estimation: '+str(detail))
            coeff = self.distPrev.coeff
            return BinaryLogisticClassifier(coeff, self.distPrev.coeffFloor, tag = self.tag), self.auxFn(coeff)

class MixtureAcc(Acc):
    def __init__(self, distPrev, classAcc, regAccs, tag = None):
        self.numComps = distPrev.numComps
        self.distPrev = distPrev
        self.classAcc = classAcc
        self.regAccs = regAccs
        self.tag = tag

        self.occ = 0.0
        self.entropy = 0.0

    def children(self):
        return [self.classAcc] + self.regAccs

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        logProbs = [ self.distPrev.logProbComp(input, comp, output) for comp in range(self.numComps) ]
        logTot = logSum(logProbs)
        relOccs = np.exp(logProbs - logTot)
        assert_allclose(sum(relOccs), 1.0)
        for comp in range(self.numComps):
            relOcc = relOccs[comp]
            if relOcc > 0.0:
                self.classAcc.add(input, comp, occ * relOcc)
                self.regAccs[comp].add(input, output, occ * relOcc)
                self.entropy -= occ * relOcc * math.log(relOcc)

    # N.B. assumes component 0 in self corresponds to component 0 in acc, etc.
    #   Also assumes distPrev is the same for self and acc (not checked).
    def addAccSingle(self, acc):
        assert self.numComps == acc.numComps
        self.occ += acc.occ
        self.entropy += acc.entropy

    def logLikeSingle(self):
        return self.entropy

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        classDist = estimateChild(self.classAcc)
        regDists = [ estimateChild(regAcc) for regAcc in self.regAccs ]
        return MixtureDist(classDist, regDists, tag = self.tag), (self.entropy, Rat.LowerBound)

class IdentifiableMixtureAcc(Acc):
    def __init__(self, classAcc, regAccs, tag = None):
        self.classAcc = classAcc
        self.regAccs = regAccs
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [self.classAcc] + self.regAccs

    def add(self, input, output, occ = 1.0):
        comp, acOutput = output
        self.occ += occ
        self.classAcc.add(input, comp, occ)
        self.regAccs[comp].add(input, acOutput, occ)

    def addAccSingle(self, acc):
        assert len(self.regAccs) == len(acc.regAccs)
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        classDist = estimateChild(self.classAcc)
        regDists = [ estimateChild(regAcc) for regAcc in self.regAccs ]
        return IdentifiableMixtureDist(classDist, regDists, tag = self.tag), (0.0, Rat.Exact)

def createVectorAcc(order, outIndices, vectorSummarizer, createAccForIndex):
    accComps = dict()
    for outIndex in outIndices:
        accComps[outIndex] = createAccForIndex(outIndex)
    return VectorAcc(order, vectorSummarizer, outIndices, accComps)

class VectorAcc(Acc):
    def __init__(self, order, vectorSummarizer, keys, accComps, tag = None):
        assert len(keys) == len(accComps)
        for key in keys:
            assert key in accComps
        self.order = order
        self.vectorSummarizer = vectorSummarizer
        self.keys = keys
        self.accComps = accComps
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [ self.accComps[key] for key in self.keys ]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        for outIndex in self.accComps:
            summary = self.vectorSummarizer(input, output[:outIndex], outIndex)
            self.accComps[outIndex].add(summary, output[outIndex], occ)

    def addAccSingle(self, acc):
        assert self.order == acc.order
        assert self.keys == acc.keys
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        distComps = dict()
        for outIndex in self.accComps:
            distComps[outIndex] = estimateChild(self.accComps[outIndex])
        return VectorDist(self.order, self.vectorSummarizer, self.keys, distComps, tag = self.tag), (0.0, Rat.Exact)

def createDiscreteAcc(keys, createAccFor):
    accDict = dict()
    for key in keys:
        accDict[key] = createAccFor(key)
    return DiscreteAcc(keys, accDict)

class DiscreteAcc(Acc):
    def __init__(self, keys, accDict, tag = None):
        assert len(keys) == len(accDict)
        for key in keys:
            assert key in accDict
        self.keys = keys
        self.accDict = accDict
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [ self.accDict[key] for key in self.keys ]

    def add(self, input, output, occ = 1.0):
        label, acInput = input
        self.occ += occ
        self.accDict[label].add(acInput, output, occ)

    def addAccSingle(self, acc):
        assert self.keys == acc.keys
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        distDict = dict()
        for label in self.accDict:
            distDict[label] = estimateChild(self.accDict[label])
        return DiscreteDist(self.keys, distDict, tag = self.tag), (0.0, Rat.Exact)

class AutoGrowingDiscreteAcc(Acc):
    """Discrete acc that creates sub-accs as necessary when a new phonetic context is seen.

    (N.B. the accumulator sub-DAGs created by createAcc should probably not have
    any nodes which are shared outside that sub-DAG. (Could think about more
    carefully if we ever have a use case).)
    """
    def __init__(self, createAcc, tag = None):
        self.accDict = dict()
        self.createAcc = createAcc
        self.tag = tag

        self.occ = 0.0

    def children(self):
        # (FIXME : the order of the result here depends on hash map details, so
        #   could get different secHashes for resulting pickled files. Probably
        #   not an issue, but if it was, could solve by sorting based on key.)
        return self.accDict.values()

    def add(self, input, output, occ = 1.0):
        label, acInput = input
        self.occ += occ
        if not label in self.accDict:
            self.accDict[label] = self.createAcc()
        self.accDict[label].add(acInput, output, occ)

    def addAccSingle(self, acc):
        self.occ += acc.occ

    def addAccChildPairs(self, acc):
        ret = []
        for label in acc.accDict:
            if not label in self.accDict:
                self.accDict[label] = self.createAcc()
            ret.append((self.accDict[label], acc.accDict[label]))
        return ret

class DecisionTreeAcc(Acc):
    pass

class DecisionTreeAccNode(DecisionTreeAcc):
    def __init__(self, fullQuestion, accYes, accNo, tag = None):
        self.fullQuestion = fullQuestion
        self.accYes = accYes
        self.accNo = accNo
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [self.accYes, self.accNo]

    def add(self, input, output, occ = 1.0):
        label, acInput = input
        self.occ += occ
        labelValuer, question = self.fullQuestion
        if question(labelValuer(label)):
            self.accYes.add(input, output, occ)
        else:
            self.accNo.add(input, output, occ)

    def addAccSingle(self, acc):
        assert self.fullQuestion == acc.fullQuestion
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        distYes = estimateChild(self.accYes)
        distNo = estimateChild(self.accNo)
        return DecisionTreeNode(self.fullQuestion, distYes, distNo, tag = self.tag), (0.0, Rat.Exact)

class DecisionTreeAccLeaf(DecisionTreeAcc):
    def __init__(self, acc, tag = None):
        self.acc = acc
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [self.acc]

    def add(self, input, output, occ = 1.0):
        label, acInput = input
        self.occ += occ
        self.acc.add(acInput, output, occ)

    def addAccSingle(self, acc):
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        dist = estimateChild(self.acc)
        return DecisionTreeLeaf(dist, tag = self.tag), (0.0, Rat.Exact)

class MappedInputAcc(Acc):
    """Acc where input is mapped using a fixed transform."""
    def __init__(self, inputTransform, acc, tag = None):
        self.inputTransform = inputTransform
        self.acc = acc
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [self.acc]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.acc.add(self.inputTransform(input), output, occ)

    def addAccSingle(self, acc):
        self.occ += acc.occ

    # FIXME : remove below once seqForInput added to AutoregressiveSequenceDist
    def count(self):
        return self.acc.count()

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        dist = estimateChild(self.acc)
        return MappedInputDist(self.inputTransform, dist, tag = self.tag), (0.0, Rat.Exact)

class MappedOutputAcc(Acc):
    """Acc where output is mapped using a fixed transform."""
    def __init__(self, outputTransform, acc, tag = None):
        self.outputTransform = outputTransform
        self.acc = acc
        self.tag = tag

        self.occ = 0.0
        self.logJac = 0.0

    def children(self):
        return [self.acc]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.acc.add(input, self.outputTransform(input, output), occ)
        self.logJac += self.outputTransform.logJac(input, output) * occ

    def addAccSingle(self, acc):
        self.occ += acc.occ
        self.logJac += acc.logJac

    def logLikeSingle(self):
        return self.logJac

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        dist = estimateChild(self.acc)
        return MappedOutputDist(self.outputTransform, dist, tag = self.tag), (self.logJac, Rat.Exact)

class TransformedInputLearnDistAccEM(AccEM):
    """Acc for transformed input, where we learn the sub-dist parameters using EM."""
    def __init__(self, inputTransform, acc, tag = None):
        self.inputTransform = inputTransform
        self.acc = acc
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [self.acc]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.acc.add(self.inputTransform(input), output, occ)

    def addAccSingle(self, acc):
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def estimateAux(self, estimateChild):
        dist = estimateChild(self.acc)
        return TransformedInputDist(self.inputTransform, dist, tag = self.tag), (0.0, Rat.Exact)

class TransformedInputLearnTransformAccEM(AccEM):
    """Acc for transformed input, where we learn the transform parameters using EM."""
    def __init__(self, inputTransformAcc, dist, tag = None):
        self.inputTransformAcc = inputTransformAcc
        self.dist = dist
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [self.inputTransformAcc]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.inputTransformAcc.add((self.dist, input), output, occ)

    def addAccSingle(self, acc):
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def estimateAux(self, estimateChild):
        inputTransform = estimateChild(self.inputTransformAcc)
        return TransformedInputDist(inputTransform, self.dist, tag = self.tag), (0.0, Rat.Exact)

class TransformedInputAccG(AccG):
    """Acc for transformed input, where we compute the gradient with respect to both the transform and sub-dist parameters."""
    def __init__(self, (inputTransformAcc, inputTransform), (acc, dist), tag = None):
        self.inputTransformAcc = inputTransformAcc
        self.inputTransform = inputTransform
        self.acc = acc
        self.dist = dist
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [self.inputTransformAcc, self.acc]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.inputTransformAcc.add((self.dist, input), output, occ)
        self.acc.add(self.inputTransform(input), output, occ)

    def addAccSingle(self, acc):
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

class TransformedOutputLearnDistAccEM(AccEM):
    """Acc for transformed output, where we learn the sub-dist parameters using EM."""
    def __init__(self, outputTransform, acc, tag = None):
        self.outputTransform = outputTransform
        self.acc = acc
        self.tag = tag

        self.occ = 0.0
        self.logJac = 0.0

    def children(self):
        return [self.acc]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.acc.add(input, self.outputTransform(input, output), occ)
        self.logJac += self.outputTransform.logJac(input, output) * occ

    def addAccSingle(self, acc):
        self.occ += acc.occ
        self.logJac += acc.logJac

    def logLikeSingle(self):
        return self.logJac

    def estimateAux(self, estimateChild):
        dist = estimateChild(self.acc)
        return TransformedOutputDist(self.outputTransform, dist, tag = self.tag), (self.logJac, Rat.Exact)

class TransformedOutputLearnTransformAccEM(AccEM):
    """Acc for transformed output, where we learn the transform parameters using EM."""
    def __init__(self, inputTransformAcc, dist, tag = None):
        self.outputTransformAcc = outputTransformAcc
        self.dist = dist
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [self.outputTransformAcc]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.outputTransformAcc.add((self.dist, input), output, occ)

    def addAccSingle(self, acc):
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def estimateAux(self, estimateChild):
        outputTransform = estimateChild(self.outputTransformAcc)
        return TransformedOutputDist(outputTransform, self.dist, tag = self.tag), (0.0, Rat.Exact)

class TransformedOutputAccG(AccG):
    """Acc for transformed output, where we compute the gradient with respect to both the transform and sub-dist parameters."""
    def __init__(self, (outputTransformAcc, outputTransform), (acc, dist), tag = None):
        self.outputTransformAcc = outputTransformAcc
        self.outputTransform = outputTransform
        self.acc = acc
        self.dist = dist
        self.tag = tag

        self.occ = 0.0
        # (FIXME : should logJac tracking go into the outputTransformAcc instead?)
        self.logJac = 0.0

    def children(self):
        return [self.outputTransformAcc, self.acc]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.outputTransformAcc.add((self.dist, input), output, occ)
        self.acc.add(input, self.outputTransform(input, output), occ)
        self.logJac += self.outputTransform.logJac(input, output) * occ

    def addAccSingle(self, acc):
        self.occ += acc.occ
        self.logJac += acc.logJac

    def logLikeSingle(self):
        return self.logJac

    def derivParamsSingle(self):
        return []

class PassThruAcc(Acc):
    def __init__(self, acc, tag = None):
        self.acc = acc
        self.tag = tag

        self.occ = 0.0

    def children(self):
        return [self.acc]

    def add(self, input, output, occ = 1.0):
        self.occ += occ
        self.acc.add(input, output, occ)

    def addAccSingle(self, acc):
        self.occ += acc.occ

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        dist = estimateChild(self.acc)
        return PassThruDist(dist, tag = self.tag), (0.0, Rat.Exact)

class DebugAcc(Acc):
    def __init__(self, maxOcc, acc, tag = None):
        self.acc = acc
        self.tag = tag

        self.memo = Memo(maxOcc = maxOcc)

    def children(self):
        return [self.acc]

    @property
    def occ(self):
        return self.memo.occ

    def add(self, input, output, occ = 1.0):
        self.memo.add(input, output, occ)
        self.acc.add(input, output, occ)

    def addAccSingle(self, acc):
        self.memo.addAccSingle(acc.memo)

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        dist = estimateChild(self.acc)
        return DebugDist(self.memo.maxOcc, dist, tag = self.tag), (0.0, Rat.Exact)

class AutoregressiveSequenceAcc(Acc):
    def __init__(self, depth, acc, tag = None):
        self.depth = depth
        self.acc = acc
        self.tag = tag

        self.occ = 0.0
        self.frames = 0.0

    def children(self):
        return [self.acc]

    def add(self, inSeq, outSeq, occ = 1.0):
        assert len(inSeq) == len(outSeq)
        self.occ += occ
        for inFrame, (outContext, outFrame) in izip(inSeq, contextualizeIter(self.depth, outSeq)):
            if len(outContext) == self.depth:
                self.frames += occ
                self.acc.add((inFrame, outContext), outFrame, occ)

    def addAccSingle(self, acc):
        self.occ += acc.occ
        self.frames += acc.frames

    def count(self):
        return self.frames

    def logLikeSingle(self):
        return 0.0

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        dist = estimateChild(self.acc)
        return AutoregressiveSequenceDist(self.depth, dist, tag = self.tag), (0.0, Rat.Exact)

class AutoregressiveNetAcc(Acc):
    def __init__(self, distPrev, durAcc, acAcc, verbosity, tag = None):
        self.distPrev = distPrev
        self.durAcc = durAcc
        self.acAcc = acAcc
        self.verbosity = verbosity
        self.tag = tag

        self.occ = 0.0
        self.frames = 0.0
        self.entropy = 0.0

    def children(self):
        return [self.durAcc, self.acAcc]

    def add(self, input, outSeq, occ = 1.0):
        if self.verbosity >= 2:
            print 'fb: seq (%s frames)' % len(outSeq)
        self.occ += occ
        self.frames += len(outSeq) * occ
        timedNet, labelToWeight = self.distPrev.getTimedNet(input, outSeq)
        totalLogProb, edgeGen = wnet.forwardBackwardAlt(timedNet, labelToWeight = labelToWeight, divisionRing = self.distPrev.ring, getAgenda = self.distPrev.getAgenda)
        entropy = totalLogProb * occ
        accedEdges = 0
        for (label, labelStartTime, labelEndTime), logOcc in edgeGen:
            if label is not None and (self.distPrev.pruneSpec is None or self.distPrev.pruneSpec.logOccThresh is None or logOcc > -self.distPrev.pruneSpec.logOccThresh):
                labelOcc = math.exp(logOcc) * occ
                entropy -= labelToWeight((label, labelStartTime, labelEndTime)) * labelOcc

                acInput = outSeq[max(labelStartTime - self.distPrev.depth, 0):labelStartTime]
                if not label[0]:
                    _, phInput, phOutput = label
                    self.durAcc.add((phInput, acInput), phOutput, labelOcc)
                else:
                    _, phInput = label
                    assert labelEndTime == labelStartTime + 1
                    acOutput = outSeq[labelStartTime]
                    self.acAcc.add((phInput, acInput), acOutput, labelOcc)
                accedEdges += 1
        self.entropy += entropy

        if self.verbosity >= 2:
            print 'fb:    log like = %s (net path entropy = %s)' % (
                (0.0, 0.0) if len(outSeq) == 0 else (totalLogProb / len(outSeq), entropy / len(outSeq))
            )
        if self.verbosity >= 3:
            print 'fb:    (accumulated over %s edges)' % accedEdges
        if self.verbosity >= 2:
            print 'fb:'

    # N.B. assumes distPrev is the same for self and acc (not checked).
    def addAccSingle(self, acc):
        self.occ += acc.occ
        self.frames += acc.frames
        self.entropy += acc.entropy

    def count(self):
        return self.frames

    def logLikeSingle(self):
        return self.entropy

    def derivParamsSingle(self):
        return []

    def estimateAux(self, estimateChild):
        durDist = estimateChild(self.durAcc)
        acDist = estimateChild(self.acAcc)
        if self.verbosity >= 1:
            print 'fb:    overall net path entropy = %s (%s frames)' % (
                (0.0, 0) if self.frames == 0 else (self.entropy / self.frames, self.frames)
            )
        return AutoregressiveNetDist(self.distPrev.depth, self.distPrev.netFor, durDist, acDist, self.distPrev.pruneSpec, tag = self.tag), (self.entropy, Rat.LowerBound)


class Dist(object):
    """Conditional probability distribution."""
    def children(self):
        abstract
    def mapChildren(self, mapChild):
        abstract
    def logProb(self, input, output):
        abstract
    def logProbDerivInput(self, input, output):
        abstract
    def logProbDerivOutput(self, input, output):
        abstract
    def createAcc(self, createAccChild):
        abstract
    def createAccG(self, createAccChild):
        return self.createAcc(createAccChild)
    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        abstract
    def paramsSingle(self):
        abstract
    def paramsChildren(self, paramsChild):
        children = self.children()
        return [] if not children else np.concatenate([ paramsChild(child) for child in children ])
    def parseSingle(self, params):
        abstract
    def parseChildren(self, params, parseChild):
        abstract
    def flooredSingle(self):
        return 0, 0
    def withTag(self, tag):
        """Set tag and return self.

        This is intended to be used immediately after object creation, such as:

            dist = SomeDist([2.0, 3.0, 4.0]).withTag('hi')

        This is particularly important here since a design goal is that Dists
        are immutable.
        """
        self.tag = tag
        return self

class TermDist(Dist):
    """Dist with no children."""
    def children(self):
        return []
    def createAccSingle(self):
        abstract
    def createAcc(self, createAccChild):
        return self.createAccSingle()
    def parseChildren(self, params, parseChild):
        return self, params

class FixedValueDist(TermDist):
    def __init__(self, value, tag = None):
        self.value = value
        self.tag = tag

    def __repr__(self):
        return 'FixedValueDist('+repr(self.value)+', tag = '+repr(self.tag)+')'

    def mapChildren(self, mapChild):
        return FixedValueDist(self.value, tag = self.tag)

    def logProb(self, input, output):
        if output == self.value:
            return 0.0
        else:
            return float('-inf')

    def logProbDerivInput(self, input, output):
        return np.zeros(np.shape(input))

    def createAccSingle(self):
        return FixedValueAcc(self.value, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        return self.value

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return FixedValueDist(self.value, tag = self.tag), params

class OracleDist(TermDist):
    def __init__(self, tag = None):
        self.tag = tag

    def __repr__(self):
        return 'OracleDist(tag = '+repr(self.tag)+')'

    def mapChildren(self, mapChild):
        return OracleDist(tag = self.tag)

    def logProb(self, input, output):
        return 0.0

    def createAccSingle(self):
        return OracleAcc(tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        return actualOutput

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return OracleDist(tag = self.tag), params

class LinearGaussian(TermDist):
    def __init__(self, coeff, variance, varianceFloor, tag = None):
        self.coeff = coeff
        self.variance = variance
        self.varianceFloor = varianceFloor
        self.tag = tag
        self.gConst = -0.5 * math.log(2.0 * math.pi)

        assert self.varianceFloor is not None
        assert self.varianceFloor >= 0.0
        assert self.variance >= self.varianceFloor
        assert self.variance > 0.0
        if self.variance < 1e-10:
            raise RuntimeError('LinearGaussian variance too miniscule (variances this small can lead to substantial loss of precision during accumulation): '+str(self.variance))

    def __repr__(self):
        return 'LinearGaussian('+repr(self.coeff)+', '+repr(self.variance)+', '+repr(self.varianceFloor)+', tag = '+repr(self.tag)+')'

    def mapChildren(self, mapChild):
        return LinearGaussian(self.coeff, self.variance, self.varianceFloor, tag = self.tag)

    def logProb(self, input, output):
        mean = np.dot(self.coeff, input)
        return self.gConst - 0.5 * math.log(self.variance) - 0.5 * (output - mean) ** 2 / self.variance

    def logProbDerivInput(self, input, output):
        mean = np.dot(self.coeff, input)
        return self.coeff * (output - mean) / self.variance

    def logProbDerivOutput(self, input, output):
        mean = np.dot(self.coeff, input)
        return -(output - mean) / self.variance

    def residual(self, input, output):
        mean = np.dot(self.coeff, input)
        return (output - mean) / math.sqrt(self.variance)

    def createAccSingle(self):
        return LinearGaussianAcc(distPrev = self, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        mean = np.dot(self.coeff, input)
        if method == SynthMethod.Meanish:
            return mean
        elif method == SynthMethod.Sample:
            return random.gauss(mean, math.sqrt(self.variance))
        else:
            raise RuntimeError('unknown SynthMethod '+repr(method))

    def paramsSingle(self):
        return np.append(self.coeff, -math.log(self.variance))

    def parseSingle(self, params):
        n = len(self.coeff)
        coeff = params[:n]
        variance = math.exp(-params[n])
        if variance < self.varianceFloor:
            raise InvalidParamsError('variance = %s < varianceFloor = %s during LinearGaussian parsing' % (variance, self.varianceFloor))
        return LinearGaussian(coeff, variance, self.varianceFloor, tag = self.tag), params[n + 1:]

    def flooredSingle(self):
        return (1, 1) if np.allclose(self.variance, self.varianceFloor) else (0, 1)

class StudentDist(TermDist):
    def __init__(self, df, precision, tag = None):
        if df <= 0.0:
            raise ValueError('df = '+str(df)+' but should be > 0.0')
        if precision <= 0.0:
            raise ValueError('precision = '+str(precision)+' but should be > 0.0')
        self.df = df
        self.precision = precision
        self.tag = tag

        self.gConst = special.gammaln(0.5) - special.betaln(0.5, 0.5 * self.df) + 0.5 * math.log(self.precision) - 0.5 * math.log(self.df) - 0.5 * math.log(math.pi)

    def __repr__(self):
        return 'StudentDist('+repr(self.df)+', '+repr(self.precision)+', tag = '+repr(self.tag)+')'

    def mapChildren(self, mapChild):
        return StudentDist(self.df, self.precision, tag = self.tag)

    def logProb(self, input, output):
        assert np.shape(output) == ()
        a = output * output * self.precision / self.df
        return self.gConst - 0.5 * (self.df + 1.0) * math.log(1.0 + a)

    def logProbDerivInput(self, input, output):
        assert np.shape(output) == ()
        return np.zeros(np.shape(input))

    def logProbDerivOutput(self, input, output):
        assert np.shape(output) == ()
        a = output * output * self.precision / self.df
        return -(self.df + 1.0) * output * self.precision / self.df / (1.0 + a)

    def logProbDerivParams(self, input, output):
        a = output * output * self.precision / self.df
        K = self.df - (1.0 + self.df) / (1.0 + a)
        return np.array([
            0.5 * K + 0.5 * self.df * (special.psi(0.5 * (self.df + 1.0)) - special.psi(0.5 * self.df) - math.log(1.0 + a)),
            -0.5 * K
        ])

    def createAcc(self, createAccChild):
        raise RuntimeError('cannot estimate Student distribution using EM')

    def createAccG(self, createAccChild):
        return DerivTermAccG(distPrev = self, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        if method == SynthMethod.Meanish:
            return 0.0
        elif method == SynthMethod.Sample:
            while True:
                out = np.random.standard_t(self.df) / math.sqrt(self.precision)
                if math.isinf(out):
                    print 'NOTE: redoing sample from t-dist since it was', out
                else:
                    return out
        else:
            raise RuntimeError('unknown SynthMethod '+repr(method))

    def paramsSingle(self):
        return np.array([math.log(self.df), math.log(self.precision)])

    def parseSingle(self, params):
        df = math.exp(params[0])
        precision = math.exp(params[1])
        return StudentDist(df, precision, tag = self.tag), params[2:]

class ConstantClassifier(TermDist):
    def __init__(self, probs, probFloors, tag = None):
        self.probs = probs
        self.probFloors = probFloors
        self.tag = tag

        assert len(self.probs) >= 1
        assert_allclose(sum(self.probs), 1.0)
        assert self.probFloors is not None
        assert len(self.probFloors) == len(self.probs)
        assert all(self.probFloors >= 0.0)
        assert sum(self.probFloors) <= 1.0
        assert all(self.probs >= self.probFloors)

    def __repr__(self):
        return 'ConstantClassifier('+repr(self.probs)+', '+repr(self.probFloors)+', tag = '+repr(self.tag)+')'

    def mapChildren(self, mapChild):
        return ConstantClassifier(self.probs, self.probFloors, tag = self.tag)

    def logProb(self, input, classIndex):
        prob = self.probs[classIndex]
        return math.log(prob) if prob != 0.0 else float('-inf')

    def logProbDerivInput(self, input, classIndex):
        return np.zeros(np.shape(input))

    def createAccSingle(self):
        return ConstantClassifierAcc(distPrev = self, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        if method == SynthMethod.Meanish:
            prob, classIndex = max([ (prob, classIndex) for classIndex, prob in enumerate(self.probs) ])
            return classIndex
        elif method == SynthMethod.Sample:
            return sampleDiscrete(list(enumerate(self.probs)))
        else:
            raise RuntimeError('unknown SynthMethod '+repr(method))

    def paramsSingle(self):
        logProbs = np.log(self.probs)
        if not np.all(np.isfinite(logProbs)):
            raise RuntimeError('this parameterization of ConstantClassifier cannot cope with zero (or NaN) probabilities (probs = '+repr(self.probs)+')')
        sumZeroLogProbs = logProbs - np.mean(logProbs)
        return sumZeroLogProbs[:-1]

    def parseSingle(self, params):
        n = len(self.probs) - 1
        p = params[:n]
        if not np.all(np.isfinite(p)):
            raise InvalidParamsError('params %s not all finite during ConstantClassifier parsing' % p)
        sumZeroLogProbs = np.append(p, -sum(p))
        assert_allclose(sum(sumZeroLogProbs), 0.0)
        probs = np.exp(sumZeroLogProbs)
        probs = probs / sum(probs)
        if not all(probs >= self.probFloors):
            raise InvalidParamsError('probs = %s not all >= probFloors = %s during ConstantClassifier parsing' % (probs, self.probFloors))
        return ConstantClassifier(probs, self.probFloors, tag = self.tag), params[n:]

    def flooredSingle(self):
        numFloored = sum([ (1 if np.allclose(prob, probFloor) else 0) for prob, probFloor in zip(self.probs, self.probFloors) ])
        if np.allclose(sum(self.probFloors), 1.0):
            assert numFloored == len(self.probs)
            return numFloored, len(self.probs)
        else:
            return numFloored, len(self.probs) - 1

class BinaryLogisticClassifier(TermDist):
    def __init__(self, coeff, coeffFloor, tag = None):
        self.coeff = coeff
        self.coeffFloor = coeffFloor
        self.tag = tag

        assert len(self.coeffFloor) == len(self.coeff)
        assert all(self.coeffFloor >= 0.0)
        assert all(np.abs(self.coeff) <= self.coeffFloor)

    def __repr__(self):
        return 'BinaryLogisticClassifier('+repr(self.coeff)+', '+repr(self.coeffFloor)+', tag = '+repr(self.tag)+')'

    def mapChildren(self, mapChild):
        return BinaryLogisticClassifier(self.coeff, self.coeffFloor, tag = self.tag)

    def logProb(self, input, classIndex):
        prob = self.prob(input, classIndex)
        return math.log(prob) if prob != 0.0 else float('-inf')

    def prob(self, input, classIndex):
        prob1 = sigmoid(np.dot(self.coeff, input))
        if classIndex == 0:
            return 1.0 - prob1
        else:
            return prob1

    def logProbDerivInput(self, input, classIndex):
        return self.coeff * (classIndex - sigmoid(np.dot(self.coeff, input)))

    def createAccSingle(self):
        return BinaryLogisticClassifierAcc(self, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        prob1 = sigmoid(np.dot(self.coeff, input))
        if method == SynthMethod.Meanish:
            if prob1 > 0.5:
                return 1
            else:
                return 0
        elif method == SynthMethod.Sample:
            if random.random() < prob1:
                return 1
            else:
                return 0
        else:
            raise RuntimeError('unknown SynthMethod '+repr(method))

    def paramsSingle(self):
        return self.coeff

    def parseSingle(self, params):
        n = len(self.coeff)
        coeff = params[:n]
        if not all(np.abs(coeff) <= self.coeffFloor):
            raise InvalidParamsError('abs(coeff = %s) not all <= coeffFloor = %s during BinaryLogisticClassifier parsing' % (coeff, self.coeffFloor))
        return BinaryLogisticClassifier(coeff, self.coeffFloor, tag = self.tag), params[n:]

    def flooredSingle(self):
        numFloored = sum([ (1 if np.allclose(abs(coeffValue), coeffFloorValue) else 0) for coeffValue, coeffFloorValue in zip(self.coeff, self.coeffFloor) ])
        return numFloored, len(self.coeff)

class MixtureDist(Dist):
    def __init__(self, classDist, regDists, tag = None):
        self.numComps = len(regDists)
        self.classDist = classDist
        self.regDists = regDists
        self.tag = tag

    def __repr__(self):
        return 'MixtureDist('+repr(self.classDist)+', '+repr(self.regDists)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.classDist] + self.regDists

    def mapChildren(self, mapChild):
        classDist = mapChild(self.classDist)
        regDists = [ mapChild(regDist) for regDist in self.regDists ]
        return MixtureDist(classDist, regDists, tag = self.tag)

    def logProb(self, input, output):
        return logSum([ self.logProbComp(input, comp, output) for comp in range(self.numComps) ])

    def logProbComp(self, input, comp, output):
        return self.classDist.logProb(input, comp) + self.regDists[comp].logProb(input, output)

    def logProbDerivInput(self, input, output):
        logTot = self.logProb(input, output)
        return np.sum([
            (regDist.logProbDerivInput(input, output) + self.classDist.logProbDerivInput(input, comp)) *
            math.exp(self.logProbComp(input, comp, output) - logTot)
            for comp, regDist in enumerate(self.regDists)
        ], axis = 0)

    def logProbDerivOutput(self, input, output):
        logTot = self.logProb(input, output)
        return np.sum([
            regDist.logProbDerivOutput(input, output) *
            math.exp(self.logProbComp(input, comp, output) - logTot)
            for comp, regDist in enumerate(self.regDists)
        ], axis = 0)

    def createAcc(self, createAccChild):
        classAcc = createAccChild(self.classDist)
        regAccs = [ createAccChild(regDist) for regDist in self.regDists ]
        return MixtureAcc(self, classAcc, regAccs, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        if method == SynthMethod.Meanish:
            return np.sum([
                regDist.synth(input, SynthMethod.Meanish, actualOutput) *
                math.exp(self.classDist.logProb(input, comp))
                for comp, regDist in enumerate(self.regDists)
            ], axis = 0)
        elif method == SynthMethod.Sample:
            comp = self.classDist.synth(input, method)
            output = self.regDists[comp].synth(input, method, actualOutput)
            return output
        else:
            raise RuntimeError('unknown SynthMethod '+repr(method))

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dists, paramsLeft = parseConcat(self.children(), params, parseChild)
        return MixtureDist(dists[0], dists[1:], tag = self.tag), paramsLeft

class IdentifiableMixtureDist(Dist):
    def __init__(self, classDist, regDists, tag = None):
        self.classDist = classDist
        self.regDists = regDists
        self.tag = tag

    def __repr__(self):
        return 'IdentifiableMixtureDist('+repr(self.classDist)+', '+repr(self.regDists)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.classDist] + self.regDists

    def mapChildren(self, mapChild):
        classDist = mapChild(self.classDist)
        regDists = [ mapChild(regDist) for regDist in self.regDists ]
        return IdentifiableMixtureDist(classDist, regDists, tag = self.tag)

    def logProb(self, input, output):
        comp, acOutput = output
        return self.classDist.logProb(input, comp) + self.regDists[comp].logProb(input, acOutput)

    def logProbDerivInput(self, input, output):
        comp, acOutput = output
        return self.regDists[comp].logProbDerivInput(input, acOutput) + self.classDist.logProbDerivInput(input, comp)

    def logProbDerivOutput(self, input, output):
        comp, acOutput = output
        return self.regDists[comp].logProbDerivOutput(input, acOutput)

    def createAcc(self, createAccChild):
        classAcc = createAccChild(self.classDist)
        regAccs = [ createAccChild(regDist) for regDist in self.regDists ]
        return IdentifiableMixtureAcc(classAcc, regAccs, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        actualComp, actualAcOutput = actualOutput if actualOutput is not None else (None, None)
        comp = self.classDist.synth(input, method, actualComp)
        acOutput = self.regDists[comp].synth(input, method, actualAcOutput)
        return comp, acOutput

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dists, paramsLeft = parseConcat(self.children(), params, parseChild)
        return IdentifiableMixtureDist(dists[0], dists[1:], tag = self.tag), paramsLeft

def createVectorDist(order, outIndices, vectorSummarizer, createDistForIndex):
    distComps = dict()
    for outIndex in outIndices:
        distComps[outIndex] = createDistForIndex(outIndex)
    return VectorDist(order, vectorSummarizer, outIndices, distComps)

class VectorDist(Dist):
    def __init__(self, order, vectorSummarizer, keys, distComps, tag = None):
        assert len(keys) == len(distComps)
        for key in keys:
            assert key in distComps
        self.order = order
        self.vectorSummarizer = vectorSummarizer
        self.keys = keys
        self.distComps = distComps
        self.tag = tag

    def __repr__(self):
        return 'VectorDist('+repr(self.order)+', '+repr(self.vectorSummarizer)+', '+repr(self.keys)+', '+orderedDictRepr(self.keys, self.distComps)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [ self.distComps[key] for key in self.keys ]

    def mapChildren(self, mapChild):
        distComps = dict()
        for outIndex in self.distComps:
            distComps[outIndex] = mapChild(self.distComps[outIndex])
        return VectorDist(self.order, self.vectorSummarizer, self.keys, distComps, tag = self.tag)

    def logProb(self, input, output):
        lp = 0.0
        for outIndex in self.distComps:
            summary = self.vectorSummarizer(input, output[:outIndex], outIndex)
            lp += self.distComps[outIndex].logProb(summary, output[outIndex])
        return lp

    def logProbDerivInput(self, input, output):
        # FIXME : complete
        notyetimplemented

    def logProbDerivOutput(self, input, output):
        # FIXME : complete
        notyetimplemented

    def createAcc(self, createAccChild):
        accComps = dict()
        for outIndex in self.distComps:
            accComps[outIndex] = createAccChild(self.distComps[outIndex])
        return VectorAcc(self.order, self.vectorSummarizer, self.keys, accComps, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        partialOutput = []
        for outIndex in range(self.order):
            if not outIndex in self.distComps:
                out = actualOutput[outIndex]
            else:
                summary = self.vectorSummarizer(input, partialOutput, outIndex)
                out = self.distComps[outIndex].synth(summary, method, actualOutput[outIndex] if actualOutput is not None else None)
            partialOutput.append(out)
        return partialOutput

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dists, paramsLeft = parseConcat(self.children(), params, parseChild)
        return VectorDist(self.order, self.vectorSummarizer, self.keys, dict(zip(self.keys, dists)), tag = self.tag), paramsLeft

def createDiscreteDist(keys, createDistFor):
    distDict = dict()
    for key in keys:
        distDict[key] = createDistFor(key)
    return DiscreteDist(keys, distDict)

class DiscreteDist(Dist):
    def __init__(self, keys, distDict, tag = None):
        assert len(keys) == len(distDict)
        for key in keys:
            assert key in distDict
        self.keys = keys
        self.distDict = distDict
        self.tag = tag

    def __repr__(self):
        return 'DiscreteDist('+repr(self.keys)+', '+orderedDictRepr(self.keys, self.distDict)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [ self.distDict[key] for key in self.keys ]

    def mapChildren(self, mapChild):
        distDict = dict()
        for label in self.distDict:
            distDict[label] = mapChild(self.distDict[label])
        return DiscreteDist(self.keys, distDict, tag = self.tag)

    def logProb(self, input, output):
        label, acInput = input
        return self.distDict[label].logProb(acInput, output)

    def logProbDerivInput(self, input, output):
        label, acInput = input
        return self.distDict[label].logProbDerivInput(acInput, output)

    def logProbDerivOutput(self, input, output):
        label, acInput = input
        return self.distDict[label].logProbDerivOutput(acInput, output)

    def createAcc(self, createAccChild):
        accDict = dict()
        for label in self.distDict:
            accDict[label] = createAccChild(self.distDict[label])
        return DiscreteAcc(self.keys, accDict, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        label, acInput = input
        return self.distDict[label].synth(acInput, method, actualOutput)

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dists, paramsLeft = parseConcat(self.children(), params, parseChild)
        return DiscreteDist(self.keys, dict(zip(self.keys, dists)), tag = self.tag), paramsLeft

class DecisionTree(Dist):
    pass

class DecisionTreeNode(DecisionTree):
    def __init__(self, fullQuestion, distYes, distNo, tag = None):
        self.fullQuestion = fullQuestion
        self.distYes = distYes
        self.distNo = distNo
        self.tag = tag

    def __repr__(self):
        return 'DecisionTreeNode('+repr(self.fullQuestion)+', '+repr(self.distYes)+', '+repr(self.distNo)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.distYes, self.distNo]

    def mapChildren(self, mapChild):
        return DecisionTreeNode(self.fullQuestion, mapChild(self.distYes), mapChild(self.distNo), tag = self.tag)

    def logProb(self, input, output):
        label, acInput = input
        labelValuer, question = self.fullQuestion
        if question(labelValuer(label)):
            return self.distYes.logProb(input, output)
        else:
            return self.distNo.logProb(input, output)

    def logProbDerivInput(self, input, output):
        label, acInput = input
        labelValuer, question = self.fullQuestion
        if question(labelValuer(label)):
            return self.distYes.logProbDerivInput(input, output)
        else:
            return self.distNo.logProbDerivInput(input, output)

    def logProbDerivOutput(self, input, output):
        label, acInput = input
        labelValuer, question = self.fullQuestion
        if question(labelValuer(label)):
            return self.distYes.logProbDerivOutput(input, output)
        else:
            return self.distNo.logProbDerivOutput(input, output)

    def createAcc(self, createAccChild):
        return DecisionTreeAccNode(self.fullQuestion, createAccChild(self.distYes), createAccChild(self.distNo), tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        label, acInput = input
        labelValuer, question = self.fullQuestion
        if question(labelValuer(label)):
            return self.distYes.synth(input, method, actualOutput)
        else:
            return self.distNo.synth(input, method, actualOutput)

    def countLeaves(self):
        return self.distYes.countLeaves() + self.distNo.countLeaves()

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        paramsLeft = params
        distYes, paramsLeft = parseChild(self.distYes, paramsLeft)
        distNo, paramsLeft = parseChild(self.distNo, paramsLeft)
        return DecisionTreeNode(self.fullQuestion, distYes, distNo, tag = self.tag), paramsLeft

class DecisionTreeLeaf(DecisionTree):
    def __init__(self, dist, tag = None):
        self.dist = dist
        self.tag = tag

    def __repr__(self):
        return 'DecisionTreeLeaf('+repr(self.dist)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.dist]

    def mapChildren(self, mapChild):
        return DecisionTreeLeaf(mapChild(self.dist), tag = self.tag)

    def logProb(self, input, output):
        label, acInput = input
        return self.dist.logProb(acInput, output)

    def logProbDerivInput(self, input, output):
        label, acInput = input
        return self.dist.logProbDerivInput(acInput, output)

    def logProbDerivOutput(self, input, output):
        label, acInput = input
        return self.dist.logProbDerivOutput(acInput, output)

    def createAcc(self, createAccChild):
        return DecisionTreeAccLeaf(createAccChild(self.dist), tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        label, acInput = input
        return self.dist.synth(acInput, method, actualOutput)

    def countLeaves(self):
        return 1

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dist, paramsLeft = parseChild(self.dist, params)
        return DecisionTreeLeaf(dist, tag = self.tag), paramsLeft

# (FIXME : merge MappedInputDist with TransformedInputDist? (Also merge some of the corresponding Accs?))
class MappedInputDist(Dist):
    """Dist where input is mapped using a fixed transform."""
    def __init__(self, inputTransform, dist, tag = None):
        self.inputTransform = inputTransform
        self.dist = dist
        self.tag = tag

    def __repr__(self):
        return 'MappedInputDist('+repr(self.inputTransform)+', '+repr(self.dist)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.dist]

    def mapChildren(self, mapChild):
        dist = mapChild(self.dist)
        return MappedInputDist(self.inputTransform, dist, tag = self.tag)

    def logProb(self, input, output):
        return self.dist.logProb(self.inputTransform(input), output)

    # (FIXME : not ideal to have to have this here)
    def logProb_frames(self, input, output):
        return self.dist.logProb_frames(self.inputTransform(input), output)

    def logProbDerivInput(self, input, output):
        return np.dot(
            self.inputTransform.deriv(input),
            self.dist.logProbDerivInput(self.inputTransform(input), output)
        )

    def logProbDerivOutput(self, input, output):
        return self.dist.logProbDerivOutput(self.inputTransform(input), output)

    # (FIXME : not ideal to have to have this here)
    def arError_frames(self, input, output, distError):
        return self.dist.arError_frames(self.inputTransform(input), output, distError)

    def createAcc(self, createAccChild):
        acc = createAccChild(self.dist)
        return MappedInputAcc(self.inputTransform, acc, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        return self.dist.synth(self.inputTransform(input), method, actualOutput)

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dist, paramsLeft = parseChild(self.dist, params)
        return MappedInputDist(self.inputTransform, dist, tag = self.tag), paramsLeft

# (FIXME : merge MappedOutputDist with TransformedOutputDist? (Also merge some of the corresponding Accs?))
class MappedOutputDist(Dist):
    """Dist where output is mapped using a fixed transform."""
    def __init__(self, outputTransform, dist, tag = None):
        self.outputTransform = outputTransform
        self.dist = dist
        self.tag = tag

    def __repr__(self):
        return 'MappedOutputDist('+repr(self.outputTransform)+', '+repr(self.dist)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.dist]

    def mapChildren(self, mapChild):
        dist = mapChild(self.dist)
        return MappedOutputDist(self.outputTransform, dist, tag = self.tag)

    def logProb(self, input, output):
        return self.dist.logProb(input, self.outputTransform(input, output)) + self.outputTransform.logJac(input, output)

    def logProbDerivInput(self, input, output):
        outputT = self.outputTransform(input, output)
        return np.dot(
            self.outputTransform.derivInput(input, output),
            self.dist.logProbDerivOutput(input, outputT)
        ) + self.dist.logProbDerivInput(input, outputT) + self.outputTransform.logJacDerivInput(input, output)

    def logProbDerivOutput(self, input, output):
        return np.dot(
            self.outputTransform.deriv(input, output),
            self.dist.logProbDerivOutput(input, self.outputTransform(input, output))
        ) + self.outputTransform.logJacDeriv(input, output)

    def createAcc(self, createAccChild):
        acc = createAccChild(self.dist)
        return MappedOutputAcc(self.outputTransform, acc, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        return self.outputTransform.inv(input,
            self.dist.synth(input, method, None if actualOutput is None else self.outputTransform(input, actualOutput))
        )

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dist, paramsLeft = parseChild(self.dist, params)
        return MappedOutputDist(self.outputTransform, dist, tag = self.tag), paramsLeft

class TransformedInputDist(Dist):
    """Dist where input is transformed using a learnable transform."""
    def __init__(self, inputTransform, dist, tag = None):
        self.inputTransform = inputTransform
        self.dist = dist
        self.tag = tag

    def __repr__(self):
        return 'TransformedInputDist('+repr(self.inputTransform)+', '+repr(self.dist)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.inputTransform, self.dist]

    def mapChildren(self, mapChild):
        inputTransform = mapChild(self.inputTransform)
        dist = mapChild(self.dist)
        return TransformedInputDist(inputTransform, dist, tag = self.tag)

    def logProb(self, input, output):
        return self.dist.logProb(self.inputTransform(input), output)

    def logProbDerivInput(self, input, output):
        return np.dot(
            self.inputTransform.deriv(input),
            self.dist.logProbDerivInput(self.inputTransform(input), output)
        )

    def logProbDerivOutput(self, input, output):
        return self.dist.logProbDerivOutput(self.inputTransform(input), output)

    def createAcc(self, createAccChild, estTransform = False):
        if estTransform:
            inputTransformAcc = createAccChild(self.inputTransform)
            return TransformedInputLearnTransformAccEM(inputTransformAcc, self.dist, tag = self.tag)
        else:
            acc = createAccChild(self.dist)
            return TransformedInputLearnDistAccEM(self.inputTransform, acc, tag = self.tag)

    def createAccG(self, createAccChild):
        inputTransformAcc = createAccChild(self.inputTransform)
        acc = createAccChild(self.dist)
        return TransformedInputAccG((inputTransformAcc, self.inputTransform), (acc, self.dist), tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        return self.dist.synth(self.inputTransform(input), method, actualOutput)

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        inputTransform, paramsLeft = parseChild(self.inputTransform, params)
        dist, paramsLeft = parseChild(self.dist, paramsLeft)
        return TransformedInputDist(inputTransform, dist, tag = self.tag), paramsLeft

class TransformedOutputDist(Dist):
    """Dist where output is transformed using a learnable transform."""
    def __init__(self, outputTransform, dist, tag = None):
        self.outputTransform = outputTransform
        self.dist = dist
        self.tag = tag

    def __repr__(self):
        return 'TransformedOutputDist('+repr(self.outputTransform)+', '+repr(self.dist)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.outputTransform, self.dist]

    def mapChildren(self, mapChild):
        outputTransform = mapChild(self.outputTransform)
        dist = mapChild(self.dist)
        return TransformedOutputDist(outputTransform, dist, tag = self.tag)

    def logProb(self, input, output):
        return self.dist.logProb(input, self.outputTransform(input, output)) + self.outputTransform.logJac(input, output)

    def logProbDerivInput(self, input, output):
        outputT = self.outputTransform(input, output)
        return np.dot(
            self.outputTransform.derivInput(input, output),
            self.dist.logProbDerivOutput(input, outputT)
        ) + self.dist.logProbDerivInput(input, outputT) + self.outputTransform.logJacDerivInput(input, output)

    def logProbDerivOutput(self, input, output):
        return np.dot(
            self.outputTransform.deriv(input, output),
            self.dist.logProbDerivOutput(input, self.outputTransform(input, output))
        ) + self.outputTransform.logJacDeriv(input, output)

    def createAcc(self, createAccChild, estTransform = False):
        if estTransform:
            outputTransformAcc = createAccChild(self.outputTransform)
            return TransformedOutputLearnTransformAccEM(outputTransformAcc, self.dist, tag = self.tag)
        else:
            acc = createAccChild(self.dist)
            return TransformedOutputLearnDistAccEM(self.outputTransform, acc, tag = self.tag)

    def createAccG(self, createAccChild):
        outputTransformAcc = createAccChild(self.outputTransform)
        acc = createAccChild(self.dist)
        return TransformedOutputAccG((outputTransformAcc, self.outputTransform), (acc, self.dist), tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        return self.outputTransform.inv(input,
            self.dist.synth(input, method, None if actualOutput is None else self.outputTransform(input, actualOutput))
        )

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        outputTransform, paramsLeft = parseChild(self.outputTransform, params)
        dist, paramsLeft = parseChild(self.dist, paramsLeft)
        return TransformedOutputDist(outputTransform, dist, tag = self.tag), paramsLeft

class PassThruDist(Dist):
    def __init__(self, dist, tag = None):
        self.dist = dist
        self.tag = tag

    def __repr__(self):
        return 'PassThruDist('+repr(self.dist)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.dist]

    def mapChildren(self, mapChild):
        return PassThruDist(mapChild(self.dist), tag = self.tag)

    def logProb(self, input, output):
        return self.dist.logProb(input, output)

    def logProbDerivInput(self, input, output):
        return self.dist.logProbDerivInput(input, output)

    def logProbDerivOutput(self, input, output):
        return self.dist.logProbDerivOutput(input, output)

    def createAcc(self, createAccChild):
        return PassThruAcc(createAccChild(self.dist), tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        return self.dist.synth(input, method, actualOutput)

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dist, paramsLeft = parseChild(self.dist, params)
        return PassThruDist(dist, tag = self.tag), paramsLeft

class DebugDist(Dist):
    def __init__(self, maxOcc, dist, tag = None):
        self.maxOcc = maxOcc
        self.dist = dist
        self.tag = tag

    def __repr__(self):
        return 'DebugDist('+repr(self.maxOcc)+', '+repr(self.dist)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.dist]

    def mapChildren(self, mapChild):
        return DebugDist(self.maxOcc, mapChild(self.dist), tag = self.tag)

    def logProb(self, input, output):
        return self.dist.logProb(input, output)

    def logProbDerivInput(self, input, output):
        return self.dist.logProbDerivInput(input, output)

    def logProbDerivOutput(self, input, output):
        return self.dist.logProbDerivOutput(input, output)

    def createAcc(self, createAccChild):
        return DebugAcc(self.maxOcc, createAccChild(self.dist), tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutput = None):
        return self.dist.synth(input, method, actualOutput)

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dist, paramsLeft = parseChild(self.dist, params)
        return DebugDist(self.maxOcc, dist, tag = self.tag), paramsLeft

class AutoregressiveSequenceDist(Dist):
    def __init__(self, depth, dist, tag = None):
        self.depth = depth
        self.dist = dist
        self.tag = tag

    def __repr__(self):
        return 'AutoregressiveSequenceDist('+repr(self.depth)+', '+repr(self.dist)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.dist]

    def mapChildren(self, mapChild):
        return AutoregressiveSequenceDist(self.depth, mapChild(self.dist), tag = self.tag)

    def logProb(self, inSeq, outSeq):
        lp = 0.0
        assert len(inSeq) == len(outSeq)
        for inFrame, (outContext, outFrame) in izip(inSeq, contextualizeIter(self.depth, outSeq)):
            if len(outContext) == self.depth:
                lp += self.dist.logProb((inFrame, outContext), outFrame)
        return lp

    def logProb_frames(self, inSeq, outSeq):
        lp = 0.0
        frames = 0
        assert len(inSeq) == len(outSeq)
        for inFrame, (outContext, outFrame) in izip(inSeq, contextualizeIter(self.depth, outSeq)):
            if len(outContext) == self.depth:
                lp += self.dist.logProb((inFrame, outContext), outFrame)
                frames += 1
        return lp, frames

    def logProbDerivInput(self, input, output):
        # FIXME : complete
        notyetimplemented

    def logProbDerivOutput(self, input, output):
        # FIXME : complete
        notyetimplemented

    def arError_frames(self, inSeq, outSeq, distError):
        error = 0.0
        frames = 0
        assert len(inSeq) == len(outSeq)
        for inFrame, (outContext, outFrame) in izip(inSeq, contextualizeIter(self.depth, outSeq)):
            if len(outContext) == self.depth:
                error += distError(self.dist, (inFrame, outContext), outFrame)
                frames += 1
        return error, frames

    def createAcc(self, createAccChild):
        return AutoregressiveSequenceAcc(self.depth, createAccChild(self.dist), tag = self.tag)

    def synth(self, inSeq, method = SynthMethod.Sample, actualOutSeq = None):
        return list(self.synthIterator(inSeq, method, actualOutSeq))

    def synthIterator(self, inSeq, method = SynthMethod.Sample, actualOutSeq = None):
        outContext = deque()
        assert len(inSeq) == len(actualOutSeq)
        for inFrame, actualOutFrame in izip(inSeq, actualOutSeq):
            if len(outContext) != self.depth:
                outFrame = actualOutFrame
            else:
                outFrame = self.dist.synth((inFrame, list(outContext)), method, actualOutFrame)

            yield outFrame

            outContext.append(outFrame)
            if len(outContext) > self.depth:
                outContext.popleft()

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        dist, paramsLeft = parseChild(self.dist, params)
        return AutoregressiveSequenceDist(self.depth, dist, tag = self.tag), paramsLeft

class SimpleLeftToRightNetFor(object):
    def __init__(self, subLabels):
        self.subLabels = subLabels
    def __repr__(self):
        return 'SimpleLeftToRightNetFor('+repr(self.subLabels)+')'
    def __call__(self, labelSeq):
        net = wnet.FlatMappedNet(
            lambda label: wnet.probLeftToRightNet(
                [ (label, subLabel) for subLabel in self.subLabels ],
                [ [ ((label, subLabel), adv) for adv in [0, 1] ] for subLabel in self.subLabels ]
            ),
            wnet.SequenceNet(labelSeq, None)
        )
        return net

class AutoregressiveNetDist(Dist):
    """An autoregressive distribution over sequences.

    The generative model is that for each input we have a net, and we jump
    forwards through this net probabilistically, generating acoustic output at
    the emitting nodes. The conditional probability of a given transition in the
    net is specified by durDist and the conditional probability of a given
    emission is specified by acDist. Each transition and each emission are
    allowed to be conditioned on the previous emissions up to a given depth. We
    stop emitting when we reach the end node of the net. This whole process is
    therefore a generative model which takes some input and produces a finite
    acoustic output sequence outSeq, where outSeq[t] is the acoustic output at
    "time" t.

    netFor is a function which takes some input and returns a net. The form of
    input is arbitrary, and is only passed to netFor. The emitting nodes of this
    net should be labelled by phonetic context, and the non-None edges should be
    labelled by (phonetic context, phonetic output) pairs. Here phonetic context
    and phonetic output are arbitrary user-specified data. durDist should have a
    (phonetic context, acoustic context) pair as input and a phonetic output as
    output. acDist should have a (phonetic context, acoustic context) pair as
    input and an acoustic output as output. Here the acoustic context at time t
    is defined as outSeq[max(t - depth, 0):t]. The net returned by netFor should
    contain no non-emitting cycles.

    This class internally expands the net specified by netFor, adding acoustic
    context as appropriate, to form a new "unrolled" net. Each transition in the
    original net has conditional log probability specified by durDist.logProb
    and each emission in the original net has conditional log probability
    specified by acDist.logProb. The conditional log probability of edges with
    label None is fixed at 0.0. As a consistency condition, for any node in the
    original net and for any acoustic context the sum of the probabilities of
    all edges leaving that node forwards must be 1.0. (During synthesis, this
    condition is checked for all nodes along the chosen path). The easiest and
    most natural way to satisfy this condition is as follows -- for each node
    with non-None edges leaving it, use the same phonetic context for all these
    edges, and have the phonetic output for the different edges correspond
    (bijectively) to the set of possible outputs given by durDist. For example,
    if durDist for a given phonetic context (and for any acoustic context) is a
    distribution over [0, 1] and we wish to use this phonetic context for a
    given node, then there should be two edges leaving this node, one with
    phonetic output 0 and one with phonetic output 1, and both with the given
    phonetic context.
    """
    def __init__(self, depth, netFor, durDist, acDist, pruneSpec, tag = None):
        self.depth = depth
        self.netFor = netFor
        self.durDist = durDist
        self.acDist = acDist
        # (FIXME : could argue pruneSpec should be specified as part of
        #   createAcc rather than part of dist itself. Would make it clumsier
        #   to use pruning during logProb computation, though, which we
        #   probably want to do.)
        self.pruneSpec = pruneSpec
        self.tag = tag

        self.ring = semiring.logRealsField

    def __repr__(self):
        return 'AutoregressiveNetDist('+repr(self.depth)+', '+repr(self.netFor)+', '+repr(self.durDist)+', '+repr(self.acDist)+', '+repr(self.pruneSpec)+', tag = '+repr(self.tag)+')'

    def children(self):
        return [self.durDist, self.acDist]

    def mapChildren(self, mapChild):
        return AutoregressiveNetDist(self.depth, self.netFor, mapChild(self.durDist), mapChild(self.acDist), self.pruneSpec, tag = self.tag)

    def getNet(self, input):
        net0 = self.netFor(input)
        net1 = wnet.MappedLabelNet(lambda (phInput, phOutput): (False, phInput, phOutput), net0)
        net2 = wnet.FlatMappedNet(lambda phInput: wnet.TrivialNet((True, phInput)), net1)
        def deltaTime(label):
            return 0 if label is None or not label[0] else 1
        net = wnet.concretizeNetTopSort(net2, deltaTime)
        return net, deltaTime

    def getTimedNet(self, input, outSeq, preComputeLabelToWeight = False):
        net, deltaTime = self.getNet(input)
        timedNet = wnet.UnrolledNet(net, startTime = 0, endTime = len(outSeq), deltaTime = deltaTime)
        labelToWeight = self.getLabelToWeight(outSeq)

        if preComputeLabelToWeight:
            times = range(len(outSeq) + 1)
            times0 = zip(times, times)
            times1 = zip(times, times[1:])
            for node in wnet.nodeSetCompute(net, accessibleOnly = False):
                for label, nextNode in net.next(node, forwards = True):
                    delta = deltaTime(label)
                    assert delta == 0 or delta == 1
                    for labelStartTime, labelEndTime in (times0 if delta == 0 else times1):
                        labelToWeight((label, labelStartTime, labelEndTime))

        return timedNet, labelToWeight

    def getLabelToWeight(self, outSeq):
        def timedLabelToLogProb((label, labelStartTime, labelEndTime)):
            if label is None:
                return 0.0
            else:
                acInput = outSeq[max(labelStartTime - self.depth, 0):labelStartTime]
                if not label[0]:
                    _, phInput, phOutput = label
                    return self.durDist.logProb((phInput, acInput), phOutput)
                else:
                    _, phInput = label
                    assert labelEndTime == labelStartTime + 1
                    acOutput = outSeq[labelStartTime]
                    return self.acDist.logProb((phInput, acInput), acOutput)
        return memoize(timedLabelToLogProb)

    def getAgenda(self, forwards):
        def negMap((time, nodeIndex)):
            return -time, -nodeIndex
        def pruneTrigger(nodePrevPop, nodeCurrPop):
            # compare times
            return (nodePrevPop[0] != nodeCurrPop[0])
        pruneThresh = None if self.pruneSpec is None else self.pruneSpec.betaThresh
        agenda = wnet.PriorityQueueSumAgenda(self.ring, forwards, negMap = negMap, pruneThresh = pruneThresh, pruneTrigger = pruneTrigger)
        return agenda

    def logProb(self, input, outSeq):
        timedNet, labelToWeight = self.getTimedNet(input, outSeq)
        totalLogProb = wnet.sum(timedNet, labelToWeight = labelToWeight, ring = self.ring, getAgenda = self.getAgenda)
        return totalLogProb

    def logProb_frames(self, input, outSeq):
        return self.logProb(input, outSeq), len(outSeq)

    def logProbDerivOutput(self, input, output):
        # FIXME : complete
        notyetimplemented

    def arError_frames(self, input, output, distError):
        # FIXME : complete (compute using expectation semiring?)
        notyetimplemented

    def createAcc(self, createAccChild, verbosity = 0):
        return AutoregressiveNetAcc(distPrev = self, durAcc = createAccChild(self.durDist), acAcc = createAccChild(self.acDist), verbosity = verbosity, tag = self.tag)

    def synth(self, input, method = SynthMethod.Sample, actualOutSeq = None, maxLength = None):
        # (FIXME : align actualOutSeq and pass down to frames below?  (What exactly do I mean?))
        # (FIXME : can we do anything simple and reasonable with durations for meanish case?)
        forwards = True
        net = self.netFor(input)
        startNode = net.start(forwards)
        endNode = net.end(forwards)
        assert net.elem(startNode) is None
        assert net.elem(endNode) is None

        outSeq = []
        acInput = []
        node = startNode
        while node != endNode:
            nodedProbs = []
            for label, nextNode in net.next(node, forwards):
                if label is None:
                    nodedProbs.append((nextNode, 1.0))
                else:
                    phInput, phOutput = label
                    logProb = self.durDist.logProb((phInput, acInput), phOutput)
                    nodedProbs.append((nextNode, math.exp(logProb)))
            node = sampleDiscrete(nodedProbs)
            elem = net.elem(node)
            if elem is not None:
                phInput = elem
                acOutput = self.acDist.synth((phInput, acInput), method)
                outSeq.append(acOutput)
                time = len(outSeq)
                acInput = outSeq[max(time - self.depth, 0):time]
            if maxLength is not None and len(outSeq) > maxLength:
                raise SynthSeqTooLongError('maximum length '+str(maxLength)+' exceeded during synth from AutoregressiveNetDist')

        return outSeq

    def paramsSingle(self):
        return []

    def parseSingle(self, params):
        return self, params

    def parseChildren(self, params, parseChild):
        paramsLeft = params
        durDist, paramsLeft = parseChild(self.durDist, paramsLeft)
        acDist, paramsLeft = parseChild(self.acDist, paramsLeft)
        return AutoregressiveNetDist(self.depth, self.netFor, durDist, acDist, self.pruneSpec, tag = self.tag), paramsLeft
