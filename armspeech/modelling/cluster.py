"""Clustering algorithms."""

# Copyright 2011, 2012, 2013 Matt Shannon

# This file is part of armspeech.
# See `License` for details of license and warranty.


import dist as d
from armspeech.util.mathhelp import assert_allclose
from armspeech.util.timing import timed
from codedep import codeDeps

import logging
import math
from collections import defaultdict

@codeDeps()
class ProtoLeaf(object):
    def __init__(self, dist, aux, auxRat, count):
        self.dist = dist
        self.aux = aux
        self.auxRat = auxRat
        self.count = count

@codeDeps(assert_allclose)
class SplitInfo(object):
    """Collected information for a (potential or actual) split."""
    def __init__(self, protoNoSplit, fullQuestion, protoYes, protoNo):
        self.protoNoSplit = protoNoSplit
        self.fullQuestion = fullQuestion
        self.protoYes = protoYes
        self.protoNo = protoNo

        assert self.protoNoSplit is not None
        if self.fullQuestion is None:
            assert self.protoYes is None and self.protoNo is None
        if self.protoYes is not None and self.protoNo is not None:
            assert_allclose(self.protoYes.count + self.protoNo.count,
                            self.protoNoSplit.count)

    def delta(self):
        """Returns the delta for this split.

        The delta is used to choose which question to use to split a given node
        and to decide whether to split at all.
        """
        if self.protoYes is None or self.protoNo is None:
            return float('-inf')
        else:
            return self.protoYes.aux + self.protoNo.aux - self.protoNoSplit.aux

@codeDeps()
class Grower(object):
    def allowSplit(self, splitInfo):
        abstract

    def useSplit(self, splitInfo):
        abstract

