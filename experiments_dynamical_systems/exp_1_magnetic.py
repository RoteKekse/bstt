#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Nov 17 10:25:27 2021

@author: goette
"""

import os
import sys

currentdir = os.path.dirname(os.path.realpath(__file__))
parentdir = os.path.dirname(currentdir)
sys.path.append(parentdir)

import numpy as np 
from misc import  __block, sinecosine_measures, random_homogenous_polynomial_sum_system2,random_fixed_variable_sum_system2,legendre_measures
from helpers import magneticDipolesSamples, SMat
from als import ALSSystem2
from bstt import BlockSparseTT
block = __block()

import warnings
warnings.filterwarnings("ignore")

# Parameters
order = [10,20,30,40,50]
degree = 2
interaction = [5,9]
trainSampleSize = [100*i for i in range(1,16)]
runs  = 5
maxSweeps=7
res = np.zeros((len(order),len(interaction),len(trainSampleSize),runs))
b = np.pi

for ii in range(len(order)):
    for jj in range(len(trainSampleSize)):
        for kk in range(len(interaction)):
            for ll in range(runs):
                print(f'Starting Order" {order[ii]} Sample {trainSampleSize[jj]} Interaction {interaction[kk]}')
                maxGroupSize = [1]+[2] +[3]*(order[ii]-4)+[2]+[1]
        
                # Model Parameters
                M = np.ones(order[ii])
                I = np.ones(order[ii])
                x = np.linspace(0,1*(order[ii]-1),order[ii])
                
                S = SMat(interaction[kk],order[ii])
                print(S)
                
                # Training Data Generation
                train_points,train_values = magneticDipolesSamples(order[ii],trainSampleSize[jj],M,x,I)
                #train_measures = legendre_measures(train_points, degree,-b,b)
                train_measures = sinecosine_measures(train_points)
                augmented_train_measures = np.concatenate([train_measures, np.ones((1,trainSampleSize[jj],degree+1))], axis=0)
                
                # Model initialization (bsTT)
                #coeffs = random_homogenous_polynomial_sum_system2([degree]*order,degree,maxGroupSize,interaction,S)
                coeffs = random_fixed_variable_sum_system2([degree]*order[ii],degree,maxGroupSize,interaction[kk],S)
                print(f"DOFS: {coeffs.dofs()}")
                print(f"Ranks: {coeffs.ranks}")
                print(f"Interaction: {coeffs.interactions}")
                
                # Solving
                solver = ALSSystem2(coeffs, augmented_train_measures,  train_values,_verbosity=1)
                solver.maxSweeps = maxSweeps
                solver.targetResidual = 1e-4
                solver.maxGroupSize=maxGroupSize
                solver.run()
                
                # Testing Data Generation
                testSampleSize = int(2e4)
                test_points,test_values = magneticDipolesSamples(order[ii],testSampleSize,M,x,I)
                #test_measures = legendre_measures(test_points, degree,-b,b)
                test_measures = sinecosine_measures(test_points)
                augmented_test_measures = np.concatenate([test_measures, np.ones((1,testSampleSize,degree+1))], axis=0)  # measures.shape == (order,N,degree+1)
                
                # Error Evaluation
                values = coeffs.evaluate(augmented_test_measures)
                values2 = coeffs.evaluate(augmented_train_measures)
                print("l2 on test data: ",np.linalg.norm(values -  test_values) / np.linalg.norm(test_values)," on training data: ",np.linalg.norm(values2 -  train_values) / np.linalg.norm(train_values))
                res[ii,kk,jj,ll] = np.linalg.norm(values -  test_values) / np.linalg.norm(test_values)
                np.save(f'exp_1_magnetic.data',res)