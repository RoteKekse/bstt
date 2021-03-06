# NOTE: This implementation is not meant to be memory efficient or fast but rather to test the approximation capabilities of the proposed model class.
import numpy as np
from sklearn.linear_model import LassoCV, RidgeCV, Ridge, Lasso
from scipy.linalg import block_diag, null_space, eigh
from bstt import Block, BlockSparseTensor, BlockSparseTT, BlockSparseTTSystem, BlockSparseTTSystem2
import sys
from matplotlib import pyplot as plt
import time


class ALS(object):
    """
    This is the standard scalar ALS on block sparse tensor trains. As methods there are l1 and l2. l2 is the standard least square solver.
    l1 is the regularized Lasso solver (see Philipp Trunsckes papers).
    By selecting increase rank and setting _maxGroupSize one gets rank adaptvity in the sense of shadow ranks as introduced by Sebastian Kraemer.
    """
    def __init__(self, _bstt, _measurements, _values, _localL2Gramians=None, _localH1Gramians=None, _maxGroupSize=3, _verbosity=0):
        assert isinstance(_bstt, BlockSparseTT)
        self.bstt = _bstt
        assert isinstance(_measurements, np.ndarray) and isinstance(
            _values, np.ndarray)
        assert _maxGroupSize > 0
        assert len(_measurements) == self.bstt.order
        assert all(compMeas.shape == (len(_values), dim)
                   for compMeas, dim in zip(_measurements, self.bstt.dimensions))
        self.measurements = _measurements
        self.values = _values
        self.verbosity = _verbosity
        self.maxSweeps = 100
        self.initialSweeps = 2
        self.targetResidual = 1e-8
        self.minDecrease = 1e-4
        self.increaseRanks = False
        self.smin = 0.01
        self.sminFactor = 0.01
        self.maxGroupSize = _maxGroupSize
        self.method = 'l1'

        if (not _localH1Gramians):
            self.localH1Gramians = [np.eye(d) for d in self.bstt.dimensions]
        else:
            assert isinstance(_localH1Gramians, list) and len(
                _localH1Gramians) == self.bstt.order
            for i in range(len(_localH1Gramians)):
                lG = _localH1Gramians[i]
                assert isinstance(lG, np.ndarray) and lG.shape == (
                    self.bstt.dimensions[i], self.bstt.dimensions[i])
                eigs_tmp = np.around(np.linalg.eigvals(lG), decimals=14)
                assert np.all(eigs_tmp >= 0) and np.allclose(
                    lG, lG.T, rtol=1e-14, atol=1e-14)
            self.localH1Gramians = _localH1Gramians

        if (not _localL2Gramians):
            self.localL2Gramians = [np.eye(d) for d in self.bstt.dimensions]
        else:
            assert isinstance(_localL2Gramians, list) and len(
                _localL2Gramians) == self.bstt.order
            for i in range(len(_localL2Gramians)):
                lG = _localL2Gramians[i]
                assert isinstance(lG, np.ndarray) and lG.shape == (
                    self.bstt.dimensions[i], self.bstt.dimensions[i])
                eigs_tmp = np.around(np.linalg.eigvals(lG), decimals=14)
                assert np.all(eigs_tmp >= 0) and np.allclose(
                    lG, lG.T, rtol=1e-14, atol=1e-14)
            self.localL2Gramians = _localL2Gramians

        self.leftStack = [np.ones((len(self.values), 1))] + \
            [None]*(self.bstt.order-1)
        self.rightStack = [np.ones((len(self.values), 1))]
        self.leftH1GramianStack = [
            np.ones([1, 1])] + [None]*(self.bstt.order-1)
        self.rightH1GramianStack = [np.ones([1, 1])]
        self.leftL2GramianStack = [
            np.ones([1, 1])] + [None]*(self.bstt.order-1)
        self.rightL2GramianStack = [np.ones([1, 1])]

        self.bstt.assume_corePosition(self.bstt.order-1)
        while self.bstt.corePosition > 0:
            self.move_core('left')

    def move_core(self, _direction):
        assert len(self.leftStack) + len(self.rightStack) == self.bstt.order+1
        assert len(self.leftH1GramianStack) + \
            len(self.rightH1GramianStack) == self.bstt.order+1
        assert len(self.leftL2GramianStack) + \
            len(self.rightL2GramianStack) == self.bstt.order+1
        valid_stacks = all(
            entry is not None for entry in self.leftStack + self.rightStack)
        if self.verbosity >= 2 and valid_stacks:
            pre_res = self.residual()
        singValues = self.bstt.move_core(_direction)
        if _direction == 'left':
            self.leftStack.pop()
            self.leftH1GramianStack.pop()
            self.leftL2GramianStack.pop()
            self.rightStack.append(np.einsum(
                'ler, ne, nr -> nl', self.bstt.components[self.bstt.corePosition+1], self.measurements[self.bstt.corePosition+1], self.rightStack[-1]))
            self.rightH1GramianStack.append(np.einsum(
                'ijk, lmn, jm,kn -> il', self.bstt.components[self.bstt.corePosition+1],  self.bstt.components[self.bstt.corePosition+1], self.localH1Gramians[self.bstt.corePosition+1], self.rightH1GramianStack[-1]))
            self.rightL2GramianStack.append(np.einsum(
                'ijk, lmn, jm,kn -> il', self.bstt.components[self.bstt.corePosition+1],  self.bstt.components[self.bstt.corePosition+1], self.localL2Gramians[self.bstt.corePosition+1], self.rightL2GramianStack[-1]))
            if self.verbosity >= 2:
                if valid_stacks:
                    print(
                        f"move_core {self.bstt.corePosition+1} --> {self.bstt.corePosition}.  (residual: {pre_res:.2e} --> {self.residual():.2e})")
                else:
                    print(
                        f"move_core {self.bstt.corePosition+1} --> {self.bstt.corePosition}.")
        elif _direction == 'right':
            if self.increaseRanks:
                slices = self.bstt.getUniqueSlices(0)
                for i, slc in zip(reversed(range(len(slices))), reversed(slices)):
                    if np.min(singValues[slc]) > self.smin and slc.stop-slc.start < self.bstt.MaxSize(i, self.bstt.corePosition-1, self.maxGroupSize):
                        assert np.allclose(np.einsum('ijk,ijl->kl', self.bstt.components[self.bstt.corePosition-1], self.bstt.components[self.bstt.corePosition-1]), np.eye(
                            self.bstt.components[self.bstt.corePosition-1].shape[2]), rtol=1e-12, atol=1e-12)
                        u = self.calculate_update(slc, 'left')
                        self.bstt.increase_block(i, u, np.zeros(
                            self.bstt.components[self.bstt.corePosition].shape[1:3]), 'left')
                        assert np.allclose(np.einsum('ijk,ijl->kl', self.bstt.components[self.bstt.corePosition-1], self.bstt.components[self.bstt.corePosition-1]), np.eye(
                            self.bstt.components[self.bstt.corePosition-1].shape[2]), rtol=1e-12, atol=1e-12)
                        if self.verbosity >= 2:
                            print(
                                f"Increased block {i} mode 2 of componment {self.bstt.corePosition-1}. Size before {slc.stop-slc.start}, size now {slc.stop-slc.start+1} of maximal Size {self.bstt.MaxSize(i,self.bstt.corePosition-1,self.maxGroupSize)}")

            self.rightStack.pop()
            self.rightH1GramianStack.pop()
            self.rightL2GramianStack.pop()
            self.leftStack.append(np.einsum(
                'nl, ne, ler -> nr', self.leftStack[-1], self.measurements[self.bstt.corePosition-1], self.bstt.components[self.bstt.corePosition-1]))
            self.leftH1GramianStack.append(np.einsum(
                'ijk, lmn, jm,il -> kn', self.bstt.components[self.bstt.corePosition-1],  self.bstt.components[self.bstt.corePosition-1], self.localH1Gramians[self.bstt.corePosition-1], self.leftH1GramianStack[-1]))
            self.leftL2GramianStack.append(np.einsum(
                'ijk, lmn, jm,il -> kn', self.bstt.components[self.bstt.corePosition-1],  self.bstt.components[self.bstt.corePosition-1], self.localL2Gramians[self.bstt.corePosition-1], self.leftL2GramianStack[-1]))
            if self.verbosity >= 2:
                if valid_stacks:
                    print(
                        f"move_core {self.bstt.corePosition-1} --> {self.bstt.corePosition}.  (residual: {pre_res:.2e} --> {self.residual():.2e})")
                else:
                    print(
                        f"move_core {self.bstt.corePosition-1} --> {self.bstt.corePosition}.")
        else:
            raise ValueError(
                f"Unknown _direction. Expected 'left' or 'right' but got '{_direction}'")

    def residual(self):
        core = self.bstt.components[self.bstt.corePosition]
        L = self.leftStack[-1]
        E = self.measurements[self.bstt.corePosition]
        R = self.rightStack[-1]
        pred = np.einsum('ler,nl,ne,nr -> n', core, L, E, R)
        return np.linalg.norm(pred - self.values) / np.linalg.norm(self.values)

    def calculate_update(self, slc, _direction):
        if _direction == 'left':
            Gramian = np.einsum(
                'ij,kl->ikjl', self.leftH1GramianStack[-1], self.localH1Gramians[self.bstt.corePosition-1])
            n = Gramian.shape[0]*Gramian.shape[1]
            basis = np.eye(n)
            basis = basis.reshape(Gramian.shape)
            blocks = self.bstt.getAllBlocksOfSlice(
                self.bstt.corePosition-1, slc, 2)
            for block in blocks:
                basis[block[0], block[1], block[0], block[1]] = 0
            basis = basis.reshape(n, n)
            left = self.bstt.components[self.bstt.corePosition -
                                        1][:, :, slc].reshape(n, -1)
            basis = np.concatenate([basis, left], axis=1)
            ns = null_space(basis.T)
            assert ns.size > 0
            ns = np.round(ns.reshape(*Gramian.shape[0:2], -1), decimals=14)
            projGramian = np.einsum('ijkl,ijm,kln->mn', Gramian, ns, ns)
            pGe, pGP = np.linalg.eigh(projGramian)
            return np.einsum('ijk,k->ij', ns, pGP[0])

    def microstep(self):
        if self.verbosity >= 2:
            pre_res = self.residual()

        core = self.bstt.components[self.bstt.corePosition]
        L = self.leftStack[-1]
        E = self.measurements[self.bstt.corePosition]
        R = self.rightStack[-1]
        coreBlocks = self.bstt.blocks[self.bstt.corePosition]
        N = len(self.values)
        
        if self.method == 'l1':
            LGH1 = self.leftH1GramianStack[-1]
            EGH1 = self.localH1Gramians[self.bstt.corePosition]
            RGH1 = self.rightH1GramianStack[-1]
    
            LGL2 = self.leftL2GramianStack[-1]
            EGL2 = self.localL2Gramians[self.bstt.corePosition]
            RGL2 = self.rightL2GramianStack[-1]
            assert np.allclose(LGL2, np.eye(LGL2.shape[0]), rtol=1e-12, atol=1e-12)
    
            Op_blocks = []
            Weights = []
            Tr_blocks = []
            for block in coreBlocks:
                op = np.einsum('nl,ne,nr -> nler',
                               L[:, block[0]], E[:, block[1]], R[:, block[2]])
                Op_blocks.append(op.reshape(N, -1))
    
                # update stacks after diagonalization of left and right gramian
                Le, LP = np.linalg.eigh(LGH1[block[0], block[0]])
                Ee, EP = np.linalg.eigh(EGH1[block[1], block[1]])
                Re, RP = np.linalg.eigh(RGH1[block[2], block[2]])
                #assert np.allclose(LP.T@LGH1[block[0],block[0]]@LP, np.diag(Le), rtol=1e-12, atol=1e-12)
                #assert np.allclose(RP.T@RGH1[block[2],block[2]]@RP, np.diag(Re), rtol=1e-12, atol=1e-12),RP.T@RGH1[block[2],block[2]]@RP
    
                RPL2 = RP.T@RGL2[block[2], block[2]]@RP
                Re = Re/np.diag(RPL2)
    
                tr = np.einsum('il,jm,kn->ijklmn', LP, EP, RP)
                Tr_blocks.append(tr.reshape(
                    Op_blocks[-1].shape[1], Op_blocks[-1].shape[1]))
    
                Weights.extend(np.einsum('i,j,k->ijk', Le, Ee, Re).reshape(-1))
            Op = np.concatenate(Op_blocks, axis=1)
            Transform = block_diag(*Tr_blocks)
            assert np.allclose(Transform@Transform.T,
                               np.eye(Transform.shape[0]), rtol=1e-14, atol=1e-14)
    
            Weights = np.sqrt(Weights)
            inverseWeightMatrix = np.diag(np.reciprocal(Weights))
    
            OpTr = Op@Transform@inverseWeightMatrix
            reg = LassoCV(eps=1e-7, cv=10, random_state=0,
                          fit_intercept=False).fit(OpTr, self.values)
            Res = reg.coef_
    
            core[...] = BlockSparseTensor(
                Transform@inverseWeightMatrix@Res, coreBlocks, core.shape).toarray()
        elif self.method == 'l2':
            Op_blocks = []
            for block in coreBlocks:
                op = np.einsum('nl,ne,nr -> nler', L[:, block[0]], E[:, block[1]], R[:, block[2]])
                Op_blocks.append(op.reshape(N,-1))
            Op = np.concatenate(Op_blocks, axis=1)
            # Res = np.linalg.solve(Op.T @ Op, Op.T @ self.values)
            Res, *_ = np.linalg.lstsq(Op, self.values, rcond=None)  # When Op.T@Op is singular (less samples then dofs in this component) then lstsq returns the minimal norm solution.
            core[...] = BlockSparseTensor(Res, coreBlocks, core.shape).toarray()
        else:
            assert False, "No valid method chosen, methods are l1 or l2"
        if self.verbosity >= 2:
            print(
                f"microstep.  (residual: {pre_res:.2e} --> {self.residual():.2e})")

    def run(self):
        prev_residual = self.residual()
        self.smin = prev_residual*self.sminFactor
        if self.verbosity >= 1:
            print(f"Initial residuum: {prev_residual:.2e}")
        increaseRanks = False  # prepare initial sweeps before rank increase
        if self.increaseRanks:
            increaseRanks = True
            self.increaseRanks = False
        for sweep in range(self.maxSweeps):
            if sweep >= self.initialSweeps and increaseRanks == True:
                self.increaseRanks = True
            while self.bstt.corePosition < self.bstt.order-1:
                self.microstep()
                self.move_core('right')
            while self.bstt.corePosition > 0:
                self.microstep()
                self.move_core('left')

            residual = self.residual()
            if self.verbosity >= 1:
                print(f"[{sweep}] Residuum: {residual:.2e}")

            if residual < self.targetResidual:
                if self.verbosity >= 1:
                    print(f"Terminating (targetResidual reached)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            if residual > prev_residual:
                if self.verbosity >= 1:
                    print(f"Terminating (residual increases)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            if (prev_residual - residual) < self.minDecrease*residual:
                if self.verbosity >= 1:
                    print(f"Terminating (minDecrease reached)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            prev_residual = residual
            self.smin = prev_residual*self.sminFactor

        if self.verbosity >= 1:
            print(f"Terminating (maxSweeps reached)")
        if self.verbosity >= 1:
            print(f"Final residuum: {self.residual():.2e}")

class ALSGrad(object):
    '''
    This is an ALS which learns the scalar function from data of the gradient. _measurements are the evaluation of the basis funcitons
    as above. _measurements_grad are the evaluation of the derivatives of the basis functions.
    Note: It is important to choose a block sparse format which fixes at least one degree of freedom, e.g. the constant polynomial
    since else the problem is not unique.
    '''
    def __init__(self, _bstt, _measurements,_measurements_grad, _values, _verbosity=0):
        self.bstt = _bstt
        assert isinstance(_measurements, np.ndarray) and isinstance(_values, np.ndarray)
        assert len(_measurements) == self.bstt.order
        assert len(_measurements_grad) == self.bstt.order or  len(_measurements_grad) == self.bstt.order -1
        assert all(compMeas.shape == (len(_values), dim) for compMeas, dim in zip(_measurements, self.bstt.dimensions))
        self.measurements = _measurements
        self.measurements_grad = _measurements_grad
        self.values = _values
        self.verbosity = _verbosity
        self.maxSweeps = 100
        self.targetResidual = 1e-8
        self.minDecrease = 1e-4

        self.leftStack1 = [np.ones((1,len(self.values),1))] + [None]*(self.bstt.order-1)
        self.leftStack2 = [np.ones((1,len(self.values),1))] + [None]*(self.bstt.order-1)
        self.leftStack1rhs = [np.ones((1,len(self.values)))] + [None]*(self.bstt.order-1)
        self.leftStack2rhs = [np.ones((1,len(self.values)))] + [None]*(self.bstt.order-1)
        self.rightStack1 = [np.ones((1,len(self.values),1))]
        self.rightStack2 = [np.ones((1,len(self.values),1))]
        self.rightStack1rhs = [np.ones((1,len(self.values)))]
        self.rightStack2rhs = [np.ones((1,len(self.values)))]
        self.bstt.assume_corePosition(self.bstt.order-1)
        while self.bstt.corePosition > 0:
            self.move_core('left')

    def move_core(self, _direction):
        assert len(self.leftStack1) + len(self.rightStack1) == self.bstt.order+1
        assert len(self.leftStack2) + len(self.rightStack2) == self.bstt.order+1
        assert len(self.leftStack1rhs) + len(self.rightStack1rhs) == self.bstt.order+1
        assert len(self.leftStack2rhs) + len(self.rightStack2rhs) == self.bstt.order+1
        valid_stacks = all(entry is not None for entry in self.leftStack1 + self.rightStack1+self.leftStack2 + self.rightStack2+self.leftStack1rhs + self.rightStack1rhs+self.leftStack2rhs + self.rightStack2rhs)
        if self.verbosity >= 2 and valid_stacks:
            pre_res = self.residual()
        self.bstt.move_core(_direction)
        if _direction == 'left':
            self.leftStack1.pop()
            self.leftStack2.pop()
            self.leftStack1rhs.pop()
            self.leftStack2rhs.pop()
            comp_measure = np.einsum('ler, me  -> lmr', self.bstt.components[self.bstt.corePosition+1], self.measurements[self.bstt.corePosition+1])
            if self.bstt.corePosition+1 == self.bstt.order-1:
                self.rightStack1.append(np.einsum('imk, lmn, kmn -> iml', comp_measure,comp_measure, self.rightStack1[-1]))
                self.rightStack2.append(np.einsum('imk, lmn, kmn -> iml', comp_measure,comp_measure, self.rightStack2[-1]))
                self.rightStack1rhs.append(np.einsum('imk, km -> im', comp_measure, self.rightStack1rhs[-1]))
                self.rightStack2rhs.append(np.einsum('imk, km -> im', comp_measure, self.rightStack2rhs[-1]))
            else:
                comp_measure_grad = np.einsum('ler, me  -> lmr', self.bstt.components[self.bstt.corePosition+1], self.measurements_grad[self.bstt.corePosition+1])
                stack2 = np.einsum('imk, lmn, kmn -> iml', comp_measure_grad,comp_measure_grad, self.rightStack1[-1])
                stack2rhs = np.einsum('imk, m,km -> im', comp_measure_grad,self.values[:,self.bstt.corePosition+1],  self.rightStack1rhs[-1])
                if self.bstt.corePosition+1 < self.bstt.order-2:
                    stack2 += np.einsum('imk, lmn, kmn -> iml', comp_measure,comp_measure, self.rightStack2[-1])
                    stack2rhs += np.einsum('imk, km -> im', comp_measure, self.rightStack2rhs[-1])
      
                self.rightStack2.append(stack2)
                self.rightStack1.append(np.einsum('imk, lmn, kmn -> iml', comp_measure,comp_measure, self.rightStack1[-1]))
                
                self.rightStack2rhs.append(stack2rhs)
                self.rightStack1rhs.append(np.einsum('imk, km -> im', comp_measure, self.rightStack1rhs[-1]))
            if self.verbosity >= 2:
                if valid_stacks:
                    print(f"move_core {self.bstt.corePosition+1} --> {self.bstt.corePosition}.  (residual: {pre_res:.2e} --> {self.residual():.2e})")
                else:
                    print(f"move_core {self.bstt.corePosition+1} --> {self.bstt.corePosition}.")
        elif _direction == 'right':
            self.rightStack1.pop()
            self.rightStack2.pop()            
            self.rightStack1rhs.pop()
            self.rightStack2rhs.pop()

            comp_measure = np.einsum('ler, me  -> lmr', self.bstt.components[self.bstt.corePosition-1], self.measurements[self.bstt.corePosition-1])
            comp_measure_grad = np.einsum('ler, me  -> lmr', self.bstt.components[self.bstt.corePosition-1], self.measurements_grad[self.bstt.corePosition-1])
            stack2 = np.einsum('iml,imk, lmn -> kmn',  self.leftStack1[-1], comp_measure_grad, comp_measure_grad)
            stack2rhs = np.einsum('im, m,imk -> km', self.leftStack1rhs[-1],self.values[:,self.bstt.corePosition-1], comp_measure_grad )
            if self.bstt.corePosition-1 > 0:
                stack2 += np.einsum('iml,imk, lmn -> kmn',   self.leftStack2[-1], comp_measure, comp_measure)
                stack2rhs += np.einsum('im, imk -> km', self.leftStack2rhs[-1],comp_measure)

            self.leftStack2.append(stack2)
            self.leftStack1.append(np.einsum('iml,imk, lmn -> kmn',  self.leftStack1[-1],comp_measure,comp_measure))
            
            self.leftStack2rhs.append(stack2rhs)
            self.leftStack1rhs.append(np.einsum('im, imk -> km', self.leftStack1rhs[-1],comp_measure ))
            if self.verbosity >= 2:
                if valid_stacks:
                    print(f"move_core {self.bstt.corePosition-1} --> {self.bstt.corePosition}.  (residual: {pre_res:.2e} --> {self.residual():.2e})")
                else:
                    print(f"move_core {self.bstt.corePosition-1} --> {self.bstt.corePosition}.")
        else:
            raise ValueError(f"Unknown _direction. Expected 'left' or 'right' but got '{_direction}'")

    def residual(self):
        res = 0
        for pos in range(self.bstt.order-1):
            tmp_measures = self.measurements.copy()
            tmp_measures[pos] = self.measurements_grad[pos]
            tmp_res = self.bstt.evaluate(tmp_measures)
            res += np.linalg.norm(tmp_res -  self.values[:,pos])**2    
        return np.sqrt(res) / np.linalg.norm(self.values)

    def microstep(self):
        if self.verbosity >= 2:
            pre_res = self.residual()

        core = self.bstt.components[self.bstt.corePosition]
        L1 = self.leftStack1[-1]
        L2 = self.leftStack2[-1]
        L1rhs = self.leftStack1rhs[-1]
        L2rhs = self.leftStack2rhs[-1]
        E = self.measurements[self.bstt.corePosition]
        E_grad = self.measurements_grad[self.bstt.corePosition]
 

        E_op = np.einsum('mp,mq->pmq',E,E)
        E_grad_op = np.einsum('mp,mq->pmq',E_grad,E_grad)
        R1 = self.rightStack1[-1]
        R2 = self.rightStack2[-1]
        R1rhs = self.rightStack1rhs[-1]
        R2rhs = self.rightStack2rhs[-1]
        coreBlocks = self.bstt.blocks[self.bstt.corePosition]

        Op_blocks = []
        Rhs_blocks = []
        for block1 in coreBlocks:
            Op_blocks_col = []
            for block2 in coreBlocks:
                op = np.einsum('imk,pmq,lmn -> iplkqn', L2[block1[0],:, block2[0]], E_op[block1[1],:, block2[1]], R1[block1[2],:, block2[2]])
                if self.bstt.corePosition < self.bstt.order-2:
                    op += np.einsum('imk,pmq,lmn -> iplkqn', L1[block1[0],:, block2[0]], E_op[block1[1],:, block2[1]], R2[block1[2],:, block2[2]])
                op += np.einsum('imk,pmq,lmn -> iplkqn', L1[block1[0],:, block2[0]], E_grad_op[block1[1],:, block2[1]], R1[block1[2],:, block2[2]])
                dim1 = op.shape[0]*op.shape[1]*op.shape[2]
                dim2 = op.shape[3]*op.shape[4]*op.shape[5]
                Op_blocks_col.append( op.reshape(dim1,dim2))
            
            rhs = np.einsum('im,mp,lm -> ipl', L2rhs[block1[0],:], E[:,block1[1]], R1rhs[block1[2],:])
            if self.bstt.corePosition < self.bstt.order-2:
                rhs += np.einsum('im,mp,lm -> ipl', L1rhs[block1[0],:], E[:,block1[1]], R2rhs[block1[2],:])
            rhs += np.einsum('im,mp,lm,m -> ipl', L1rhs[block1[0],:], E_grad[:,block1[1]], R1rhs[block1[2],:],self.values[:,self.bstt.corePosition])
            rhs = rhs.reshape(dim1)
            
            Rhs_blocks.append(rhs.reshape(dim1))
            Op_blocks.append(np.concatenate(Op_blocks_col, axis=1))
           
        Op = np.concatenate(Op_blocks, axis=0)
        Rhs = np.concatenate(Rhs_blocks, axis=0)
        Res = np.linalg.solve(Op, Rhs)
        #Res, *_ = np.linalg.lstsq(Op, self.values, rcond=None)  # When Op.T@Op is singular (less samples then dofs in this component) then lstsq returns the minimal norm solution.
        core[...] = BlockSparseTensor(Res, coreBlocks, core.shape).toarray()

        if self.verbosity >= 2:
            print(f"microstep.  (residual: {pre_res:.2e} --> {self.residual():.2e})")

    def run(self):
        prev_residual = self.residual()
        if self.verbosity >= 1: print(f"Initial residuum: {prev_residual:.2e}")
        for sweep in range(self.maxSweeps):
            while self.bstt.corePosition < self.bstt.order-2:
                self.microstep()
                self.move_core('right')
            while self.bstt.corePosition > 0:
                self.microstep()
                self.move_core('left')

            residual = self.residual()
            if self.verbosity >= 1: print(f"[{sweep}] Residuum: {residual:.2e}")

            if residual < self.targetResidual:
                if self.verbosity >= 1:
                    print(f"Terminating (targetResidual reached)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            if residual > prev_residual:
                if self.verbosity >= 1:
                    print(f"Terminating (residual increases)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            if (prev_residual - residual) < self.minDecrease*residual:
                if self.verbosity >= 1:
                    print(f"Terminating (minDecrease reached)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            prev_residual = residual

        if self.verbosity >= 1: print(f"Terminating (maxSweeps reached)")
        if self.verbosity >= 1: print(f"Final residuum: {self.residual():.2e}")

class ALSSystem(object):
    '''
    This is an ALS which learns a system of equation with the use of a selection tensor (as in A. Goessmann et al.)
    '''
    def __init__(self, _bstt, _measurements, _values, _localL2Gramians=None, _localH1Gramians=None, _maxGroupSize=3, _verbosity=0):
        self.bstt = _bstt
        assert isinstance(_bstt, BlockSparseTTSystem)
        assert isinstance(_measurements, np.ndarray) and isinstance(
            _values, np.ndarray)
        assert _maxGroupSize > 0
        assert len(_measurements) == self.bstt.order
        assert all(compMeas.shape == (len(_values), dim)
                   for compMeas, dim in zip(_measurements, self.bstt.dimensions))
        assert (_values.shape == (
            _measurements.shape[1], self.bstt.numberOfEquations))
        self.measurements = _measurements
        self.numberOfSamples = _measurements.shape[1]
        self.values = _values
        self.verbosity = _verbosity
        self.maxSweeps = 100
        self.initialSweeps = 2
        self.targetResidual = 1e-8
        self.minDecrease = 1e-4
        self.increaseRanks = False
        self.smin = 0.01
        self.sminFactor = 0.01
        self.maxGroupSize = _maxGroupSize
        self.alpha = 0.1
        if (not _localH1Gramians):
            self.localH1Gramians = [np.eye(d) for d in self.bstt.dimensions]
        else:
            assert isinstance(_localH1Gramians, list) and len(
                _localH1Gramians) == self.bstt.order
            for i in range(len(_localH1Gramians)):
                lG = _localH1Gramians[i]
                assert isinstance(lG, np.ndarray) and lG.shape == (
                    self.bstt.dimensions[i], self.bstt.dimensions[i])
                eigs_tmp = np.around(np.linalg.eigvals(lG), decimals=14)
                assert np.all(eigs_tmp >= 0) and np.allclose(
                    lG, lG.T, rtol=1e-14, atol=1e-14)
            self.localH1Gramians = _localH1Gramians

        if (not _localL2Gramians):
            self.localL2Gramians = [np.eye(d) for d in self.bstt.dimensions]
        else:
            assert isinstance(_localL2Gramians, list) and len(
                _localL2Gramians) == self.bstt.order
            for i in range(len(_localL2Gramians)):
                lG = _localL2Gramians[i]
                assert isinstance(lG, np.ndarray) and lG.shape == (
                    self.bstt.dimensions[i], self.bstt.dimensions[i])
                eigs_tmp = np.around(np.linalg.eigvals(lG), decimals=14)
                assert np.all(eigs_tmp >= 0) and np.allclose(
                    lG, lG.T, rtol=1e-14, atol=1e-14)
            self.localL2Gramians = _localL2Gramians

        self.leftStack = [np.ones(
            (self.numberOfSamples, 1, self.bstt.numberOfEquations))] + [None]*(self.bstt.order-1)
        self.rightStack = [
            np.ones((self.numberOfSamples, 1, self.bstt.numberOfEquations))]
        self.leftH1GramianStack = [
            np.ones([1, 1, self.bstt.numberOfEquations])] + [None]*(self.bstt.order-1)
        self.rightH1GramianStack = [
            np.ones([1, 1, self.bstt.numberOfEquations])]
        self.leftL2GramianStack = [
            np.ones([1, 1, self.bstt.numberOfEquations])] + [None]*(self.bstt.order-1)
        self.rightL2GramianStack = [
            np.ones([1, 1, self.bstt.numberOfEquations])]

        self.bstt.assume_corePosition(self.bstt.order-1)
        while self.bstt.corePosition > 0:
            self.move_core('left')
        self.bstt.components[self.bstt.corePosition] /= np.linalg.norm(
            self.bstt.components[self.bstt.corePosition])
        self.prev_residual = self.residual()

    def move_core(self, _direction):
        assert len(self.leftStack) + len(self.rightStack) == self.bstt.order+1
        assert len(self.leftH1GramianStack) + \
            len(self.rightH1GramianStack) == self.bstt.order+1
        assert len(self.leftL2GramianStack) + \
            len(self.rightL2GramianStack) == self.bstt.order+1
        valid_stacks = all(
            entry is not None for entry in self.leftStack + self.rightStack)
        if self.verbosity >= 2 and valid_stacks:
            pre_res = self.residual()
        self.bstt.move_core(_direction)
        if _direction == 'left':
            Smat = self.bstt.selectionMatrix(
                self.bstt.corePosition+1, self.bstt.numberOfEquations)
            comp = self.bstt.components[self.bstt.corePosition+1]
            self.leftStack.pop()
            # self.leftH1GramianStack.pop()
            # self.leftL2GramianStack.pop()
            self.rightStack.append(np.einsum('lesr, ne, sd, nrd -> nld', comp,
                                   self.measurements[self.bstt.corePosition+1], Smat, self.rightStack[-1]))
            #self.rightH1GramianStack.append(np.einsum('ijsk, lmtn, jm,knd,sd,td -> ild', comp,  comp, self.localH1Gramians[self.bstt.corePosition+1], self.rightH1GramianStack[-1],Smat,Smat))
            #self.rightL2GramianStack.append(np.einsum('ijsk, lmtn, jm,knd,sd,td -> ild', comp,  comp, self.localL2Gramians[self.bstt.corePosition+1], self.rightL2GramianStack[-1],Smat,Smat))
            if self.verbosity >= 2:
                if valid_stacks:
                    print(
                        f"move_core {self.bstt.corePosition+1} --> {self.bstt.corePosition}.  (residual: {pre_res:.2e} --> {self.residual():.2e})")
                else:
                    print(
                        f"move_core {self.bstt.corePosition+1} --> {self.bstt.corePosition}.")
        elif _direction == 'right':
            Smat = self.bstt.selectionMatrix(
                self.bstt.corePosition-1, self.bstt.numberOfEquations)
            comp = self.bstt.components[self.bstt.corePosition-1]
            self.rightStack.pop()
            # self.rightH1GramianStack.pop()
            # self.rightL2GramianStack.pop()
            self.leftStack.append(np.einsum(
                'nld, ne, md, lemr -> nrd', self.leftStack[-1], self.measurements[self.bstt.corePosition-1], Smat, comp))
            #self.leftH1GramianStack.append(np.einsum('ijsk, lmtn, jm,ild,sd,td -> knd', comp,  comp, self.localH1Gramians[self.bstt.corePosition-1], self.leftH1GramianStack[-1],Smat,Smat))
            #self.leftL2GramianStack.append(np.einsum('ijsk, lmtn, jm,ild,sd,td -> knd', comp,  comp, self.localL2Gramians[self.bstt.corePosition-1], self.leftL2GramianStack[-1],Smat,Smat))
            if self.verbosity >= 2:
                if valid_stacks:
                    # (residual: {pre_res:.2e} --> {self.residual():.2e})")
                    print(
                        f"move_core {self.bstt.corePosition-1} --> {self.bstt.corePosition}.")
                else:
                    print(
                        f"move_core {self.bstt.corePosition-1} --> {self.bstt.corePosition}.")
        else:
            raise ValueError(
                f"Unknown _direction. Expected 'left' or 'right' but got '{_direction}'")

    def residual(self):
        core = self.bstt.components[self.bstt.corePosition]
        L = self.leftStack[-1]
        E = self.measurements[self.bstt.corePosition]
        R = self.rightStack[-1]
        S = self.bstt.selectionMatrix(
            self.bstt.corePosition, self.bstt.numberOfEquations)
        pred = np.einsum('lesr,nld,ne,sd,nrd -> nd', core, L, E, S, R)
        return np.linalg.norm(pred.reshape(-1) - self.values.reshape(-1)) / np.linalg.norm(self.values.reshape(-1))

    # def calculate_update(self,slc,_direction):
    #     if _direction == 'left':
    #         Gramian = np.einsum('ij,kl->ikjl',self.leftH1GramianStack[-1],self.localH1Gramians[self.bstt.corePosition-1])
    #         n = Gramian.shape[0]*Gramian.shape[1]
    #         basis = np.eye(n)
    #         basis = basis.reshape(Gramian.shape)
    #         blocks = self.bstt.getAllBlocksOfSlice(self.bstt.corePosition-1,slc,2)
    #         for block in blocks:
    #             basis[block[0],block[1],block[0],block[1]] = 0
    #         basis=basis.reshape(n,n)
    #         left = self.bstt.components[self.bstt.corePosition-1][:,:,slc].reshape(n,-1)
    #         basis = np.concatenate([basis,left],axis = 1)
    #         ns = null_space(basis.T)
    #         assert ns.size > 0
    #         ns = np.round(ns.reshape(*Gramian.shape[0:2],-1),decimals=14)
    #         projGramian = np.einsum('ijkl,ijm,kln->mn',Gramian,ns,ns)
    #         pGe, pGP = np.linalg.eigh(projGramian)
    #         return np.einsum('ijk,k->ij',ns,pGP[0])

    def microstep(self):
        if self.verbosity >= 2:
            pre_res = self.residual()

        L = self.leftStack[-1]
        E = self.measurements[self.bstt.corePosition]
        S = self.bstt.selectionMatrix(
            self.bstt.corePosition, self.bstt.numberOfEquations)
        R = self.rightStack[-1]
        coreBlocks = self.bstt.blocks[self.bstt.corePosition]
        # print(S)

        Op_blocks = []
        for block in coreBlocks:
            op = np.einsum('nld,ne,sd,nrd -> ndlesr',
                           L[:, block[0], :], E[:, block[1]], S[block[2], :], R[:, block[3], :])
            Op_blocks.append(op)

        core = np.zeros(self.bstt.components[self.bstt.corePosition].shape)
        shape = (core.shape[0], core.shape[1], core.shape[3])
        reducedBlocks = [Block((b[0], b[1], b[3])) for b in coreBlocks]
        for k in range(self.bstt.interaction[self.bstt.corePosition]):
            Op_blocks_eq = []
            eqs = [True if S[k, l] == 1 else False for l in range(
                self.bstt.numberOfEquations)]
            for o in Op_blocks:
                Op_blocks_eq.append(o[:, eqs, :, :, k, :].reshape(
                    self.numberOfSamples*np.sum(eqs), -1))

            Op = np.concatenate(Op_blocks_eq, axis=1)

            U, s, VT = np.linalg.svd(Op, full_matrices=False)
            #s = s[s>(s[0]*1e-16)]
            #Op = U[:,:len(s)]@np.diag(s)@VT[:len(s),:]
            rhs = self.values[:, eqs].reshape(-1, order='F')

            #Res, *_ = np.linalg.lstsq(Op, rhs, rcond=None)
            self.alpha = s[0]*1e-12*self.prev_residual
            Res = np.linalg.solve(Op.T@Op+self.alpha *
                                  np.eye(Op.shape[1]), Op.T@rhs)
            #core[:,:,k,:] = BlockSparseTensor(Transform@inverseWeightMatrix@Res, reducedBlocks, shape).toarray()
            core[:, :, k, :] = BlockSparseTensor(
                Res, reducedBlocks, shape).toarray()
            # print(Op.shape,np.linalg.matrix_rank(Op,tol=1e-16),s[-1])

        self.bstt.components[self.bstt.corePosition] = core
        if self.verbosity >= 2:
            print(
                f"microstep.  (residual: {pre_res:.2e} --> {self.residual():.2e}, Norm: {np.linalg.norm(self.bstt.components[self.bstt.corePosition])})")

    def run(self):
        prev_residual = self.residual()
        self.smin = prev_residual*self.sminFactor
        if self.verbosity >= 1:
            print(f"Initial residuum: {prev_residual:.2e}")
        increaseRanks = False  # prepare initial sweeps before rank increase
        if self.increaseRanks:
            increaseRanks = True
            self.increaseRanks = False
        for sweep in range(self.maxSweeps):
            #self.alpha =1e-15
            if sweep >= self.initialSweeps and increaseRanks == True:
                self.increaseRanks = True
            while self.bstt.corePosition < self.bstt.order-1:
                self.microstep()
                self.move_core('right')
            while self.bstt.corePosition > 0:
                self.microstep()
                self.move_core('left')
            residual = self.residual()
            if self.verbosity >= 1:
                print(f"[{sweep}] Residuum: {residual:.2e}, Norm: {np.linalg.norm(self.bstt.components[self.bstt.corePosition])}, alpha = {self.alpha}")

            if residual < self.targetResidual:
                if self.verbosity >= 1:
                    print(f"Terminating (targetResidual reached)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            # if residual > prev_residual:
            #     if self.verbosity >= 1:
            #         print(f"Terminating (residual increases)")
            #         print(f"Final residuum: {self.residual():.2e}")
            #     return

            # if (prev_residual - residual) < self.minDecrease*residual:
            #     if self.verbosity >= 1:
            #         print(f"Terminating (minDecrease reached)")
            #         print(f"Final residuum: {self.residual():.2e}")
            #     return

            prev_residual = residual
            self.smin = prev_residual*self.sminFactor

        if self.verbosity >= 1:
            print(f"Terminating (maxSweeps reached)")
        if self.verbosity >= 1:
            print(f"Final residuum: {self.residual():.2e}")


class ALSSystem2(object):
    '''
    This is an ALS which learns a system of equation with the use of weight sharing.
    '''
    def __init__(self, _coeffs, _measurements, _values, _verbosity=0):
        self.coeffs = _coeffs
        assert isinstance(_coeffs, BlockSparseTTSystem2)
        assert isinstance(_measurements, np.ndarray) and isinstance(
            _values, np.ndarray)
        assert len(_measurements) == self.coeffs.order
        assert all(compMeas.shape == (len(_values), dim)
                   for compMeas, dim in zip(_measurements, self.coeffs.dimensions))
        assert (_values.shape == (
            _measurements.shape[1], self.coeffs.numberOfEquations))
        self.measurements = _measurements
        self.numberOfSamples = _measurements.shape[1]
        self.values = _values
        self.verbosity = _verbosity
        self.maxSweeps = 100
        self.targetResidual = 1e-8
        self.minDecrease = 1e-3
        self.alpha = 0.1

        self.leftStack = [[np.ones((self.numberOfSamples, 1))] *
                          self.coeffs.numberOfEquations] + [None]*(self.coeffs.order-1)
        self.rightStack = [
            [np.ones((self.numberOfSamples, 1))]*self.coeffs.numberOfEquations]

        self.coeffs.assume_corePosition(self.coeffs.order-1)
        self.direction = 'left'
        while self.coeffs.corePosition > 0:
            self.move_core()

        self.prev_residual = 1.0

    def move_core(self):
        assert len(self.leftStack) + \
            len(self.rightStack) == self.coeffs.order+1
        self.coeffs.move_core(self.direction)
        if self.direction == 'left':
            self.leftStack.pop()
            newStack = []
            for eq in range(self.coeffs.numberOfEquations):
                comp = self.coeffs.bstts[self.coeffs.selectionMatrix[eq,
                        self.coeffs.corePosition+1]].components[self.coeffs.corePosition+1]
                newStack.append(np.einsum('ler, me, mr -> ml', comp,
                                self.measurements[self.coeffs.corePosition+1], self.rightStack[-1][eq]))
            self.rightStack.append(newStack)
            if self.verbosity >= 2:
                print(
                    f"move_core {self.coeffs.corePosition+1} --> {self.coeffs.corePosition}. ")
        elif self.direction == 'right':
            self.rightStack.pop()
            newStack = []
            for eq in range(self.coeffs.numberOfEquations):
                comp = self.coeffs.bstts[self.coeffs.selectionMatrix[eq,
                        self.coeffs.corePosition-1]].components[self.coeffs.corePosition-1]
                newStack.append(np.einsum(
                    'ml, me, ler -> mr', self.leftStack[-1][eq], self.measurements[self.coeffs.corePosition-1], comp))
            self.leftStack.append(newStack)
            if self.verbosity >= 2:
                print(
                    f"move_core {self.coeffs.corePosition-1} --> {self.coeffs.corePosition}.")
        else:
            raise ValueError(
                f"Unknown _direction. Expected 'left' or 'right' but got '{self.direction}'")

    def residual(self):
        pred = []
        for eq in range(self.coeffs.numberOfEquations):
            core = self.coeffs.bstts \
                    [self.coeffs.selectionMatrix[eq, self.coeffs.corePosition]] \
                    .components[self.coeffs.corePosition]
            L = self.leftStack[-1][eq]
            E = self.measurements[self.coeffs.corePosition]
            R = self.rightStack[-1][eq]
            pred.append(np.einsum('ler,ml,me,mr -> m', core, L, E, R))
        pred = np.column_stack(pred)
        return np.linalg.norm(pred.reshape(-1) - self.values.reshape(-1)) / np.linalg.norm(self.values.reshape(-1))

    def microstep(self):
        L = self.leftStack[-1]
        E = self.measurements[self.coeffs.corePosition]
        R = self.rightStack[-1]
        coreBlocks = self.coeffs.blocks[self.coeffs.corePosition]

        # Build for each equation the corresponding local operator
        Op_eq = []
        for eq in range(self.coeffs.numberOfEquations):
            Op_blocks_eq = []
            for block in coreBlocks:
                op = np.einsum(
                    'ml,me,mr -> mler', L[eq][:, block[0]], E[:, block[1]], R[eq][:, block[2]])
                Op_blocks_eq.append(op.reshape(self.numberOfSamples, -1))
            Op_eq.append(np.concatenate(Op_blocks_eq, axis=1))
        
        # Optimize interaction range many cores
        used = []
        for k in range(self.coeffs.interactions):
            core = self.coeffs.bstts[k].components[self.coeffs.corePosition]
            eqs = [True if self.coeffs.selectionMatrix[eq, self.coeffs.corePosition]
                   == k else False for eq in range(self.coeffs.numberOfEquations)]
            if sum(eqs) == 0: continue # skip if core is not used at the current position  
            if sum(eqs) == 1 or (self.direction == 'right' and k == self.coeffs.interactions-1 and self.coeffs.corePosition > 0) or  (self.direction == 'left' and k == 0 and self.coeffs.corePosition < self.coeffs.order-1):
                used.append('first')              
                Op_eq_aux = []
                for i in range(len(eqs)):
                    if eqs[i]:
                        Op_eq_aux.append(Op_eq[i])
                
                Op = np.concatenate(Op_eq_aux, axis=0)
    
                rhs = self.values[:, eqs].reshape(-1, order='F')
                Res, *_ = np.linalg.lstsq(Op, rhs, rcond=None)
                #Res = np.linalg.solve(Op.T@Op+self.alpha*np.eye(Op.shape[1]), Op.T@rhs)
                core[:, :, :] = BlockSparseTensor(
                    Res, coreBlocks, core.shape).toarray()
            elif (self.direction == 'right' and k == 0) or (self.direction == 'left' and k == 0 and self.coeffs.corePosition == self.coeffs.order-1): 
                used.append('second')            

                eqs2 = [True if self.coeffs.selectionMatrix[eq, self.coeffs.corePosition-1]
                       == k else False for eq in range(self.coeffs.numberOfEquations)]
                diff = np.array(eqs) == np.array(eqs2)
                switched_eqs = np.where(diff == diff.min())[0]
                blocks_switched_eq =  list(set([Block((b[0], b[0])) for b in coreBlocks]))

                # solve for coefficents for multiple equations
                Op_eq_aux = []
                for i in range(len(eqs2)):
                    if eqs2[i]:
                        Op_eq_aux.append(Op_eq[i])
                
                Op = np.concatenate(Op_eq_aux, axis=0)
    
                rhs = self.values[:, eqs2].reshape(-1, order='F')
                Res, *_ = np.linalg.lstsq(Op, rhs, rcond=None)
                #Res = np.linalg.solve(Op.T@Op+self.alpha*np.eye(Op.shape[1]), Op.T@rhs)
                core[:, :, :] = BlockSparseTensor(
                    Res, coreBlocks, core.shape).toarray()
                
                # find basistransformation to reuse coefficents
                for switched_eq in switched_eqs:
                    R_new = np.einsum('ler, me, mr -> ml', core,
                                    self.measurements[self.coeffs.corePosition], R[switched_eq])
                    Op_blocks_switched_eq = []
                    for block in blocks_switched_eq:
                        op = np.einsum(
                            'ml,mr -> mlr', L[switched_eq][:, block[0]], R_new[:, block[1]])
                        Op_blocks_switched_eq.append(op.reshape(self.numberOfSamples, -1))
                    Op_switched_eq = np.concatenate(Op_blocks_switched_eq, axis=1)
                    rhs_switched_eq = self.values[:, switched_eq].reshape(-1, order='F')
                    Res_switched_eq, *_ = np.linalg.lstsq(Op_switched_eq, rhs_switched_eq, rcond=None)
                    core_switched_eq = BlockSparseTensor(
                        Res_switched_eq,  blocks_switched_eq, (core.shape[0], core.shape[0])).toarray()
                    self.leftStack[-1][switched_eq] = np.einsum(
                       'ml, lr -> mr', self.leftStack[-1][switched_eq], core_switched_eq)
                    comp =  self.coeffs.bstts[ self.coeffs.selectionMatrix[switched_eq, 
                        self.coeffs.corePosition-1]].components[self.coeffs.corePosition-1]
                    self.coeffs.bstts[ self.coeffs.selectionMatrix[switched_eq, 
                        self.coeffs.corePosition-1]].components[self.coeffs.corePosition-1] = \
                        np.einsum('ler,rs->les',comp,core_switched_eq)
            elif self.direction == 'left' and k == self.coeffs.interactions-1 or (self.direction == 'right' and k ==  self.coeffs.interactions-1 and self.coeffs.corePosition ==0): 
                used.append('third')              
                eqs2 = [True if self.coeffs.selectionMatrix[eq, self.coeffs.corePosition+1]
                       == k else False for eq in range(self.coeffs.numberOfEquations)]
                diff = np.array(eqs) == np.array(eqs2)
                switched_eqs = np.where(diff == diff.min())[0] 
                blocks_switched_eq =  list(set([Block((b[2], b[2])) for b in coreBlocks]))
                
                # solve for coefficents for multiple equations
                Op_eq_aux = []
                for i in range(len(eqs2)):
                    if eqs2[i]:
                        Op_eq_aux.append(Op_eq[i])
                
                Op = np.concatenate(Op_eq_aux, axis=0)
    
                rhs = self.values[:, eqs2].reshape(-1, order='F')
                Res, *_ = np.linalg.lstsq(Op, rhs, rcond=None)
                #Res = np.linalg.solve(Op.T@Op+self.alpha*np.eye(Op.shape[1]), Op.T@rhs)
                core[:, :, :] = BlockSparseTensor(
                    Res, coreBlocks, core.shape).toarray()
                
                # find basistransformation to reuse coefficents
                for switched_eq in switched_eqs:
 
                    L_new = np.einsum( 'ml, me, ler -> mr', L[switched_eq], 
                                      self.measurements[self.coeffs.corePosition], core)
                    Op_blocks_switched_eq = []
                    for block in blocks_switched_eq:
                        op = np.einsum(
                            'ml,mr -> mlr', L_new[:, block[0]], R[switched_eq][:, block[1]])
                        Op_blocks_switched_eq.append(op.reshape(self.numberOfSamples, -1))
                    Op_switched_eq = np.concatenate(Op_blocks_switched_eq, axis=1)
                    rhs_switched_eq = self.values[:, switched_eq].reshape(-1, order='F')
                    Res_switched_eq, *_ = np.linalg.lstsq(Op_switched_eq, rhs_switched_eq, rcond=None)
                    core_switched_eq = BlockSparseTensor(
                        Res_switched_eq,  blocks_switched_eq, (core.shape[2], core.shape[2])).toarray()
                    self.rightStack[-1][switched_eq] = np.einsum(
                       'lr, mr -> ml', core_switched_eq,self.rightStack[-1][switched_eq])
                    comp =  self.coeffs.bstts[ self.coeffs.selectionMatrix[switched_eq, 
                        self.coeffs.corePosition+1]].components[self.coeffs.corePosition+1]
                    self.coeffs.bstts[ self.coeffs.selectionMatrix[switched_eq, 
                        self.coeffs.corePosition+1]].components[self.coeffs.corePosition+1] = \
                        np.einsum('kl,ler->ker',core_switched_eq,comp)
                    
        self.coeffs.verify()
        if self.verbosity >= 2:
            print(
                f"microstep.  (residual: {self.prev_residual:.2e} --> {self.residual():.2e}), Direction {self.direction}, Core {self.coeffs.corePosition}, used {used}, interaction {self.coeffs.interactions}")

    def run(self):
        self.prev_residual = self.residual()
        if self.verbosity >= 1:
            print(f"Initial residuum: {self.prev_residual:.2e}")
        for sweep in range(self.maxSweeps):
            self.direction = 'right'
            while self.coeffs.corePosition < self.coeffs.order-1:
                self.microstep()
                self.move_core()
            self.direction = 'left'
            while self.coeffs.corePosition > 0:
                self.microstep()
                self.move_core()
            self.microstep()
            residual = self.residual()
            if self.verbosity >= 1:
                print(f"[{sweep}] Residuum: {residual:.2e}")

            if residual < self.targetResidual:
                if self.verbosity >= 1:
                    print(f"Terminating (targetResidual reached)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            if residual > self.prev_residual and sweep > 0:
                if self.verbosity >= 1:
                    print(f"Terminating (residual increases)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            if (self.prev_residual - residual) < self.minDecrease*residual and sweep > 0:
                if self.verbosity >= 1:
                    print(f"Terminating (minDecrease reached)")
                    print(f"Final residuum: {self.residual():.2e}")
                return

            self.prev_residual = residual

        if self.verbosity >= 1:
            print(f"Terminating (maxSweeps reached)")
        if self.verbosity >= 1:
            print(f"Final residuum: {self.residual():.2e}")