@codeDeps(ProtoLeaf, SplitInfo, d.DecisionTreeLeaf, d.DecisionTreeNode,
    d.EstimationError, d.addAcc, d.sumValuedRats, timed
)
class DecisionTreeClusterer(object):
    def __init__(self, accForLabel, questionGroups, createAcc, estimateTotAux,
                 verbosity):
        self.accForLabel = accForLabel
        self.questionGroups = questionGroups
        self.createAcc = createAcc
        self.estimateTotAux = estimateTotAux
        self.verbosity = verbosity

    def getAccFromLabels(self, labels):
        accForLabel = self.accForLabel
        accTot = self.createAcc()
        for label in labels:
            d.addAcc(accTot, accForLabel(label))
        return accTot

    def getProto(self, acc):
        try:
            dist, (aux, auxRat) = self.estimateTotAux(acc)
        except d.EstimationError:
            return None
        count = acc.count()
        return ProtoLeaf(dist, aux, auxRat, count)

    def findBestSplit(self, protoNoSplit, splitInfos):
        bestSplitInfo = SplitInfo(protoNoSplit, None, None, None)
        for splitInfo in splitInfos:
            if splitInfo.delta() > bestSplitInfo.delta():
                bestSplitInfo = splitInfo
        return bestSplitInfo

    def computeBestSplit(self, state, grower, questionGroups):
        labels, isYesList, protoNoSplit = state

        def getProtosForQuestion(labelValueToAcc, question):
            accYes = self.createAcc()
            accNo = self.createAcc()
            for labelValue, acc in labelValueToAcc.iteritems():
                if question(labelValue):
                    d.addAcc(accYes, acc)
                else:
                    d.addAcc(accNo, acc)

            return self.getProto(accYes), self.getProto(accNo)

        accForLabel = self.accForLabel
        labelToValueToAccs = [
            (labelValuer, defaultdict(self.createAcc))
            for labelValuer, questions in questionGroups
        ]
        labelValueToAccs = [ labelValueToAcc
                             for labelValuer, labelValueToAcc
                             in labelToValueToAccs ]
        for label in labels:
            acc = accForLabel(label)
            for labelValuer, labelValueToAcc in labelToValueToAccs:
                d.addAcc(labelValueToAcc[labelValuer(label)], acc)

        def getSplitInfos(labelValueToAccs, questionGroups):
            for (
                labelValueToAcc, (labelValuer, questions)
            ) in zip(labelValueToAccs, questionGroups):
                for question in questions:
                    protoYes, protoNo = getProtosForQuestion(labelValueToAcc,
                                                             question)
                    fullQuestion = labelValuer, question
                    splitInfo = SplitInfo(protoNoSplit, fullQuestion,
                                          protoYes, protoNo)
                    if grower.allowSplit(splitInfo):
                        yield splitInfo

        splitInfos = getSplitInfos(labelValueToAccs, questionGroups)
        return self.findBestSplit(protoNoSplit, splitInfos)

    def decideSplit(self, labels, isYesList, splitInfo, grower):
        if self.verbosity >= 2:
            indent = '    '+''.join([ ('|  ' if isYes else '   ')
                                      for isYes in isYesList ])
        if grower.useSplit(splitInfo):
            labelValuer, question = splitInfo.fullQuestion
            if self.verbosity >= 2:
                print ('cluster:%squestion ( %s %s ) ( delta = %s )' %
                       (indent, labelValuer.shortRepr(), question.shortRepr(),
                        splitInfo.delta()))
            labelsYes = []
            labelsNo = []
            for label in labels:
                if question(labelValuer(label)):
                    labelsYes.append(label)
                else:
                    labelsNo.append(label)

            return ((labelsYes, isYesList + [True], splitInfo.protoYes),
                    (labelsNo, isYesList + [False], splitInfo.protoNo))
        else:
            if self.verbosity >= 2:
                print 'cluster:'+indent+'leaf'
            return None, None

    def findBestSplitAndDecide(self, state, grower, *splitInfos):
        labels, isYesList, protoNoSplit = state

        splitInfo = self.findBestSplit(protoNoSplit, splitInfos)
        splitInfoDict = dict()
        splitInfoDict[tuple(isYesList)] = splitInfo
        stateYes, stateNo = self.decideSplit(labels, isYesList, splitInfo,
                                             grower)
        return splitInfoDict, stateYes, stateNo

    def computeBestSplitAndDecide(self, state, grower):
        labels, isYesList, protoNoSplit = state

        if self.verbosity >= 3:
            indent = '    '+''.join([ ('|  ' if isYes else '   ')
                                      for isYes in isYesList ])
            splitInfo = timed(
                self.computeBestSplit,
                msg = 'cluster:%schoose split took' % indent
            )(state, grower, self.questionGroups)
        else:
            splitInfo = self.computeBestSplit(state, grower,
                                              self.questionGroups)

        splitInfoDict = dict()
        splitInfoDict[tuple(isYesList)] = splitInfo
        stateYes, stateNo = self.decideSplit(labels, isYesList, splitInfo,
                                             grower)
        return splitInfoDict, stateYes, stateNo

    def printNodeInfo(self, state):
        labels, isYesList, protoNoSplit = state

        indent = '    '+''.join([ ('|  ' if isYes else '   ')
                                  for isYes in isYesList[:-1] ])
        if not isYesList:
            extra = ''
        elif isYesList[-1]:
            extra = '|->'
        else:
            extra = '\->'
        print ('cluster:%s%snode ( count = %s , remaining labels = %s )' %
               (indent, extra, protoNoSplit.count, len(labels)))

    def subTreeSplitInfoDict(self, stateInit, grower):
        splitInfoDict = dict()
        agenda = [stateInit]
        while agenda:
            state = agenda.pop()
            if self.verbosity >= 2:
                self.printNodeInfo(state)
            splitInfoDictOne, stateYes, stateNo = (
                self.computeBestSplitAndDecide(state, grower)
            )
            assert all([ path not in splitInfoDict
                         for path in splitInfoDictOne ])
            splitInfoDict.update(splitInfoDictOne)
            if stateNo is not None:
                agenda.append(stateNo)
            if stateYes is not None:
                agenda.append(stateYes)
        return splitInfoDict

    def combineSplitInfoDicts(self, splitInfoDicts):
        splitInfoDictTot = dict()
        for splitInfoDict in splitInfoDicts:
            for path, splitInfo in splitInfoDict.iteritems():
                assert path not in splitInfoDictTot
                splitInfoDictTot[path] = splitInfo
        return splitInfoDictTot

    def growTree(self, splitInfoDict, grower):
        def grow(isYesList):
            splitInfo = splitInfoDict[tuple(isYesList)]
            if grower.useSplit(splitInfo):
                distYes, auxValuedRatYes = grow(isYesList + [True])
                distNo, auxValuedRatNo = grow(isYesList + [False])
                auxValuedRat = d.sumValuedRats([auxValuedRatYes,
                                                auxValuedRatNo])
                distNew = d.DecisionTreeNode(splitInfo.fullQuestion,
                                             distYes, distNo)
                return distNew, auxValuedRat
            else:
                protoNoSplit = splitInfo.protoNoSplit
                auxValuedRat = protoNoSplit.aux, protoNoSplit.auxRat
                return d.DecisionTreeLeaf(protoNoSplit.dist), auxValuedRat

        return grow([])

@codeDeps(Grower)
class SimpleGrower(Grower):
    def __init__(self, thresh, minCount, maxCount = None):
        self.thresh = thresh
        self.minCount = minCount
        self.maxCount = maxCount

    def allowSplit(self, splitInfo):
        protoYes = splitInfo.protoYes
        protoNo = splitInfo.protoNo
        return (protoYes is not None and
                protoNo is not None and
                protoYes.count >= self.minCount and
                protoNo.count >= self.minCount)

    def useSplit(self, splitInfo):
        protoNoSplit = splitInfo.protoNoSplit
        allowNoSplit = (self.maxCount is None or
                        protoNoSplit.count <= self.maxCount)

        if splitInfo.fullQuestion is not None and (
            not allowNoSplit or splitInfo.delta() > self.thresh
        ):
            return True
        else:
            if not allowNoSplit:
                assert splitInfo.fullQuestion is None
                logging.warning('not splitting decision tree node even though'
                                ' count = %s > maxCount = %s, since no further'
                                ' splitting allowed' %
                                (protoNoSplit.count, self.maxCount))
            return False

@codeDeps(DecisionTreeClusterer, SimpleGrower, d.Rat,
    d.getDefaultEstimateTotAuxNoRevert, d.getDefaultParamSpec
)
def decisionTreeCluster(labels, accForLabel, createAcc, questionGroups,
                        thresh, minCount, maxCount = None, mdlFactor = 1.0,
                        estimateTotAux = d.getDefaultEstimateTotAuxNoRevert(),
                        paramSpec = d.getDefaultParamSpec(),
                        verbosity = 2):
    clusterer = DecisionTreeClusterer(accForLabel, questionGroups, createAcc,
                                      estimateTotAux, verbosity)

    protoRoot = clusterer.getProto(clusterer.getAccFromLabels(labels))
    countRoot = protoRoot.count
    if thresh is None:
        numParamsPerLeaf = len(paramSpec.params(protoRoot.dist))
        thresh = 0.5 * mdlFactor * numParamsPerLeaf * math.log(countRoot + 1.0)
        if verbosity >= 1:
            print ('cluster: setting thresh using MDL: mdlFactor = %s and'
                   ' numParamsPerLeaf = %s and count = %s' %
                   (mdlFactor, numParamsPerLeaf, countRoot))
    grower = SimpleGrower(thresh, minCount, maxCount)
    if verbosity >= 1:
        print ('cluster: decision tree clustering with thresh = %s and'
               ' minCount = %s and maxCount = %s' %
               (thresh, minCount, maxCount))

    splitInfoDict = clusterer.subTreeSplitInfoDict((labels, [], protoRoot),
                                                   grower)
    dist, (aux, auxRat) = clusterer.growTree(splitInfoDict, grower)

    if verbosity >= 1:
        print 'cluster: %s leaves' % dist.countLeaves()
        print ('cluster: aux root = %s (%s) -> aux tree = %s (%s) (%s count)' %
               (protoRoot.aux / countRoot, d.Rat.toString(protoRoot.auxRat),
                aux / countRoot, d.Rat.toString(auxRat),
                countRoot))
    return dist
